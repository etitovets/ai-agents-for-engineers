# Data contract and tool-to-source mapping

Recorded from the actual stand before any SQL was written. Sources of truth, in order:

1. `lesson_4/db/init.sql` (schema; executed once on first Postgres start)
2. the running database (`docker exec … psql`) for observed values and cardinalities
3. `lesson_4/simulator/simulate.py` for what is seeded and how
4. `lesson_4/README.md` for intent

The README's table listing is **incomplete** (it omits `request_logs.service`, `request_logs.method`,
`app_logs.request_id`, `services.name`, `services.description`); `init.sql` wins.

## Schema as discovered

### `services`

| column | type | notes |
| --- | --- | --- |
| `name` | `TEXT` PK | `payments`, `checkout`, `auth` |
| `team` | `TEXT` | e.g. `billing` |
| `oncall` | `TEXT` | e.g. `a.kuznetsov` |
| `dependencies` | `JSONB` default `'[]'` | JSON array of strings |
| `description` | `TEXT` default `''` | |

### `deploys`

| column | type | notes |
| --- | --- | --- |
| `id` | `BIGSERIAL` PK | |
| `service` | `TEXT` FK → `services.name` | catalog vocabulary |
| `version` | `TEXT` | e.g. `v1.5.0` |
| `deployed_at` | `TIMESTAMPTZ` | indexed `(service, deployed_at DESC)` |
| `author` | `TEXT` | |
| `notes` | `TEXT` default `''` | |

### `incidents`

| column | type | notes |
| --- | --- | --- |
| `id` | `TEXT` PK | `INC-001` … `INC-NNN` |
| `service` | `TEXT` FK → `services.name` | |
| `severity` | `TEXT` | CHECK: `low, medium, high, critical` |
| `status` | `TEXT` | CHECK: `open, acknowledged, resolved` |
| `opened_at` | `TIMESTAMPTZ` | indexed `(service, opened_at DESC)` |
| `title` | `TEXT` | |
| `summary` | `TEXT` NULL | often NULL for the active incident |

### `request_logs`

| column | type | notes |
| --- | --- | --- |
| `id` | `BIGSERIAL` PK | |
| `ts` | `TIMESTAMPTZ` | indexed `(ts DESC)`, `(service, ts DESC)` |
| `service` | `TEXT` | **runtime label**, e.g. `payments-api` |
| `endpoint` | `TEXT` | route template, e.g. `/api/v1/orders/{order_id}/price` |
| `method` | `TEXT` | `GET` |
| `status_code` | `INTEGER` | |
| `duration_ms` | `DOUBLE PRECISION` | milliseconds |
| `cache_hit` | `BOOLEAN` NULL | NULL for non-cached endpoints (catalog, health) |
| `cache_size` | `INTEGER` NULL | |
| `request_id` | `TEXT` | |

Observed endpoints: `/api/v1/orders/{order_id}/price`, `/api/v1/catalog/items`, `/health`
(a single liveness probe row).

### `app_logs`

| column | type | notes |
| --- | --- | --- |
| `id` | `BIGSERIAL` PK | |
| `ts` | `TIMESTAMPTZ` | indexed |
| `service` | `TEXT` | `payments-api`, `checkout`, `auth`, `deploy-bot` |
| `level` | `TEXT` | **no CHECK constraint**; observed `INFO`, `WARN` |
| `message` | `TEXT` | contains one deliberate prompt-injection line |
| `request_id` | `TEXT` NULL | |

### `price_events`

Append-only price-component journal, explicitly described by `lesson_4/README.md` as internal
service data rather than logs. **No tool exposes it.**

## Tool → source mapping

| Tool | Tables | Notes |
| --- | --- | --- |
| `incidents_search` | `incidents` | filter + order + limit, `limit+1` probe for `has_more` |
| `incident_get` | `incidents` | by primary key |
| `incident_acknowledge` | `incidents` | write: `UPDATE ... SET status` only, row locked |
| `incident_create_summary` | `incidents` | write: `UPDATE ... SET summary` only, row locked |
| `deploys_recent` | `deploys` | `ORDER BY deployed_at DESC, id DESC` |
| `logs_query` | `app_logs` | half-open `ts` window, optional level, literal substring |
| `metrics_latency` | `request_logs` | `date_bin` aggregation + `generate_series` fill |
| `runbook_get` | filesystem | `lesson_4/runbooks/<service>.md` |
| `service_catalog_get` | `services` | by primary key |

