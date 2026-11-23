# Create MCP-cerver for on-call engineer.

## Objective and scope

Build a local, read-only Model Context Protocol (MCP) server to support an on-call engineer investigating incidents in the service described by ./lesson_4/README.md. The intended client is OpenCode.

The server provides diagnostic data only. It must not remediate incidents, trigger deployments, edit runbooks, change the observed service, write to its databases, or execute user-supplied SQL or shell commands. Do not add tools beyond the seven listed below unless approved.

## Stack and runtime
- Python 3.12, `uv`, Pydantic, and the standalone `fastmcp` package (`from fastmcp import FastMCP`). Do not silently switch to `mcp.server.fastmcp.FastMCP`, which is a different import path.
- Maintain a `pyproject.toml` and a committed `uv.lock`; pin and document dependency versions.
- The server must be startable from a documented, reproducible command such as `uv run python -m oncall_mcp` (create a suitable entry point if this command is chosen).
- Add a working OpenCode local MCP configuration example for the actual entry point and working directory, following the version of OpenCode used in the project. For example, if the selected module entry point is implemented and installed via `uv`, a current OpenCode configuration is:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "oncall": {
        "type": "local",
        "command": ["uv", "run", "python", "-m", "oncall_mcp"],
        "cwd": "."
      }
    }
  }
}
```

Confirm the config against the installed OpenCode version and the actual project entry point.
### Database connection and configuration
* Database engine: PostgreSQL.
* Database connection string (DATABASE_URL) must be loaded from environment variables, with .env support for local development.
* Never hardcode database credentials, connection strings, or other secrets in source code.
* Include .env.example with placeholder values. Add .env to .gitignore.
* Create one PostgreSQL connection pool per MCP server process during application initialization.
* Reuse the same pool across all tool calls. Do not create a new pool or establish a standalone database connection for each tool invocation.
* Acquire connections from the pool for individual database operations and release them back to the pool after use.
* Close the pool gracefully during server shutdown.
* Configure pool minimum and maximum sizes, acquisition timeout, and connection timeout through settings with documented defaults.
* Use a PostgreSQL role with read-only permissions.

### STDIO and secrets safety
* The MCP server must use stdio transport only.
* stdout is reserved exclusively for MCP protocol messages.
* All application logs, diagnostics, and debug output must go to stderr or a configured log file.
* Never use print() or configure logging handlers to write to stdout.
* Never log or expose database passwords, full connection strings, tokens, or other secrets, including in exception messages.
### Required verification

* Verify that the PostgreSQL connection pool is initialized exactly once per server process.
* Verify that multiple tool calls reuse the same pool.
* Verify that acquired connections are released and the pool closes during shutdown.
* Verify that stdout contains only valid MCP protocol messages.
* Verify that missing or invalid DATABASE_URL produces a controlled startup error without exposing secrets.
* Verify that .env is not committed to version control.

## Public tools - exact requested names

Implement the following seven tools. Tool names must remain unique and domain-prefixed. Each must have an explicit title, a description explaining what it does, when to use it, and its read-only restrictions, plus validated input and structured output schemas.

| Tool | Input signature | Responsibility |
| --- | --- | --- |
| `incidents_search` | `(service, severity?, status?, time_range?)` | Search incidents using validated filters. |
| `incident_get` | `(incident_id)` | Fetch one incident by ID. |
| `deploys_recent` | `(service, limit?)` | Return recent deployments for a service in descending chronological order. |
| `logs_query` | `(service, time_range, query?, level?)` | Search application logs for a service. |
| `metrics_latency` | `(endpoint, time_range, bucket?)` | Return per-time-bucket request count, average latency, p95 latency, and cache-hit percentage from request logs. |
| `runbook_get` | `(service)` | Read `lesson_4/runbooks/<service>.md`. |
| `service_catalog_get` | `(service)` | Return the service's team, on-call ownership, and dependencies. |

## Input contracts to settle and document

- Represent time ranges consistently with a Pydantic model containing timezone-aware `start` and `end`; normalize to UTC, require `start < end`, and treat intervals as `[start, end)` unless the real service explicitly requires another convention. Define a bounded maximum range based on data size and observed use cases.
- Define the precise meaning and supported values of `severity`, `status`, and `level` from actual source data; use enums where possible. Specify what omitted filters mean.
- Validate `service` against real known services. Verify that any requested `endpoint` belongs to the intended scope. `incident_id` must match the actual identifier type.
- Define defaults and maximums for `limit` and `bucket` after inspecting actual data and performance. Do not permit unbounded log/incident queries or unlimited response size. If existing signatures cannot express paging, define and document a safe bounded result policy.
- Define `query` semantics explicitly (literal substring, full-text, or a supported search syntax); avoid implicit regex or arbitrary SQL.
- `runbook_get` must map an allowed service name to exactly one file under the runbooks directory. Resolve paths and reject traversal, absolute paths, and symlink escapes.

## Output contracts to settle and document

- Return consistent JSON-serializable, typed structures (prefer Pydantic output models) instead of prose-only text. Preserve source field names/meanings or document transformations.
- Distinguish `not found`, `invalid arguments`, `data source unavailable`, and `no matching records`. Return an empty list for a valid search with zero matches; avoid inventing records.
- Include timestamps in RFC 3339 / ISO 8601 with explicit timezone, and document the ordering of list results.
- Define stable field names for each incident/deployment/log record and for each tool's return value using the actual observed schema.
- For `metrics_latency`, return bucket start/end, request count, average latency, p95 latency, and cache-hit percentage. Document the latency unit, p95 percentile definition (exact/approximate and interpolation method), cache-hit source field, treatment of missing values, bucket alignment, and treatment of empty buckets. Test all of these against known fixtures.
- Limit response size and do not leak credentials, connection strings, or sensitive log contents beyond the scope of the on-call use case.

## Read-only and security invariants

- All database operations must be parameterized and restricted to allowlisted tables/fields. Do not expose generic `query(sql)`, arbitrary file access, code execution, or shell execution.
- Enforce read-only access at the data-source level where supported (for example, a read-only DB connection/account); don't rely solely on tool descriptions or MCP annotations.
- Declare appropriate MCP tool annotations, including `readOnlyHint: true`; understand that annotations are hints, not a security boundary.
- Apply bounds to time ranges, result counts, and expensive aggregations. Make database connectivity and paths configurable without embedding secrets in the repository.
- Tool calls must never start the simulator, mutate sample data, run migrations, or change the observed service.
- Sanitize error messages: useful to the caller, but without revealing secrets or stack traces. Use stderr for developer diagnostics.

## Architecture and implementation order

1. Document the discovered data model and make a tool-to-source mapping.
2. Implement validated domain-specific repository/query functions separately from MCP decorators. Keep SQL out of tool input parameters.
3. Implement typed input/output models and domain error mapping.
4. Implement seven thin MCP tools calling the repository functions.
5. Add test fixtures and tests, then a reproducible launch command and OpenCode configuration.
6. Keep the new MCP implementation isolated from `lesson_4` unless a narrowly documented integration change is necessary. Avoid unrelated refactoring.

Suggested package layout (adapt to existing repository conventions):

```text
pyproject.toml
uv.lock
src/oncall_mcp/
  __init__.py
  __main__.py
  server.py
  models.py
  repositories.py
  settings.py
