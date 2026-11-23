"""Read-only invariants: tools must never alter the observed stand."""

from __future__ import annotations

import hashlib

import psycopg
import pytest
from fastmcp import Client
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from oncall_mcp.runtime import StandRuntime
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