No other table is reachable. The two write tools may only change `incidents.status` and
`incidents.summary`; every other table and every other incident column is read-only. No tool accepts
a table name, column name, or SQL fragment.

## Service vocabulary (the central ambiguity)

`services`, `incidents`, `deploys`, and runbook filenames use **`payments` / `checkout` / `auth`**.
`request_logs` and `app_logs` use **`payments-api`** for the payments service, and logs additionally
contain **`deploy-bot`**, which has no catalog card.

Decision: expose identifiers **verbatim** with per-tool allowlists; do not alias or rewrite.

| Tool | Allowed values |
| --- | --- |
| `incidents_search`, `deploys_recent`, `runbook_get`, `service_catalog_get` | `payments`, `checkout`, `auth` |
| `logs_query` | `payments-api`, `checkout`, `auth`, `deploy-bot` |
| `metrics_latency` | `payments-api` |

Unknown values are `invalid_arguments` with the valid set in the message. Consequence:
`logs_query(service="payments")` does not silently return nothing, and `runbook_get("payments-api")`
is not silently empty — the former is rejected, the latter is `not_found` for a missing file.

## Endpoint scope

`request_logs.endpoint` does not identify a service on its own: the table has its own `service`
column and the schema permits the same endpoint on several services. `metrics_latency` therefore
requires `service` and allowlists two endpoints:

- `/api/v1/orders/{order_id}/price`
- `/api/v1/catalog/items`

`/health` is excluded: it is a liveness probe with no diagnostic value and no cache-hit data.
A regression test inserts a `checkout` row on the price endpoint and asserts it never appears in
`payments-api` metrics.

## Write tools

Both writes use one explicit transaction on the process-wide pool:

```sql
BEGIN;
SET TRANSACTION READ WRITE;
SELECT ... FROM incidents WHERE id = %(id)s FOR UPDATE;
-- validate, then
UPDATE incidents SET <authorized column> = %(value)s WHERE id = %(id)s RETURNING ...;
COMMIT;
```

The row lock serializes concurrent calls for the same incident. Pooled connections keep
`default_transaction_read_only = on`; only these transactions switch to read-write. The database role
is the hard boundary and has `UPDATE` only on `incidents.status` and `incidents.summary`.

### `incident_acknowledge`

| Current status | Result |
| --- | --- |
| `open` | `status = acknowledged`, `changed = true`, `previous_status = open` |
| `acknowledged` | no update, `changed = false`, `previous_status = acknowledged` |
| `resolved` | `invalid_transition`, nothing changed |
| missing ID | `not_found` |

Only `incidents.status` is written. The updated incident is returned as `item`, together with
`changed` and `previous_status`.

### `incident_create_summary`

- Input is trimmed; empty/whitespace-only summaries are rejected with `invalid_arguments`.
- Maximum length is **5 000 characters** after trimming.
- Any incident status may receive a summary.
- A missing summary is created (`changed = true`, `previous_summary_present = false`).
- An existing summary is overwritten (`changed = true`, `previous_summary_present = true`).
- An identical summary is a no-op (`changed = false`, `previous_summary_present = true`).
- Concurrent writes are serialized by `SELECT ... FOR UPDATE`; last writer wins.
- The updated incident is returned as `item`, together with `changed` and
  `previous_summary_present`.

Only `incidents.summary` is written.

## Aggregation contract for `metrics_latency`

- Buckets: `date_bin(bucket, ts, origin = window start)`, so the first bucket starts exactly at
  `start`; buckets are `[bucket_start, bucket_end)`.
- Empty buckets are emitted with `request_count = 0` and `NULL` statistics, via a
  `generate_series` left join. No bucket is dropped.
- `avg_latency_ms = round(avg(duration_ms), 2)`, unit **milliseconds**.
- `p95_latency_ms = round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), 2)` —
  **exact, linear interpolation** (the same expression the simulator and stand README use).
  Example: over `[10,20,30,40,50]`, `p95 = 48.0`, not `50.0`.
- `cache_hit_pct = round(100.0 * count(*) FILTER (WHERE cache_hit) / nullif(count(cache_hit), 0), 2)`.
  The denominator **excludes NULL `cache_hit`**; if no row in the bucket has a non-NULL value the
  result is `NULL`, not `0.0`.
- Bucket count is bounded: `ceil(span / bucket) <= 500`, else `invalid_arguments`.

## Windows, limits, ordering