tests/
  test_tools.py
  test_metrics.py
  test_read_only.py
README.md
```

## Verification and tests

- Establish exactly how the observed service is initialized. The existing instruction is `cd lesson_4 && docker compose run --rm simulator`, but inspect Compose and document what that command actually starts, writes, and leaves running before relying on it.
- Add automated tests with deterministic fixtures. Cover every tool's normal result, no matches, missing IDs/files, invalid service/level/status/severity, invalid or oversized time range, query limits, unavailable data source, and injection/path-traversal attempts.
- For `metrics_latency`, test a known sequence of requests with explicit expected count, average, p95, cache-hit percentage, bucket boundaries, and empty-bucket behavior.
- Prove the read-only invariant by verifying that MCP tool calls do not alter the observed database, runbooks, or other monitored files.
- Test MCP initialization, `tools/list` (all seven tools with correct names, descriptions and schemas), and `tools/call` via MCP Inspector, including CLI tests suitable for a coding agent.
- Test the actual OpenCode connection and an end-to-end on-call diagnostic workflow, not just Inspector.
- Use `uv run pytest` for automated tests; document any additional lint/type-check commands actually configured in the project.

## Definition of done

- Seven requested tools are discoverable and callable over MCP stdio from OpenCode.
- Every tool uses actual observed-service data; no fabricated success paths or stub output remain.
- Inputs and outputs are documented and validated; error cases are predictable.
- Metrics aggregation matches documented formulas and deterministic test expectations.
- Tool calls are read-only, bounded, and safe against SQL/file path injection.
- A fresh checkout can install dependencies, initialize the required test data, run tests, start the MCP server, and connect it to OpenCode using documented commands.
- The implementation includes a README with prerequisites, configuration, startup, testing, troubleshooting, and a brief example of an on-call investigation.
- Deliver a final summary of files changed, commands run, test results, unresolved assumptions, and any deviations from this brief.

## Open decisions - do not silently guess

- Real repository/database schema and the data source for the service catalog.
- Precise incident status/severity and log-level values; search semantics; defaults and limits.
- Whether `metrics_latency` needs a `service` argument to prevent mixing metrics from different services.
- P95 calculation convention and cache-hit source field.
- Simulator initialization and lifecycle, including fixture timing and persistence.

