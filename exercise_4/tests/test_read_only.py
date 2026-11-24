"""Read-only invariants: tools must never alter the observed stand."""

from __future__ import annotations

import hashlib

import psycopg
import pytest
from fastmcp import Client
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from incident_mcp.runtime import StandRuntime
from tests.conftest import CATALOG_ENDPOINT, FIXED_END, FIXED_START, PRICE_ENDPOINT, RUNBOOKS_DIR

TABLES = ("services", "deploys", "incidents", "request_logs", "app_logs", "price_events")


async def table_counts(pool: AsyncConnectionPool) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with pool.connection() as conn:
        for table in TABLES:
            query = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
            cur = await conn.execute(query)
            row = await cur.fetchone()
            counts[table] = int(row["n"])
    return counts


def runbook_digests() -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def incident_rows(dsn: str) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT id, service, severity, status, opened_at, title, summary "
            "FROM incidents ORDER BY id"
        ).fetchall()


async def test_tool_calls_leave_data_and_runbooks_untouched(
    client: Client, pool: AsyncConnectionPool
) -> None:
    before_counts = await table_counts(pool)
    before_digests = runbook_digests()
    assert before_counts["request_logs"] > 0
    assert before_digests

    calls = [
        ("incidents_search", {"service": "payments"}),
        ("incident_get", {"incident_id": "INC-001"}),
        ("deploys_recent", {"service": "payments"}),
        (
            "logs_query",
            {
                "service": "payments-api",
                "time_range": {"start": FIXED_START.isoformat(), "end": FIXED_END.isoformat()},
            },
        ),
        (
            "metrics_latency",
            {
                "service": "payments-api",
                "endpoint": PRICE_ENDPOINT,
                "time_range": {"start": FIXED_START.isoformat(), "end": FIXED_END.isoformat()},
            },
        ),
        (
            "metrics_latency",
            {"service": "payments-api", "endpoint": CATALOG_ENDPOINT},
        ),
        ("runbook_get", {"service": "payments"}),
        ("service_catalog_get", {"service": "checkout"}),
    ]
    for name, arguments in calls:
        await client.call_tool(name, arguments)

    assert await table_counts(pool) == before_counts
    assert runbook_digests() == before_digests


async def test_write_tools_change_only_authorized_incident_fields(
    client: Client,
    pool: AsyncConnectionPool,
    test_dsn: str,
    reset_incident_state: None,
) -> None:
    before_counts = await table_counts(pool)
    before_rows = incident_rows(test_dsn)
    before_digests = runbook_digests()

    await client.call_tool("incident_acknowledge", {"incident_id": "INC-001"})
    await client.call_tool(
        "incident_create_summary",
        {"incident_id": "INC-004", "summary": "Cache key included request_id."},
    )

    assert await table_counts(pool) == before_counts
    assert runbook_digests() == before_digests

    after_rows = incident_rows(test_dsn)
    by_id_before = {row[0]: row for row in before_rows}
    by_id_after = {row[0]: row for row in after_rows}
    assert set(by_id_before) == set(by_id_after)

    for incident_id, before in by_id_before.items():
        after = by_id_after[incident_id]
        if incident_id == "INC-001":
            assert after[:3] == before[:3]
            assert after[3] == "acknowledged"
            assert after[4:] == before[4:]
        elif incident_id == "INC-004":
            assert after[:6] == before[:6]
            assert after[6] == "Cache key included request_id."
        else:
            assert after == before


def _run_as_role(conn: psycopg.Connection, role: str, statement: str) -> None:
    conn.execute("BEGIN")
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    try:
        conn.execute(statement)
    finally:
        conn.rollback()


def test_restricted_role_allows_only_incident_status_and_summary(
    test_dsn: str, restricted_role: str
) -> None:
    with psycopg.connect(test_dsn, autocommit=True) as conn:
        for statement in (
            "UPDATE incidents SET title = title WHERE id = 'INC-001'",
            "UPDATE deploys SET notes = notes",
            "DELETE FROM incidents WHERE id = 'INC-001'",
            "INSERT INTO incidents (id, service, severity, status, opened_at, title) "
            "VALUES ('INC-999', 'payments', 'low', 'open', now(), 'x')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                _run_as_role(conn, restricted_role, statement)

        _run_as_role(
            conn,
            restricted_role,
            "UPDATE incidents SET status = 'acknowledged' WHERE id = 'INC-001'",
        )
        _run_as_role(
            conn,
            restricted_role,
            "UPDATE incidents SET summary = 'x' WHERE id = 'INC-001'",
        )


async def test_every_connection_is_read_only(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        cur = await conn.execute("SHOW default_transaction_read_only")
        assert (await cur.fetchone())["default_transaction_read_only"] == "on"

        cur = await conn.execute("SHOW transaction_read_only")
        assert (await cur.fetchone())["transaction_read_only"] == "on"

        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await conn.execute(
                "INSERT INTO services (name, team, oncall) VALUES ('rogue', 'x', 'y')"
            )

        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await conn.execute("CREATE TABLE should_not_exist (id int)")

        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await conn.execute("UPDATE incidents SET status = 'resolved'")

        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await conn.execute("DELETE FROM request_logs")


async def test_pool_is_created_once_and_reused(client: Client, runtime: StandRuntime) -> None:
    pool = runtime.pool
    assert runtime.open_count == 1

    for _ in range(3):
        await client.call_tool("service_catalog_get", {"service": "payments"})
        await client.call_tool("deploys_recent", {"service": "payments"})

    assert runtime.open_count == 1
    assert runtime.pool is pool
    stats = pool.get_stats()
    assert stats["pool_size"] <= runtime.settings.pool_max_size
    assert stats["requests_waiting"] == 0


async def test_pool_is_closed_on_shutdown(runtime: StandRuntime) -> None:
    pool = runtime.pool
    assert pool.closed is False

    await runtime.stop()

    assert pool.closed is True
    assert runtime.open_count == 1
    # Stopping twice must not raise.
    await runtime.stop()
