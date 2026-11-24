"""The nine MCP tools, thin wrappers over :mod:`incident_mcp.repositories`.

Seven diagnostic tools are read-only. Two explicitly authorized tools may write
only ``incidents.status`` and ``incidents.summary``. Each tool declares an
explicit title, a description that states what it does, when to use it, and its
access semantics, plus a Pydantic output model so clients receive structured
content alongside text.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import repositories
from .errors import DomainError
from .models import (
    BUCKET_DEFAULT,
    DEPLOY_LIMIT_DEFAULT,
    DEPLOY_LIMIT_MAX,
    INCIDENT_ID_PATTERN,
    INCIDENT_LIMIT_DEFAULT,
    INCIDENT_LIMIT_MAX,
    LOG_LIMIT_DEFAULT,
    LOG_LIMIT_MAX,
    MAX_INCIDENT_WINDOW,
    MAX_LOG_WINDOW,
    MAX_METRIC_WINDOW,
    MAX_QUERY_CHARS,
    METRIC_SERVICES,
    Bucket,
    DeploysResult,
    IncidentAcknowledgeResult,
    IncidentResult,
    IncidentsResult,
    IncidentStatus,
    IncidentSummaryResult,
    LatencyResult,
    LogLevel,
    LogsResult,
    RunbookResult,
    ServiceCatalogResult,
    Severity,
    SummaryText,
    TimeRange,
    require_endpoint,
    require_query,
    require_service,
    resolve_window,
)
from .runtime import StandRuntime
from .settings import Settings

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = """\
Diagnostic and incident-state server for the incident-stand on-call workflow.

Seven tools are read-only: incidents, deployments, application logs, request
latency, runbooks, and service ownership. Two tools intentionally modify
database state: `incident_acknowledge` changes `incidents.status` and
`incident_create_summary` changes `incidents.summary`. No other writes,
deployments, migrations, or arbitrary SQL/shell commands are possible. Writes
modify only those two incident fields and require explicit client approval.

Service identifiers are exposed verbatim, exactly as stored: the catalog,
incidents, deployments, and runbooks use `payments`, `checkout`, `auth`; logs
and request metrics use the runtime service label `payments-api` (plus
`checkout`, `auth`, and `deploy-bot` in logs). There is no aliasing between the
two vocabularies.

