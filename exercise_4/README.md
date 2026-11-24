# oncall-mcp — read-only MCP server for the incident stand

A local [Model Context Protocol](https://modelcontextprotocol.io) server that gives an on-call
engineer diagnostic access to the `lesson_4` incident stand. It speaks **stdio only**, reads a
**PostgreSQL** database through a **single connection pool**, and exposes **seven domain tools**.

It cannot remediate anything: no writes, no deployments, no migrations, no shell, no arbitrary SQL,
no file access outside `lesson_4/runbooks/`.

- Data contract and every settled decision: [`docs/data-contract.md`](docs/data-contract.md)
- OpenCode example config: [`opencode.json`](opencode.json)

---

## Prerequisites

| Requirement | Version used |
| --- | --- |
| Python | **3.12.x exactly** (`requires-python = "==3.12.*"`, `.python-version` = `3.12`; tested on 3.12.16) |
| [uv](https://docs.astral.sh/uv/) | 0.13.7 |
| Docker + Compose | Compose v5.5.0 |
| OpenCode (client) | v2.3.2 |
| Pinned Python deps | `fastmcp==4.2.4`, `psycopg[binary]==3.3.9`, `psycopg-pool==3.3.3`, `pydantic-settings==2.16.3` |

`uv.lock` is committed and resolves for CPython 3.12 only; use `uv sync --locked` for a reproducible
install. `uv run` picks the 3.12 interpreter from `.python-version` automatically — no manual venv is
needed, and 3.13+ is rejected by the project metadata.

## Quick start

```bash
# 1. Start the observed system (Postgres on :5433, payments-api on :8080)
cd lesson_4
cp .env.example .env          # if you have not already
docker compose up -d --build

# 2. Seed history + generate ~5 minutes of live traffic (resets stand data)
docker compose run --rm simulator

# 3. Configure and run this server
cd ..
cp .env.example .env          # local stand defaults work as-is
uv sync --locked
uv run python -m oncall_mcp   # speaks MCP on stdin/stdout; logs on stderr
```

The server is not meant to be typed at interactively — a client drives it. Ctrl-C or closing stdin
shuts it down and closes the pool.

Both entry points below are equivalent; the project installs the console script, and the module form
needs no script:

```bash
uv run oncall-mcp            # console script from pyproject [project.scripts]
uv run python -m oncall_mcp  # module entry point, used by opencode.json
```

From outside this directory, add `--project` so uv runs in the right environment:

```bash
uv run --project /path/to/exercise_4 python -m oncall_mcp
```

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `STAND_DATABASE_URL` | **required** | PostgreSQL DSN for the stand, host side: `postgresql://…@localhost:5433/oncall` |
| `ONCALL_RUNBOOKS_DIR` | `<repo>/lesson_4/runbooks` | Directory holding `<service>.md` |
| `ONCALL_POOL_MIN_SIZE` | `1` | Pool minimum connections |
| `ONCALL_POOL_MAX_SIZE` | `5` | Pool maximum connections |
| `ONCALL_POOL_ACQUIRE_TIMEOUT` | `5.0` | Seconds to wait for a pooled connection |
| `ONCALL_DB_CONNECT_TIMEOUT` | `5.0` | Seconds to establish a connection |
| `ONCALL_STATEMENT_TIMEOUT_MS` | `5000` | Per-statement timeout, also used for idle-in-transaction |

`.env` is loaded from the project root and from the current directory (project root first).
`.env` is gitignored; `.env.example` holds placeholders only.

## OpenCode configuration

`opencode.json` in this directory is a working example for OpenCode v2.3.2:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "oncall": {
        "type": "local",
        "command": ["uv", "run", "python", "-m", "oncall_mcp"],
        "cwd": ".",
        "enabled": true,
        "timeout": 15000
      }
    }
  }
}
```

No credentials are in the file: the server reads `STAND_DATABASE_URL` from `.env`, which is
gitignored. Verify the connection from this directory:

```bash
opencode mcp list
# ✓ oncall  connected
```

## Tools

All seven tools are read-only (`readOnlyHint: true`, `destructiveHint: false`), return structured
content validated by Pydantic models, and never accept SQL, identifiers, or paths from the caller.

| Tool | Inputs (defaults) | Returns |
| --- | --- | --- |
| `incidents_search` | `service`, `severity?`, `status?`, `time_range?`, `limit=20` | `items[]`, `count`, `limit`, `has_more`, echoed filters |
| `incident_get` | `incident_id` (`INC-NNN`) | `item` |
| `deploys_recent` | `service`, `limit=5` | `items[]` newest first, `has_more` |
| `logs_query` | `service`, `time_range?`, `query?`, `level?`, `limit=50` | `items[]` newest first, `has_more` |
| `metrics_latency` | `service`, `endpoint`, `time_range?`, `bucket=1m` | per-bucket `request_count`, `avg_latency_ms`, `p95_latency_ms`, `cache_hit_pct` |
| `runbook_get` | `service` | `item` with Markdown `content`, `path`, `size_bytes` |
| `service_catalog_get` | `service` | `item` with `team`, `oncall`, `dependencies[]`, `description` |

### Service vocabulary (verbatim, no aliasing)

| Tool(s) | Accepted `service` values |
| --- | --- |
| `incidents_search`, `deploys_recent`, `runbook_get`, `service_catalog_get` | `payments`, `checkout`, `auth` |
| `logs_query` | `payments-api`, `checkout`, `auth`, `deploy-bot` |
| `metrics_latency` | `payments-api` |

`runbook_get("payments-api")` is `not_found`-by-design: the catalog and the log labels are different
vocabularies in the stand. `logs_query(service="payments")` is rejected as `invalid_arguments` rather
than silently returning nothing.

### Time windows

- Half-open `[start, end)`, RFC 3339 with an explicit offset, normalized to UTC. Naive datetimes are
  rejected.
- Omitted `time_range`: **last 5 minutes** for `logs_query` and `metrics_latency`; **no time filter**
  for `incidents_search`.
- Maximum span: **24 h** for logs and metrics, **7 days** for incidents.
- `bucket` ∈ `10s, 30s, 1m, 5m, 15m, 1h`; at most **500 buckets** per response.
- Limits: logs `1..200`, incidents `1..100`, deploys `1..50`.

### Errors

| Code | When |
| --- | --- |
| `invalid_arguments` | unknown service/endpoint/level/severity, bad range, too many buckets, oversized query |
| `not_found` | valid identifier that does not exist (`INC-999`, missing runbook) |
| `data_source_unavailable` | pool exhausted, database unreachable, runbook directory unreadable |
| `internal_error` | unexpected server fault (details only on stderr) |

A valid search with zero matches is **not** an error: it returns `items: []`, `count: 0`.

## Read-only database role

The stand's `oncall` role owns the schema, so tool annotations and `default_transaction_read_only`
are not the only line of defence. Create a dedicated role once, as an operator (the MCP server never
runs DDL):

```sql
CREATE ROLE oncall_ro LOGIN PASSWORD 'replace-me';
GRANT CONNECT ON DATABASE oncall TO oncall_ro;
GRANT USAGE ON SCHEMA public TO oncall_ro;
GRANT SELECT ON services, deploys, incidents, request_logs, app_logs TO oncall_ro;
```

Then point `STAND_DATABASE_URL` at `oncall_ro`. Additionally, every pooled connection is configured
with `default_transaction_read_only = on`, so an accidental write fails with
`ReadOnlySqlTransaction` even for a privileged role (see `tests/test_read_only.py`).

## What the simulator does

`cd lesson_4 && docker compose run --rm simulator` runs the `simulator` service (Compose profile
`sim`) and:

1. `TRUNCATE`s `request_logs`, `app_logs`, `price_events`, `incidents`, `deploys` and deletes
   `services`, then reseeds: 3 services, 5 deploys, 3 incidents, 4 000 baseline request logs,
   60 000 `price_events`, ~843 app log lines (including the deliberate prompt-injection line).
2. Drives 20 rps for 300 s against `payments-api` (80 % price endpoint, 20 % catalog endpoint).
3. Prints a per-minute latency summary **to stderr** and exits.

Postgres and the API keep running; data persists in the `pgdata` volume. Every run resets the data.
`docker compose down -v` removes the volume.

## Testing

```bash
uv run pytest          # 93 tests
uv run ruff check .    # lint
uv run ruff format --check .
```

The suite creates a throwaway database `oncall_mcp_test` on the stand's Postgres, applies
`lesson_4/db/init.sql`, and seeds deterministic fixtures. It **never** touches the stand's own
`oncall` database, so running tests cannot change observed data. If the stand is not running the
suite is skipped with an explicit reason.

Coverage includes: every tool's normal result and no-match path, missing IDs/files, invalid
service/level/status/severity, oversized ranges, limit overflow, unavailable database, SQL and
path-traversal attempts, prompt-injection text returned as data, hand-computed metric expectations
(counts, averages, p95, cache-hit %, empty buckets, bucket alignment, half-open boundaries),
read-only proofs (table counts and runbook hashes unchanged; writes rejected by the session),
single-pool reuse, and a real stdio subprocess test asserting stdout contains only JSON-RPC frames.

## Example on-call investigation

Prompt an OpenCode session in this directory:

> `/api/v1/orders/{order_id}/price` is slow. Use the oncall tools to find out why.

A faithful run of the tools (real output from this stand):

1. `incidents_search(service="payments", status="open")` → `INC-001`, severity `high`, opened
   `09:24:19Z`.
2. `deploys_recent(service="payments")` → `v1.5.0` "refactor: unified response cache" deployed
   `09:22:19Z` — two minutes before the incident.
3. `metrics_latency(service="payments-api", endpoint="/api/v1/orders/{order_id}/price", bucket="1m")`
   → request count flat at ~959/min while latency grows minute over minute:

   | minute | req | avg ms | p95 ms | cache-hit % |
   | --- | --- | --- | --- | --- |
   | 09:25 | 636 | 54.73 | 119.08 | 0.0 |
   | 09:26 | 959 | 100.52 | 135.40 | 0.0 |
   | 09:27 | 959 | 166.80 | 221.53 | 0.0 |
   | 09:28 | 958 | 272.27 | 381.83 | 0.0 |
   | 09:29 | 815 | 924.72 | 1605.30 | 0.0 |
   | 09:30 | 473 | 1011.46 | 1548.50 | 0.0 |

4. `metrics_latency(..., endpoint="/api/v1/catalog/items")` → the same minute buckets stay at
   **0.47–1.06 ms** for four minutes while the price path grows 20×, so the regression is specific
   to the price path, not the host or Postgres. (Catalog also degrades in the final minute, which is
   consistent with an in-process structure growing.)
5. `logs_query(service="payments-api", level="WARN", query="cache")` →
   `response cache grew to 8492 entries, hit_rate=0.0` — the cache grows and never hits.
6. `runbook_get(service="payments")` → step 2 is "check the response cache"; the runbook's decision
   table distinguishes a plateau (external dependency) from monotonic growth (something accumulating
   in-process), matching observation 3.
7. `service_catalog_get(service="payments")` → team `billing`, on-call `a.kuznetsov`, dependencies
   `postgres-main`, `checkout`, `fraud-scoring`.

Conclusion available from data alone: the v1.5.0 unified response cache is the regression; it is
in-process and accumulates, so latency grows monotonically while the cache hit rate stays at 0 %.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `configuration error: STAND_DATABASE_URL: …` on startup (exit code 2) | Missing/invalid DSN. Set it in `.env` or the environment. Values are never printed. |
| `data_source_unavailable: cannot connect …` | Stand not running, or wrong port. `docker compose up -d --build` in `lesson_4`; Postgres is published on `5433`. |
| `invalid_arguments: unknown service …` | Use the vocabulary table above; `payments` ≠ `payments-api`. |
| Metrics are all zeros with `null` stats | The window has no traffic (the simulator finished > 5 min ago, or `time_range` is wrong). Re-run the simulator. |
| `opencode mcp list` shows `failed` | Run `uv run python -m oncall_mcp` manually in this directory and read stderr. |
| Inspector: `Failed to spawn: incident-mcp` | There is no `incident-mcp` executable. The server is named **oncall**: use `uv run oncall-mcp` or `uv run python -m oncall_mcp`, and start the Inspector from this directory (or add `--project`). |
| Inspector: `Failed to spawn: oncall-mcp` from the repo root | `uv` found no project there. `cd exercise_4` first, or use `uv run --project exercise_4 oncall-mcp`. |
| Inspector works but every call fails with `configuration error` | The Inspector process has no `.env`/`STAND_DATABASE_URL` in scope: add `-e STAND_DATABASE_URL=postgresql://…` to the Inspector command. |
| Tests skipped: "Postgres is not reachable" | Start the stand; tests need it for the throwaway database. |

