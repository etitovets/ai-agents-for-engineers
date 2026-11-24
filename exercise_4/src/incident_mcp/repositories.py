"""Domain queries over the incident stand.

All SQL lives here, uses server-side parameters, and touches only the
allowlisted tables: ``incidents``, ``deploys``, ``app_logs``, ``request_logs``,
``services``. No function accepts SQL, identifiers, or table names from the
caller.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from .errors import DataSourceUnavailable, InvalidArguments, InvalidTransition, NotFound
from .models import (
    CATALOG_SERVICES,
    LOG_SERVICES,
    MAX_MESSAGE_CHARS,
    MAX_RUNBOOK_BYTES,
    METRIC_SERVICES,
    Bucket,
    Deploy,
    DeploysResult,
    Incident,
    IncidentAcknowledgeResult,
    IncidentResult,
    IncidentsResult,
    IncidentStatus,
    IncidentSummaryResult,
    LatencyBucket,
    LatencyResult,
    LogEntry,
    LogLevel,
    LogsResult,
    Runbook,
    RunbookResult,
    ServiceCatalogEntry,
    ServiceCatalogResult,
    Severity,
    TimeRangeOut,
    bucket_count,
    require_endpoint,
    require_service,
    require_summary,
)
from .settings import Settings

logger = logging.getLogger(__name__)


# --- Low-level helpers ------------------------------------------------------


async def _fetch_all(
    pool: AsyncConnectionPool, query: sql.Composable | str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchall()
    except PoolTimeout:
        logger.warning("timed out acquiring a database connection")
        raise DataSourceUnavailable(
            "the database connection pool is exhausted; retry shortly"
        ) from None
    except psycopg.Error as exc:
        # Full detail to stderr; the caller gets a static message.
        logger.warning("database query failed: %s", exc)
        raise DataSourceUnavailable("the incident stand database query failed") from None


async def _fetch_one(
    pool: AsyncConnectionPool, query: sql.Composable | str, params: dict[str, Any]
) -> dict[str, Any] | None:
    rows = await _fetch_all(pool, query, params)
    return rows[0] if rows else None


async def _in_write_transaction[T](
    pool: AsyncConnectionPool,
    operation: Callable[[AsyncConnection[dict[str, Any]]], Awaitable[T]],
) -> T:
    """Run one authorized write in an explicit read-write transaction.

    Pooled connections keep ``default_transaction_read_only = on``; this helper
    is the only place that switches a single transaction to read-write. The
    database role remains the hard boundary: it may update only
    ``incidents.status`` and ``incidents.summary``.
    """

    try:
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION READ WRITE")
            return await operation(conn)
    except PoolTimeout:
        logger.warning("timed out acquiring a database connection for a write")
        raise DataSourceUnavailable(
            "the database connection pool is exhausted; retry shortly"
        ) from None
    except psycopg.Error as exc:
        logger.warning("database write failed: %s", exc)
        raise DataSourceUnavailable("the incident stand database write failed") from None


def _split_page(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    has_more = len(rows) > limit
    return rows[:limit], has_more


# --- incidents --------------------------------------------------------------

_INCIDENT_COLUMNS = "id, service, severity, status, opened_at, title, summary"


async def search_incidents(
    pool: AsyncConnectionPool,
    *,
    service: str,
    severity: Severity | None,
    status: IncidentStatus | None,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> IncidentsResult:
    require_service(service, CATALOG_SERVICES, "incidents_search")

    clauses = ["service = %(service)s"]
    params: dict[str, Any] = {"service": service, "limit": limit + 1}
    if severity is not None:
        clauses.append("severity = %(severity)s")
        params["severity"] = severity.value
    if status is not None:
        clauses.append("status = %(status)s")
        params["status"] = status.value
    if start is not None and end is not None:
        clauses.append("opened_at >= %(start)s AND opened_at < %(end)s")
        params["start"] = start
        params["end"] = end

    query = (
        sql.SQL(
            "SELECT id, service, severity, status, opened_at, title, summary FROM incidents WHERE "
        )
        + sql.SQL(" AND ").join(sql.SQL(clause) for clause in clauses)
        + sql.SQL(" ORDER BY opened_at DESC, id ASC LIMIT %(limit)s")
    )

    rows, has_more = _split_page(await _fetch_all(pool, query, params), limit)
    return IncidentsResult(
        items=[Incident(**row) for row in rows],
        count=len(rows),
        limit=limit,
        has_more=has_more,
        service=service,
        severity=severity,
        status=status,
        time_range=TimeRangeOut.of(start, end) if start is not None and end is not None else None,
    )


async def get_incident(pool: AsyncConnectionPool, *, incident_id: str) -> IncidentResult:
    row = await _fetch_one(
        pool,
        "SELECT id, service, severity, status, opened_at, title, summary "
        "FROM incidents WHERE id = %(id)s",
        {"id": incident_id},
    )
    if row is None:
        raise NotFound(f"no incident with id {incident_id!r}")
    return IncidentResult(item=Incident(**row))


async def acknowledge_incident(
    pool: AsyncConnectionPool, *, incident_id: str
) -> IncidentAcknowledgeResult:
    """Move an open incident to ``acknowledged``.

    Repeated acknowledgement is idempotent. A resolved incident cannot be
    acknowledged. The row is locked so concurrent acknowledgements serialize
    and the second caller observes ``changed=False``.
    """

    async def operation(conn: AsyncConnection[dict[str, Any]]) -> IncidentAcknowledgeResult:
        cur = await conn.execute(
            f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE id = %(id)s FOR UPDATE",
            {"id": incident_id},
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFound(f"no incident with id {incident_id!r}")

        previous = IncidentStatus(row["status"])
        if previous is IncidentStatus.ACKNOWLEDGED:
            return IncidentAcknowledgeResult(
                item=Incident(**row), changed=False, previous_status=previous
            )
        if previous is not IncidentStatus.OPEN:
            raise InvalidTransition(
                f"cannot acknowledge incident {incident_id!r} in status {previous.value!r}"
            )

        cur = await conn.execute(
            f"UPDATE incidents SET status = %(status)s WHERE id = %(id)s "
            f"RETURNING {_INCIDENT_COLUMNS}",
            {"id": incident_id, "status": IncidentStatus.ACKNOWLEDGED.value},
        )
        updated = await cur.fetchone()
        if updated is None:  # pragma: no cover - guarded by the locked SELECT
            raise NotFound(f"no incident with id {incident_id!r}")
        return IncidentAcknowledgeResult(
            item=Incident(**updated), changed=True, previous_status=previous
        )

    return await _in_write_transaction(pool, operation)


async def create_incident_summary(
    pool: AsyncConnectionPool, *, incident_id: str, summary: str
) -> IncidentSummaryResult:
    """Create or overwrite ``incidents.summary`` for one incident.

    The input is trimmed and bounded. An identical summary is a no-op. Writes
    are serialized with ``SELECT ... FOR UPDATE``; concurrent callers therefore
    apply last-writer-wins without touching any other incident field.
    """

    text = require_summary(summary)

    async def operation(conn: AsyncConnection[dict[str, Any]]) -> IncidentSummaryResult:
        cur = await conn.execute(
            f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE id = %(id)s FOR UPDATE",
            {"id": incident_id},
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFound(f"no incident with id {incident_id!r}")

        previous_present = row["summary"] is not None
        if row["summary"] == text:
            return IncidentSummaryResult(
                item=Incident(**row),
                changed=False,
                previous_summary_present=previous_present,
            )

        cur = await conn.execute(
            f"UPDATE incidents SET summary = %(summary)s WHERE id = %(id)s "
            f"RETURNING {_INCIDENT_COLUMNS}",
            {"id": incident_id, "summary": text},
        )
        updated = await cur.fetchone()
        if updated is None:  # pragma: no cover - guarded by the locked SELECT
            raise NotFound(f"no incident with id {incident_id!r}")
        return IncidentSummaryResult(
            item=Incident(**updated),
            changed=True,
            previous_summary_present=previous_present,
        )

    return await _in_write_transaction(pool, operation)


# --- deploys ----------------------------------------------------------------


async def recent_deploys(pool: AsyncConnectionPool, *, service: str, limit: int) -> DeploysResult:
    require_service(service, CATALOG_SERVICES, "deploys_recent")

    rows, has_more = _split_page(
        await _fetch_all(
            pool,
            "SELECT id, service, version, deployed_at, author, notes FROM deploys "
            "WHERE service = %(service)s "
            "ORDER BY deployed_at DESC, id DESC LIMIT %(limit)s",
            {"service": service, "limit": limit + 1},
        ),
        limit,
    )
    return DeploysResult(
        items=[Deploy(**row) for row in rows],
        count=len(rows),
        limit=limit,
        has_more=has_more,
        service=service,
    )


# --- logs -------------------------------------------------------------------


async def query_logs(
    pool: AsyncConnectionPool,
    *,
    service: str,
    start: datetime,
    end: datetime,
    query: str | None,
    level: LogLevel | None,
    limit: int,
) -> LogsResult:
    require_service(service, LOG_SERVICES, "logs_query")

    clauses = ["service = %(service)s", "ts >= %(start)s", "ts < %(end)s"]
    params: dict[str, Any] = {
        "service": service,
        "start": start,
        "end": end,
        "limit": limit + 1,
    }
    if level is not None:
        clauses.append("level = %(level)s")
        params["level"] = level.value
    if query is not None:
        # Literal, case-insensitive substring match. No regex, no wildcards.
        clauses.append("position(lower(%(query)s) in lower(message)) > 0")
        params["query"] = query

    statement = (
        sql.SQL("SELECT ts, service, level, message, request_id FROM app_logs WHERE ")
        + sql.SQL(" AND ").join(sql.SQL(clause) for clause in clauses)
        + sql.SQL(" ORDER BY ts DESC, id DESC LIMIT %(limit)s")
    )

    rows, has_more = _split_page(await _fetch_all(pool, statement, params), limit)
    items = []
    for row in rows:
        message = row["message"]
        truncated = len(message) > MAX_MESSAGE_CHARS
        items.append(
            LogEntry(
                ts=row["ts"],
                service=row["service"],
                level=row["level"],
                message=message[:MAX_MESSAGE_CHARS],
                message_truncated=truncated,
                request_id=row["request_id"],
            )
        )
    return LogsResult(
        items=items,
        count=len(items),
        limit=limit,
        has_more=has_more,
        service=service,
        level=level,
        query=query,
        time_range=TimeRangeOut.of(start, end),
    )


# --- metrics ----------------------------------------------------------------

_METRICS_SQL = sql.SQL(
    """
    WITH bounds AS (
        SELECT %(start)s::timestamptz AS start_ts,
               %(end)s::timestamptz   AS end_ts,
               %(bucket)s::interval   AS stride
    ),
    buckets AS (
        SELECT gs AS bucket_start, gs + b.stride AS bucket_end
        FROM bounds b,
             generate_series(b.start_ts, b.end_ts, b.stride) AS gs
        WHERE gs < b.end_ts
    ),
    aggregated AS (
        SELECT date_bin(b.stride, r.ts, b.start_ts) AS bucket_start,
               count(*)                            AS request_count,
               avg(r.duration_ms)                  AS avg_latency_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY r.duration_ms) AS p95_latency_ms,
               100.0 * count(*) FILTER (WHERE r.cache_hit)
                     / nullif(count(r.cache_hit), 0) AS cache_hit_pct
        FROM request_logs r, bounds b
        WHERE r.service = %(service)s
          AND r.endpoint = %(endpoint)s
          AND r.ts >= b.start_ts
          AND r.ts < b.end_ts
        GROUP BY 1
    )
    SELECT b.bucket_start,
           b.bucket_end,
           coalesce(a.request_count, 0) AS request_count,
           a.avg_latency_ms,
           a.p95_latency_ms,
           a.cache_hit_pct
    FROM buckets b
    LEFT JOIN aggregated a USING (bucket_start)
    ORDER BY b.bucket_start
    """
)


async def latency_metrics(
    pool: AsyncConnectionPool,
    *,
    service: str,
    endpoint: str,
    start: datetime,
    end: datetime,
    bucket: Bucket,
) -> LatencyResult:
    require_service(service, METRIC_SERVICES, "metrics_latency")
    require_endpoint(endpoint)
    bucket_count(start, end, bucket)  # bounds the aggregation

    rows = await _fetch_all(
        pool,
        _METRICS_SQL,
        {
            "service": service,
            "endpoint": endpoint,
            "start": start,
            "end": end,
            "bucket": f"{bucket.seconds} seconds",
        },
    )

    buckets = [
        LatencyBucket(
            bucket_start=row["bucket_start"],
            bucket_end=row["bucket_end"],
            request_count=int(row["request_count"]),
            avg_latency_ms=_round(row["avg_latency_ms"]),
            p95_latency_ms=_round(row["p95_latency_ms"]),
            cache_hit_pct=_round(row["cache_hit_pct"]),
        )
        for row in rows
    ]
    return LatencyResult(
        service=service,
        endpoint=endpoint,
        time_range=TimeRangeOut.of(start, end),
        bucket=bucket,
        bucket_seconds=bucket.seconds,
        total_requests=sum(item.request_count for item in buckets),
        buckets=buckets,
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


# --- runbooks ---------------------------------------------------------------


def read_runbook(settings: Settings, *, service: str) -> RunbookResult:
    require_service(service, CATALOG_SERVICES, "runbook_get")

    base = settings.runbooks_dir.resolve()
    if not base.is_dir():
        logger.warning("runbook directory is missing: %s", base)
        raise DataSourceUnavailable("the runbook directory is not available")

    candidate = base / f"{service}.md"
    if candidate.is_symlink():
        raise InvalidArguments("refusing to follow a symlinked runbook")

    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        # Unreachable through the allowlist, kept as defence in depth.
        raise InvalidArguments("runbook path escapes the runbook directory")
    if not resolved.is_file():
        raise NotFound(f"no runbook for service {service!r}")

    try:
        size = resolved.stat().st_size
    except OSError as exc:
        logger.warning("cannot stat runbook %s: %s", resolved, exc)
        raise DataSourceUnavailable("the runbook could not be read") from None

    if size > MAX_RUNBOOK_BYTES:
        raise InvalidArguments(f"runbook is {size} bytes; the maximum is {MAX_RUNBOOK_BYTES}")

    try:
        content = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("cannot read runbook %s: %s", resolved, exc)
        raise DataSourceUnavailable("the runbook could not be read") from None

    return RunbookResult(
        item=Runbook(
            service=service,
            path=str(_relative_to(resolved, base)),
            content=content,
            size_bytes=size,
        )
    )


def _relative_to(path: Path, base: Path) -> Path:
    try:
        return path.relative_to(base)
    except ValueError:  # pragma: no cover - guarded by is_relative_to above
        return path


# --- service catalog --------------------------------------------------------


async def get_service_catalog(pool: AsyncConnectionPool, *, service: str) -> ServiceCatalogResult:
    require_service(service, CATALOG_SERVICES, "service_catalog_get")

    row = await _fetch_one(
        pool,
        "SELECT name, team, oncall, dependencies, description FROM services WHERE name = %(name)s",
        {"name": service},
    )
    if row is None:
        raise NotFound(f"no catalog entry for service {service!r}")

    return ServiceCatalogResult(
        item=ServiceCatalogEntry(
            name=row["name"],
            team=row["team"],
            oncall=row["oncall"],
            dependencies=list(row["dependencies"] or []),
            description=row["description"],
        )
    )
