"""Runtime lifecycle: one pool per process, sanitized startup failures."""

from __future__ import annotations

import pytest

from oncall_mcp.errors import DataSourceUnavailable
from oncall_mcp.runtime import StandRuntime
from oncall_mcp.settings import Settings


def unreachable_settings() -> Settings:
    return Settings(
        database_url="postgresql://oncall:supersecret@127.0.0.1:1/oncall",
        pool_min_size=0,
        pool_max_size=1,
        pool_acquire_timeout=0.5,
        db_connect_timeout=1.0,
    )


async def test_unreachable_database_fails_without_leaking_the_dsn() -> None:
    runtime = StandRuntime(unreachable_settings())

    with pytest.raises(DataSourceUnavailable) as caught:
        await runtime.start()

    message = str(caught.value)
    assert "supersecret" not in message
    assert "postgresql://" not in message
    assert "cannot connect" in message
    assert runtime.open_count == 0
    with pytest.raises(DataSourceUnavailable, match="not open"):
        _ = runtime.pool


async def test_start_is_idempotent(settings: Settings) -> None:
    runtime = StandRuntime(settings)
    await runtime.start()
    try:
        first = runtime.pool
        await runtime.start()
        assert runtime.pool is first
        assert runtime.open_count == 1
    finally:
        await runtime.stop()


async def test_stop_closes_the_pool(settings: Settings) -> None:
    runtime = StandRuntime(settings)
    await runtime.start()
    pool = runtime.pool

    await runtime.stop()

    assert pool.closed is True
    with pytest.raises(DataSourceUnavailable, match="not open"):
        _ = runtime.pool
