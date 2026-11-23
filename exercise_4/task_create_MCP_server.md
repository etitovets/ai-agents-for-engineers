## Task for create local MCP server

1. Inspect `lesson_4/README.md`, `lesson_4/compose.yaml` / `docker-compose.yml` (whichever exists), the simulator entry point, repository layout, schema/migrations, sample data, and `lesson_4/runbooks/`.
2. Identify the authoritative data source and exact fields/types for incidents, deployments, application logs (`app_logs`), request logs (`request_logs`), runbooks, and the service catalog. Do not infer columns or invent tables from this brief.
3. Determine whether `endpoint` identifies a service uniquely. If not, surface the ambiguity in `metrics_latency(endpoint, ...)` and obtain an explicit resolution before changing the public signature or returning cross-service data.
4. Record discoveries and decisions in `README.md` (or `docs/data-contract.md`) before implementing SQL.
5. If essential files, data sources, or fields are missing, report the specific blocker. Do not replace real data with hard-coded successful responses.

