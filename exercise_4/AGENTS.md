# Create MCP-cerver for on-call engineer.

## Objective and scope
Build a local MCP server with exactly nine tools:
seven read-only diagnostic tools and two explicitly
authorized write tools.

Only the following database mutations are permitted:
- `incident_acknowledge`: update the incident status.
- `incident_create_summary`: update `incidents.summary`.

All other database mutations, deployments, remediation,
runbook modifications, and arbitrary SQL or shell
execution are prohibited.

The source of truth for the observed service is
`./lesson_4/README.md`, together with the actual
database schema, Docker Compose configuration,
and runbooks.

Inspect these sources before implementation.
Do not invent schema fields, statuses, or data sources.
If required database fields are missing, report
the blocker instead of modifying the schema.

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
        "command": [
          "uv",
          "run",
          "python",
          "-m",
          "oncall_mcp"
        ],
        "cwd": "."
      }
    }
  },
  "permissions": [
    {
      "action": "oncall_incident_acknowledge",
      "resource": "*",
      "effect": "ask"
    },
    {
      "action": "oncall_incident_create_summary",
      "resource": "*",
      "effect": "ask"
    }
  ]
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

### Database permissions

- Use exactly one PostgreSQL connection pool per process.
- Use a database role with SELECT permissions on required
  tables and narrowly scoped UPDATE permissions for
  authorized incident fields only.
- Execute all seven read tools in read-only transactions.
- Execute write operations in explicit transactions.
- Verify that unrelated tables and columns cannot be
  modified using the configured database role.

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

## Public read tools - exact requested names

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


## Public write tools - exact requested names

Implement exactly two additional state-changing tools.
Together with the seven read tools, the MCP server must
expose exactly nine tools.

| Tool | Input signature | Responsibility |
| --- | --- | --- |
| `incident_acknowledge` | `(incident_id)` | Change the incident status to `acknowledged`, subject to documented and validated status transitions. |
| `incident_create_summary` | `(incident_id, summary)` | Save the provided analysis summary to `incidents.summary`, following a documented overwrite policy. |

Requirements:
- Both write tools MUST have explicit titles, descriptions, validated input schemas, and structured output schemas.
- Both tools MUST explicitly declare `readOnlyHint: false`.
- Both tools MUST explicitly declare `destructiveHint: true`.
- Tool descriptions MUST state that the operation modifies database state.
- Validate all inputs using Pydantic.
- Use parameterized SQL and database transactions.
- Restrict writes to the explicitly permitted incident fields.
- Define behavior for missing incidents, invalid transitions, repeated calls, and concurrent updates.
- Return structured results indicating the outcome.
- Never execute arbitrary SQL provided by the client.
- Add automated tests verifying the intended changes and the absence of unintended modifications.
- Write-tool invocations MUST require explicit user approval in the OpenCode client configuration.
- MCP annotations alone MUST NOT be treated as authorization or access-control mechanisms.

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

## Security invariants

- Seven read tools MUST declare `readOnlyHint: true`.
- Two write tools MUST declare `readOnlyHint: false` and `destructiveHint: true`.
- Read tools MUST NOT modify database state.
- Write tools MUST modify only explicitly authorized incident fields.
- Tools MUST NOT start the simulator, run migrations, or perform unrelated state changes.
- All SQL queries executed by any tool MUST use parameterized statements.
- Never construct SQL queries by concatenating untrusted input.
- Restrict dynamic SQL identifiers to explicit allowlists.

## Architecture and implementation order

Implement nine thin MCP tools:
seven read-only and two state-changing.

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
- Verify exactly nine tools through MCP `tools/list`.
- Test all tools through MCP Inspector.
- Run automated tests using `uv run pytest`.
- Use deterministic test fixtures.
- Verify that all read tools preserve database state.
- Verify that write tools modify only authorized fields.
- Test invalid inputs, nonexistent incidents, repeated
  writes, concurrent updates, and database failures.
- Verify PostgreSQL pool reuse and graceful shutdown.
- Verify stdout contains only MCP protocol messages.
- Test connection and tool execution from OpenCode.
- Test metrics against known expected values.

## Definition of done
- All nine tools are discoverable and callable from OpenCode.
- All tools operate on real observed-service data.
- No stub implementations remain.
- All read/write restrictions are enforced and tested.
- Automated tests pass.
- The project includes reproducible setup and launch
  commands, .env.example, and README documentation.
- The agent reports changed files, test results,
  and unresolved assumptions.

## Open decisions - do not silently guess

- Real repository/database schema and the data source for the service catalog.
- Precise incident status/severity and log-level values; search semantics; defaults and limits.
- Whether `metrics_latency` needs a `service` argument to prevent mixing metrics from different services.
- P95 calculation convention and cache-hit source field.
- Simulator initialization and lifecycle, including fixture timing and persistence.