| Tool | Default window | Maximum span | Default / max limit |
| --- | --- | --- | --- |
| `logs_query` | last 5 min | 24 h | 50 / 200 |
| `metrics_latency` | last 5 min | 24 h | n/a (≤500 buckets) |
| `incidents_search` | no time filter | 7 d when provided | 20 / 100 |
| `deploys_recent` | n/a | n/a | 5 / 50 |

- Intervals are half-open `[start, end)`; boundaries are tested (a row at `end` and a row just before
  `start` are both excluded).
- Ordering: incidents and deploys by time **descending**; logs by `ts` descending; buckets by
  `bucket_start` ascending.
- `has_more` is computed by fetching `limit + 1` rows; no total count is fabricated.

## Output field names and transformations

Source names are preserved. Documented transformations:

| Output | Source | Transformation |
| --- | --- | --- |
| `incidents.opened_at`, `deploys.deployed_at`, `logs.ts`, `bucket_start/end` | `TIMESTAMPTZ` | serialized RFC 3339 in UTC (`…Z`) |
| `dependencies` | `JSONB` array | `list[str]` |
| `logs.message` | `TEXT` | truncated to 2 000 chars, `message_truncated` flags it |
| `runbook.path` | absolute file | path relative to the runbooks directory |
| `cache_hit_pct` | derived | nullable float, 2 decimals |
| `severity`, `status` | `TEXT` | enums (`Severity`, `IncidentStatus`) |
| `level` (output) | `TEXT` | plain string, so unexpected stored values never fail a read |
| `changed` (write tools) | derived | `true` only when a row was actually updated |
| `previous_status` | pre-write `incidents.status` | returned by `incident_acknowledge` |
| `previous_summary_present` | pre-write `incidents.summary IS NOT NULL` | returned by `incident_create_summary` |

## `logs_query.query` semantics

Case-insensitive **literal substring** implemented as
`position(lower(%(query)s) in lower(message)) > 0`. Not a regex, not full-text search, not SQL.
`%` and `_` are literal characters (regression-tested). Maximum 200 characters; surrounding
whitespace is trimmed and an empty string means "no filter".

## Error semantics

| Code | Meaning |
| --- | --- |
| `invalid_arguments` | caller value outside the contract (service, endpoint, enum, range, bucket count, query/summary length) |
| `not_found` | validated identifier absent (`INC-999`, missing runbook file) |
| `invalid_transition` | requested incident state change is not allowed from the current status |
| `data_source_unavailable` | pool timeout, database error, write permission denied, unreadable runbook directory |
| `internal_error` | unexpected fault; details only on stderr |

Zero matches is a success: `items: []`, `count: 0`. Error text is static and never contains the DSN,
SQL, stack traces, or file paths.

## Access model

- Every connection is configured with `default_transaction_read_only = on` (and a statement
  timeout) in the pool's `configure` hook; connections run in autocommit mode, so all seven read
  tools run under read-only transactions.
- The two write tools open an explicit transaction and execute `SET TRANSACTION READ WRITE` before a
  parameterized `UPDATE`. They then commit or roll back that single transaction.
- The pool is created once per process, opened in the MCP lifespan, reused by all tools, and closed
  on shutdown. There is no second pool for writes.
- Recommended deployment uses the restricted `incident_mcp` role (see README): `SELECT` on the
  diagnostic tables plus `UPDATE (status, summary)` on `incidents`. The stand's `oncall` role is a
  local convenience only.
- `runbook_get` validates against the catalog allowlist, rejects symlinks, resolves the path, and
  requires the result to stay inside `lesson_4/runbooks/`.
- `tests/test_read_only.py` proves table counts and runbook hashes are unchanged after every read
  tool, that write tools change only the target incident's authorized field, and that a
  column-scoped role cannot modify unrelated columns or tables.

## Assumptions and open items

- The stand is the only data source; no Prometheus/Tempo/incident API is involved.
- `deploy-bot` is intentionally log-only.
- `app_logs.level` has no CHECK constraint, so the accepted enum is `{INFO, WARN, ERROR}`: the first
  two are observed, `ERROR` is the conventional third level.
- Service names, endpoints, and runbook names are hard-coded allowlists. If the stand gains a new
  service, `models.py` must be updated and the tests extended.
- Incident writes are deliberately limited to acknowledgement and summary. There is no resolve,
  reopen, severity, owner, or title write tool, and no incident creation or deletion.
- `price_events` is out of scope by design.
