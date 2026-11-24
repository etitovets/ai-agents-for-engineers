"""Process-wide runtime owned by the MCP server lifespan."""

from __future__ import annotations

import logging
from collections.abc import Callable

from psycopg_pool import AsyncConnectionPool

from .errors import DataSourceUnavailable
from .pool import create_pool
from .settings import Settings

logger = logging.getLogger(__name__)


class StandRuntime:
    """Holds the settings and the single pool for one server process."""

    def __init__(
        self,
        settings: Settings,
        *,
        pool_factory: Callable[[Settings], AsyncConnectionPool] = create_pool,
    ) -> None:
        self.settings = settings
        self._pool_factory = pool_factory
        self._pool: AsyncConnectionPool | None = None
        self.open_count = 0

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise DataSourceUnavailable("the database pool is not open")
        return self._pool

    async def start(self) -> None:
        if self._pool is not None:
            logger.debug("runtime already started; reusing the existing pool")
            return

        pool = self._pool_factory(self.settings)
        timeout = self.settings.pool_acquire_timeout + self.settings.db_connect_timeout
        try:
            await pool.open(wait=True, timeout=timeout)
            # Probe once so startup fails fast even with pool_min_size=0, and so
            # a misconfigured DSN never reaches the first tool call.
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:
            # Never include the DSN: psycopg errors can carry host and user.
            logger.exception("failed to open the postgres pool (%s)", self.settings.redacted_dsn)
            await pool.close()
            raise DataSourceUnavailable(
                "cannot connect to the incident stand database; "
                "check STAND_DATABASE_URL and that the stand is running"
            ) from None

        self._pool = pool
        self.open_count += 1
        logger.info(
            "postgres pool opened: min=%s max=%s timeout=%ss",
            self.settings.pool_min_size,
            self.settings.pool_max_size,
            self.settings.pool_acquire_timeout,
        )

    async def stop(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        await pool.close()
        logger.info("postgres pool closed")
