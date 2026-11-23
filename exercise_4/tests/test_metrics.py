"""Exact aggregation expectations for ``metrics_latency``.

Fixture (see ``tests/conftest.py``), endpoint ``/api/v1/orders/{order_id}/price``
for service ``payments-api`` over ``[09:40, 09:45)`` on 2026-07-28:

===========  =======  =======  =======  =============
bucket       count    avg      p95      cache-hit %
===========  =======  =======  =======  =============
09:40        5        30.0     48.0     40.0
09:41        0        -        -        -
09:42        2        150.0    195.0    0.0
09:43        1        5.0      5.0      100.0
09:44        0        -        -        -
===========  =======  =======  =======  =============
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg_pool import AsyncConnectionPool

from oncall_mcp import repositories
from oncall_mcp.errors import InvalidArguments
from oncall_mcp.models import (
    MAX_METRIC_WINDOW,
    Bucket,
    LatencyResult,
    TimeRange,
    resolve_window,
)
from tests.conftest import CATALOG_ENDPOINT, FIXED_END, FIXED_START, PRICE_ENDPOINT


async def fetch(
    pool: AsyncConnectionPool,
    *,
    start: datetime = FIXED_START,
    end: datetime = FIXED_END,
    bucket: Bucket = Bucket.M1,
    endpoint: str = PRICE_ENDPOINT,
) -> LatencyResult:
    return await repositories.latency_metrics(
        pool,
        service="payments-api",
        endpoint=endpoint,
        start=start,
        end=end,
        bucket=bucket,
    )


async def test_one_minute_buckets_match_hand_computed_values(
    pool: AsyncConnectionPool,
) -> None:
    result = await fetch(pool)

    assert result.bucket == Bucket.M1
    assert result.bucket_seconds == 60
    assert result.total_requests == 8
    assert len(result.buckets) == 5

    expected = [
        (5, 30.0, 48.0, 40.0),
        (0, None, None, None),
        (2, 150.0, 195.0, 0.0),
        (1, 5.0, 5.0, 100.0),
        (0, None, None, None),
    ]
    for offset, (bucket, values) in enumerate(zip(result.buckets, expected, strict=True)):
        count, avg, p95, cache = values
        assert bucket.request_count == count
        assert bucket.avg_latency_ms == avg
        assert bucket.p95_latency_ms == p95
        assert bucket.cache_hit_pct == cache
        assert bucket.bucket_start == FIXED_START + timedelta(minutes=offset)
        assert bucket.bucket_end == bucket.bucket_start + timedelta(minutes=1)


async def test_p95_uses_linear_interpolation_not_nearest_rank(
    pool: AsyncConnectionPool,
) -> None:
    result = await fetch(pool)
    first = result.buckets[0]

    # percentile_cont(0.95) over [10,20,30,40,50]: rank 1 + 0.95*4 = 4.8
    # -> 40 + 0.8*(50-40) = 48.0. Nearest-rank would return 50.0.
    assert first.p95_latency_ms == 48.0

    second = result.buckets[2]
    # [100,200]: rank 1.95 -> 100 + 0.95*100 = 195.0
    assert second.p95_latency_ms == 195.0


async def test_single_row_bucket_p95_equals_the_value(
    pool: AsyncConnectionPool,
) -> None:
    result = await fetch(pool)
    third = result.buckets[3]
    assert third.request_count == 1
    assert third.p95_latency_ms == 5.0
    assert third.avg_latency_ms == 5.0


async def test_cache_hit_percentage_ignores_null_rows(
    pool: AsyncConnectionPool,
) -> None:
    result = await fetch(pool, endpoint=CATALOG_ENDPOINT)

    bucket = result.buckets[1]
    assert bucket.request_count == 2
    assert bucket.avg_latency_ms == 8.0
    assert bucket.p95_latency_ms == 8.9
    # Both rows have cache_hit = NULL: denominator count(cache_hit) is 0 -> null,
    # not 0.0%.
    assert bucket.cache_hit_pct is None


async def test_empty_buckets_are_emitted_with_null_statistics(
    pool: AsyncConnectionPool,
) -> None:
    result = await fetch(pool)

    for index in (1, 4):
        bucket = result.buckets[index]
        assert bucket.request_count == 0
        assert bucket.avg_latency_ms is None
        assert bucket.p95_latency_ms is None
        assert bucket.cache_hit_pct is None


async def test_window_is_half_open(pool: AsyncConnectionPool) -> None:
    result = await fetch(pool)

    # Fixtures at 09:39:59.999 and exactly 09:45:00 carry duration_ms=999 and
    # must be excluded.
    assert result.total_requests == 8
    assert all(
        bucket.avg_latency_ms is None or bucket.avg_latency_ms < 999.0 for bucket in result.buckets
    )


async def test_other_services_never_leak_into_metrics(
    pool: AsyncConnectionPool,
) -> None:
    result = await fetch(pool)

    # The fixture also has a `checkout` row on the same endpoint with 1000 ms.
    assert result.total_requests == 8
    assert max(bucket.avg_latency_ms or 0.0 for bucket in result.buckets) == 150.0


async def test_smaller_buckets_align_to_window_start(
    pool: AsyncConnectionPool,
) -> None:
    result = await fetch(pool, bucket=Bucket.S10)

    assert len(result.buckets) == 30
    assert result.buckets[0].bucket_start == FIXED_START
    assert all(
        bucket.bucket_end - bucket.bucket_start == timedelta(seconds=10)
        for bucket in result.buckets
    )
    # Only 09:40:05 (10 ms) falls into the first ten seconds.
    assert result.buckets[0].request_count == 1
    assert result.buckets[0].avg_latency_ms == 10.0


async def test_bucket_count_rounds_up(pool: AsyncConnectionPool) -> None:
    result = await fetch(
        pool,
        start=FIXED_START,
        end=FIXED_START + timedelta(seconds=90),
        bucket=Bucket.M1,
    )
    assert len(result.buckets) == 2
    assert result.buckets[-1].bucket_end == FIXED_START + timedelta(seconds=120)


async def test_bucket_limit_is_enforced(pool: AsyncConnectionPool) -> None:
    # A clean 24-hour window with no fixture rows inside it.
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=24)

    with pytest.raises(InvalidArguments, match="maximum is 500"):
        await fetch(pool, start=start, end=end, bucket=Bucket.M1)

    wide = await fetch(pool, start=start, end=end, bucket=Bucket.M5)
    assert len(wide.buckets) == 288
    assert wide.total_requests == 0


def test_time_range_maximum_is_enforced() -> None:
    start = datetime(2026, 7, 28, tzinfo=UTC)
    too_wide = TimeRange(start=start - timedelta(hours=25), end=start)

    with pytest.raises(InvalidArguments, match="maximum allowed"):
        resolve_window(too_wide, MAX_METRIC_WINDOW)

    exact = TimeRange(start=start - timedelta(hours=24), end=start)
    assert resolve_window(exact, MAX_METRIC_WINDOW) == (
        start - timedelta(hours=24),
        start,
    )
