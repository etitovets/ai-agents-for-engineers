"""PostgreSQL repository for news items and entities.

Inserts a news item and its entities in ONE transaction. Duplicate links use
ON CONFLICT (link) DO NOTHING so replayed messages are idempotent: no duplicate
news rows and no incremented entity counts on replay.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import asyncpg

from news_pipeline.schemas import NewsItem

logger = logging.getLogger(__name__)

Entity = Tuple[str, str, int]  # (text, label, count)


class NewsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def insert_news_and_entities(
        self,
        item: NewsItem,
        entities: List[Entity],
    ) -> bool:
        """Insert a news item and its entities in one transaction.

        Returns True if the news row was inserted, False if the link already
        existed (idempotent replay - no changes made).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO news (source, title, link, published_at, text)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (link) DO NOTHING
                    RETURNING id
                    """,
                    item.source,
                    item.title,
                    str(item.link),
                    item.published_at,
                    item.text,
                )
                if row is None:
                    logger.info("Duplicate link skipped (idempotent replay): %s", item.link)
                    return False

                news_id = row["id"]
                if entities:
                    await conn.executemany(
                        """
                        INSERT INTO entities (news_id, text, label, count)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (news_id, text, label) DO NOTHING
                        """,
                        [(news_id, text, label, count) for text, label, count in entities],
                    )
                logger.info("Inserted news id=%s with %d entities", news_id, len(entities))
                return True