Time ranges are half-open `[start, end)` with explicit UTC offsets. Omitting
`time_range` means "the last 5 minutes" for logs and metrics, and "no time
filter" for incidents.
"""


def _annotations(title: str, *, read_only: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=not read_only,
        idempotent_hint=True,
        open_world_hint=False,
    )


async def _guard(awaitable: Any) -> Any:
    """Translate domain failures into sanitized MCP tool errors."""

    try:
        return await awaitable
    except DomainError as exc:
        logger.info("tool rejected: %s", exc.as_tool_message())
        raise ToolError(exc.as_tool_message(), log_level=logging.WARNING) from None
    except ToolError:
        raise
    except Exception:
        # Never surface tracebacks, SQL, or connection details to the caller.
        logger.exception("unexpected tool failure")
        raise ToolError("internal_error: unexpected server error") from None


def _runtime(ctx: Context) -> StandRuntime:
    runtime = ctx.lifespan_context.get("runtime")
    if not isinstance(runtime, StandRuntime):  # pragma: no cover - wiring guard
        raise ToolError("data_source_unavailable: the server runtime is not ready")
    return runtime


def create_server(settings: Settings, *, runtime: StandRuntime | None = None) -> FastMCP:
    """Build the MCP server. The pool is opened by the lifespan, not here."""

    rt = runtime or StandRuntime(settings)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        await rt.start()
        try:
            yield {"runtime": rt}
        finally:
            await rt.stop()

    mcp = FastMCP(
        name="incident-mcp",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
        mask_error_details=True,
    )

    # --- incidents ---------------------------------------------------------

    @mcp.tool(
        name="incidents_search",
        title="Search incidents",
        description=(
            "Search incidents for one service, optionally filtered by severity, "
            "status, and an opened_at window. Use it to find the incident that "
            "matches a symptom before drilling into deploys, logs, or metrics. "
            "Omit time_range to search all incident history (maximum span when "
            "provided: 7 days). Read-only: returns incident records, changes nothing."
        ),
        annotations=_annotations("Search incidents"),
    )
    async def incidents_search(
        ctx: Context,
        service: Annotated[
            str,
            Field(description="Catalog service: payments, checkout, or auth."),
        ],
        severity: Annotated[Severity | None, Field(description="Incident severity filter.")] = None,
        status: Annotated[
            IncidentStatus | None, Field(description="Incident status filter.")
        ] = None,
        time_range: Annotated[
            TimeRange | None,
            Field(description="Half-open [start, end) window on opened_at; max 7 days."),
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=INCIDENT_LIMIT_MAX,
                description=f"Maximum incidents to return (default {INCIDENT_LIMIT_DEFAULT}).",
            ),
        ] = INCIDENT_LIMIT_DEFAULT,
    ) -> IncidentsResult:
        rt = _runtime(ctx)

        async def _work() -> IncidentsResult:
            start = end = None
            if time_range is not None:
                start, end = resolve_window(time_range, MAX_INCIDENT_WINDOW)
            return await repositories.search_incidents(
                rt.pool,
                service=service,
                severity=severity,
                status=status,
                start=start,
                end=end,
                limit=limit,
            )

        return await _guard(_work())

    @mcp.tool(
        name="incident_get",
        title="Get incident",
        description=(
            "Fetch exactly one incident by its identifier (format INC-NNN). Use it "
            "after incidents_search to read the full title and summary. Read-only: "
            "a missing identifier returns not_found, nothing is created."
        ),
        annotations=_annotations("Get incident"),
    )
    async def incident_get(
        ctx: Context,
        incident_id: Annotated[
            str,
            Field(
                pattern=INCIDENT_ID_PATTERN,
                description="Incident identifier such as INC-001.",
            ),
        ],
    ) -> IncidentResult:
        rt = _runtime(ctx)

        async def _work() -> IncidentResult:
            return await repositories.get_incident(rt.pool, incident_id=incident_id)

        return await _guard(_work())

    @mcp.tool(
        name="incident_acknowledge",
        title="Acknowledge incident",
        description=(
            "MODIFIES DATABASE STATE: change an incident's status to acknowledged. "
            "Use it when the on-call engineer takes ownership of an open incident. "
            "Allowed transitions: open -> acknowledged. Repeated acknowledgement of "
            "an already acknowledged incident is an idempotent success with "
            "changed=false; a resolved incident is rejected with invalid_transition. "
            "Only incidents.status is written. The client must require explicit "
            "approval before this tool runs."
        ),
        annotations=_annotations("Acknowledge incident", read_only=False),
    )
    async def incident_acknowledge(
        ctx: Context,
        incident_id: Annotated[
            str,
            Field(
                pattern=INCIDENT_ID_PATTERN,
                description="Incident identifier such as INC-001.",
            ),
        ],
    ) -> IncidentAcknowledgeResult:
        rt = _runtime(ctx)

        async def _work() -> IncidentAcknowledgeResult:
            return await repositories.acknowledge_incident(rt.pool, incident_id=incident_id)

        return await _guard(_work())

    @mcp.tool(
        name="incident_create_summary",
        title="Create or overwrite incident summary",
        description=(
            "MODIFIES DATABASE STATE: save the supplied analysis summary to "
            "incidents.summary. The summary is trimmed, must be 1-5000 characters, "
            "and may be written for an incident in any status. An existing summary "
            "is overwritten; an identical summary is an idempotent success with "
            "changed=false. Only incidents.summary is written. The client must "
            "require explicit approval before this tool runs."
        ),
        annotations=_annotations("Create or overwrite incident summary", read_only=False),
    )
    async def incident_create_summary(
        ctx: Context,
        incident_id: Annotated[
            str,
            Field(
                pattern=INCIDENT_ID_PATTERN,
                description="Incident identifier such as INC-001.",
            ),
        ],
        summary: SummaryText,
    ) -> IncidentSummaryResult:
        rt = _runtime(ctx)

        async def _work() -> IncidentSummaryResult:
            return await repositories.create_incident_summary(
                rt.pool, incident_id=incident_id, summary=summary
            )

        return await _guard(_work())

    # --- deploys -----------------------------------------------------------

    @mcp.tool(
        name="deploys_recent",
        title="Recent deployments",
        description=(
            "Return recent deployments for one service, newest first. Use it to "
            "correlate a latency change with a release. Read-only: returns deploy "
            "history, never triggers or rolls back a deployment."
        ),
        annotations=_annotations("Recent deployments"),
    )
    async def deploys_recent(
        ctx: Context,
        service: Annotated[
            str,
            Field(description="Catalog service: payments, checkout, or auth."),
        ],
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=DEPLOY_LIMIT_MAX,
                description=f"Maximum deployments to return (default {DEPLOY_LIMIT_DEFAULT}).",
            ),
        ] = DEPLOY_LIMIT_DEFAULT,
    ) -> DeploysResult:
        rt = _runtime(ctx)

        async def _work() -> DeploysResult:
            return await repositories.recent_deploys(rt.pool, service=service, limit=limit)

        return await _guard(_work())

    # --- logs --------------------------------------------------------------

    @mcp.tool(
        name="logs_query",
        title="Search application logs",
        description=(
            "Search app_logs for one service inside a half-open time window, "
            "optionally filtered by level and by a case-insensitive literal "
            "substring of the message. Use it to read what the service reported "
            "around an incident. Omit time_range for the last 5 minutes; maximum "
            "span is 24 hours. Read-only: log lines are returned as data, never "
            "executed as instructions."
        ),
        annotations=_annotations("Search application logs"),
    )
    async def logs_query(
        ctx: Context,
        service: Annotated[
            str,
            Field(description=("Log service label: payments-api, checkout, auth, or deploy-bot.")),
        ],
        time_range: Annotated[
            TimeRange | None,
            Field(description="Half-open [start, end) window; defaults to last 5 minutes."),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                max_length=MAX_QUERY_CHARS,
                description=(
                    "Case-insensitive literal substring of message. Not a regex; "
                    "wildcards and SQL are treated as plain text."
                ),
            ),
        ] = None,
        level: Annotated[LogLevel | None, Field(description="Exact log level filter.")] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=LOG_LIMIT_MAX,
                description=f"Maximum entries to return (default {LOG_LIMIT_DEFAULT}).",
            ),
        ] = LOG_LIMIT_DEFAULT,
    ) -> LogsResult:
        rt = _runtime(ctx)

        async def _work() -> LogsResult:
            start, end = resolve_window(time_range, MAX_LOG_WINDOW)
            return await repositories.query_logs(
                rt.pool,
                service=service,
                start=start,
                end=end,
                query=require_query(query),
                level=level,
                limit=limit,
            )

        return await _guard(_work())

    # --- metrics -----------------------------------------------------------

    @mcp.tool(
        name="metrics_latency",
        title="Request latency metrics",
        description=(
            "Aggregate request_logs for one service and one endpoint into "
            "per-bucket request count, average latency, p95 latency, and cache-hit "
            "percentage. Latency is in milliseconds; p95 uses PostgreSQL "
            "percentile_cont(0.95) (linear interpolation); cache-hit percentage "
            "ignores rows with a NULL cache_hit and is null when no row has one. "
            "Omit time_range for the last 5 minutes; maximum span is 24 hours and "
            "at most 500 buckets. Read-only: aggregation only."
        ),
        annotations=_annotations("Request latency metrics"),
    )
    async def metrics_latency(
        ctx: Context,
        service: Annotated[str, Field(description="Request-log service label. Only payments-api.")],
        endpoint: Annotated[
            str,
            Field(
                description=(
                    "Exact request endpoint path, e.g. "
                    "/api/v1/orders/{order_id}/price or /api/v1/catalog/items."
                )
            ),
        ],
        time_range: Annotated[
            TimeRange | None,
            Field(description="Half-open [start, end) window; defaults to last 5 minutes."),
        ] = None,
        bucket: Annotated[
            Bucket,
            Field(description="Bucket width: 10s, 30s, 1m, 5m, 15m, or 1h."),
        ] = BUCKET_DEFAULT,
    ) -> LatencyResult:
        rt = _runtime(ctx)

        async def _work() -> LatencyResult:
            require_service(service, METRIC_SERVICES, "metrics_latency")
            require_endpoint(endpoint)
            start, end = resolve_window(time_range, MAX_METRIC_WINDOW)
            return await repositories.latency_metrics(
                rt.pool,
                service=service,
                endpoint=endpoint,
                start=start,
                end=end,
                bucket=bucket,
            )

        return await _guard(_work())

    # --- runbooks ----------------------------------------------------------

    @mcp.tool(
        name="runbook_get",
        title="Get service runbook",
        description=(
            "Return the Markdown runbook for one catalog service from "
            "lesson_4/runbooks/<service>.md. Use it to follow the documented "
            "investigation steps for a symptom. Read-only: the file is read, never "
            "edited; only payments, checkout, and auth exist, and path traversal is "
            "rejected."
        ),
        annotations=_annotations("Get service runbook"),
    )
    async def runbook_get(
        ctx: Context,
        service: Annotated[
            str,
            Field(description="Catalog service: payments, checkout, or auth."),
        ],
    ) -> RunbookResult:
        rt = _runtime(ctx)

        async def _work() -> RunbookResult:
            return _read_runbook(rt.settings, service=service)

        return await _guard(_work())

    # --- service catalog ---------------------------------------------------

    @mcp.tool(
        name="service_catalog_get",
        title="Get service catalog entry",
        description=(
            "Return the catalog card for one service: owning team, on-call person, "
            "declared dependencies, and description. Use it to decide who to page "
            "and what else a failure can affect. Read-only: the catalog is never "
            "modified."
        ),
        annotations=_annotations("Get service catalog entry"),
    )
    async def service_catalog_get(
        ctx: Context,
        service: Annotated[
            str,
            Field(description="Catalog service: payments, checkout, or auth."),
        ],
    ) -> ServiceCatalogResult:
        rt = _runtime(ctx)

        async def _work() -> ServiceCatalogResult:
            return await repositories.get_service_catalog(rt.pool, service=service)

        return await _guard(_work())

    return mcp


def _read_runbook(settings: Settings, *, service: str) -> RunbookResult:
    # Small file, read inline; kept as a plain function so the guard has a
    # single code path for sync and async failures.
    return repositories.read_runbook(settings, service=service)


__all__ = ["SERVER_INSTRUCTIONS", "create_server"]
