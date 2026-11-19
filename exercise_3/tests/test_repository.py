"""Repository tests.

The unit tests use a fake asyncpg pool. The integration tests run against a
real PostgreSQL (with db/init.sql applied) when TEST_DATABASE_URL is set, e.g.
TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/news
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from news_pipeline.schemas import NewsItem
from news_pipeline.worker.repository import NewsRepository


class FakeConnection:
    def __init__(self, fetchrow_return=None):
        self.fetchrow_return = fetchrow_return
        self.executemany_calls: list = []

    async def fetchrow(self, *args):
        return self.fetchrow_return

    async def executemany(self, query, rows):
        self.executemany_calls.append(rows)

    def transaction(self):
        return _AsyncNullContext()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _AsyncNullContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


async def test_insert_news_and_entities(news_item):
    conn = FakeConnection(fetchrow_return={"id": 1})
    inserted = await NewsRepository(FakePool(conn)).insert_news_and_entities(
        news_item, [("Москва", "LOC", 2), ("Газпром", "ORG", 1)]
    )
    assert inserted is True
    assert conn.executemany_calls == [[(1, "Москва", "LOC", 2), (1, "Газпром", "ORG", 1)]]


async def test_duplicate_link_skips_entities(news_item):
    conn = FakeConnection(fetchrow_return=None)
    inserted = await NewsRepository(FakePool(conn)).insert_news_and_entities(
        news_item, [("Москва", "LOC", 1)]
    )
    assert inserted is False
    assert conn.executemany_calls == []


DB_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    yield pool
    await pool.execute("DELETE FROM news WHERE link LIKE 'https://test.local/%'")
    await pool.close()


def unique_item(news_item: NewsItem) -> NewsItem:
    return news_item.model_copy(update={"link": f"https://test.local/{uuid.uuid4()}"})


async def _counts(pool, link: str) -> tuple[int, int, int]:
    row = await pool.fetchrow(
        """
        SELECT count(DISTINCT n.id) AS news, count(e.id) AS entities,
               coalesce(sum(e.count), 0) AS occurrences
        FROM news n LEFT JOIN entities e ON e.news_id = n.id
        WHERE n.link = $1
        """,
        link,
    )
    return row["news"], row["entities"], row["occurrences"]


@requires_db
async def test_replay_is_idempotent(pg_pool, news_item):
    item = unique_item(news_item)
    repo = NewsRepository(pg_pool)
    entities = [("Путин", "PER", 2), ("Москва", "LOC", 1)]

    assert await repo.insert_news_and_entities(item, entities) is True
    assert await repo.insert_news_and_entities(item, entities) is False
    assert await _counts(pg_pool, str(item.link)) == (1, 2, 3)


@requires_db
async def test_entity_failure_rolls_back_news_row(pg_pool, news_item):
    item = unique_item(news_item)
    repo = NewsRepository(pg_pool)

    with pytest.raises(asyncpg.CheckViolationError):
        await repo.insert_news_and_entities(item, [("Путин", "PER", 0)])
    assert await _counts(pg_pool, str(item.link)) == (0, 0, 0)
