"""One PostgreSQL connection pool per server process.

The pool is created once, opened during MCP startup, reused by every tool call,
and closed on shutdown. Each connection is configured read-only at the session
level. Only the two authorized write repositories open an explicit transaction
and switch that single transaction to read-write; the database role restricts
those writes to ``incidents.status`` and ``incidents.summary``.
"""

from __future__ import annotations

import logging

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .settings import Settings

logger = logging.getLogger(__name__)


def _configure_connection(settings: Settings):
    async def configure(conn: AsyncConnection) -> None:
        async with conn.cursor() as cur:
            await cur.execute("SET default_transaction_read_only = on")
            await cur.execute(
                sql.SQL("SET statement_timeout = {}").format(
                    sql.Literal(settings.statement_timeout_ms)
                )
            )
            await cur.execute(
                sql.SQL("SET idle_in_transaction_session_timeout = {}").format(
                    sql.Literal(settings.statement_timeout_ms)
                )
            )

    return configure


def create_pool(settings: Settings) -> AsyncConnectionPool:
    """Build an unopened pool. The caller opens it exactly once."""

    return AsyncConnectionPool(
        conninfo=settings.database_url.get_secret_value(),
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        timeout=settings.pool_acquire_timeout,
        kwargs={
            "connect_timeout": settings.db_connect_timeout,
            "row_factory": dict_row,
            "application_name": "incident-mcp",
            # Autocommit keeps the session out of idle transactions and lets the
            # read-only default apply to every implicit transaction.
            "autocommit": True,
        },
        configure=_configure_connection(settings),
        open=False,
        name="incident-mcp",
    )
