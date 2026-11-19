"""Persistent deduplication store for the Monitor.

Uses SQLite (aiosqlite) mounted on a named Docker volume so that seen-state
survives container restarts. An item is marked as seen only after RabbitMQ
confirms successful publication.

The Monitor is treated as a single active replica; this local SQLite store does
not provide multi-replica exactly-once guarantees.
"""

from __future__ import annotations

from typing import Optional

import aiosqlite

from news_pipeline.config import settings


class SeenStore:
    """Tracks which article links have already been published.

    The dedup key is the normalized canonical link, scoped by source so that
    identical links from different feeds do not collide.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.dedup_db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_links (
                source      TEXT NOT NULL,
                link        TEXT NOT NULL,
                seen_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                PRIMARY KEY (source, link)
            )
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def is_seen(self, source: str, link: str) -> bool:
        if self._conn is None:
            raise RuntimeError("SeenStore is not open")
        cursor = await self._conn.execute(
            "SELECT 1 FROM seen_links WHERE source = ? AND link = ? LIMIT 1",
            (source, link),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def mark_seen(self, source: str, link: str) -> None:
        if self._conn is None:
            raise RuntimeError("SeenStore is not open")
        await self._conn.execute(
            "INSERT OR IGNORE INTO seen_links (source, link) VALUES (?, ?)",
            (source, link),
        )
        await self._conn.commit()