## Verification performed

| Check | Command | Result |
| --- | --- | --- |
| Interpreter | `uv run python --version` | CPython 3.12.16 |
| Automated suite | `uv run pytest` | 93 passed |
| Lint / format | `uv run ruff check .`, `ruff format --check .` | clean |
| Locked install | `uv sync --locked` | OK |
| MCP handshake + `tools/list` | `npx @modelcontextprotocol/inspector --cli uv run python -m oncall_mcp --method tools/list` | 7 tools, `readOnlyHint: true` |
| MCP `tools/call` | same, `--method tools/call --tool-name incidents_search --tool-arg service=payments` | `INC-001`, structured content |
| OpenCode connection | `opencode mcp list` | `✓ oncall connected` |
| End-to-end workflow | OpenCode session using the seven tools | cache regression identified from data |

## Deviations from the brief
1. `metrics_latency` takes an extra required `service` argument (the stand has a `service` column and
   a per-endpoint query alone cannot guarantee isolation).
2. `time_range` is optional on `logs_query`/`metrics_latency` (default: last 5 minutes) and on
   `incidents_search` (default: no time filter), and a bounded `limit` was added to
   `incidents_search`.
3. `cache_hit_pct` is nullable: the cache-hit source column is `request_logs.cache_hit`, which is
   NULL for catalog requests, and the percentage is `100 * count(*) FILTER (WHERE cache_hit) /
   count(cache_hit)`.
4. The read-only role is operator-provisioned (documented above), because tool calls must not run
   DDL; session-level `default_transaction_read_only` is enforced by the server itself.
5. `deploy-bot` exists only in logs and has no catalog card; `runbook_get` accepts only catalog names.


## Exercise Result

### Metrics before
latency /api/v1/orders/{order_id}/price по минутам:  
minute                   req    avg_ms    p95_ms    hit%  
2026-11-23 18:47         398      45.2      60.0     0.0  
2026-11-23 18:48         958      82.8     118.2     0.0  
2026-11-23 18:49         959     146.7     197.9     0.0  
2026-11-23 18:50         959     222.3     306.7     0.0  
2026-11-23 18:51         925     583.1    1227.0     0.0  
2026-11-23 18:52         601     964.9    1418.4     0.0  

### The Agent's hypothesis

Краткий вывод
Деградация payments — это не сеть, не общая нехватка CPU и не внешняя зависимость. Данные показывают регрессию кэша ответов, приехавшую с деплоем v1.5.0:
- сразу после деплоя v1.5.0 (refactor: unified response cache) кэш перестал попадать: cache_hit_pct = 0 на всех запросах цены;
- кэш растёт линейно и не вытесняется: ~500 записей за 31 с, ровно по числу запросов;
- каждый промах уходит в медленный путь, который читает растущий append-only журнал price_events; стоимость промаха увеличивается со временем, поэтому latency растёт монотонно, а не выходит на плато.

### Dialog Log
file Dialog.md

### Fix
file fix-response-cache.patch

### Metrics after

latency /api/v1/orders/{order_id}/price по минутам:  
minute                   req    avg_ms    p95_ms    hit%  
2026-11-23 19:21         405      19.9      62.4    46.2  
2026-11-23 19:22         960      15.5      56.5    70.2  
2026-11-23 19:23         959      15.8      76.3    77.1  
2026-11-23 19:24         961      19.7      90.4    76.6  
2026-11-23 19:25         959      27.6     104.6    69.0  
2026-11-23 19:26         556      41.2     109.5    56.8  

