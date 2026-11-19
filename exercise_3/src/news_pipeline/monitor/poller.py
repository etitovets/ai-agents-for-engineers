"""Polling orchestrator for the Monitor.

Polls all configured feeds, deduplicates against the persistent seen-store,
publishes new items to RabbitMQ, and marks them seen only after RabbitMQ
confirmed the publication.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

import httpx
from faststream.rabbit import RabbitBroker

from news_pipeline.monitor.dedup import SeenStore
from news_pipeline.monitor.feeds import fetch_feed, iter_feed_urls, parse_feed
from news_pipeline.monitor.publisher import declare_news_queue, publish_items
from news_pipeline.schemas import NewsItem

logger = logging.getLogger(__name__)


async def _load_feed(client: httpx.AsyncClient, url: str, source: str) -> List[NewsItem]:
    try:
        content = await fetch_feed(client, url)
        return parse_feed(content, url, source)
    except Exception as exc:
        logger.error("Feed source=%s url=%s failed: %s", source, url, exc)
        return []


async def poll_all_feeds(
    broker: RabbitBroker,
    seen_store: SeenStore,
    client: httpx.AsyncClient,
) -> int:
    """Poll every configured feed once and publish new items.

    Returns the number of messages confirmed by RabbitMQ in this invocation.
    Feed failures are isolated so one bad feed does not stop the others.
    """
    feeds = list(iter_feed_urls())
    results = await asyncio.gather(*(_load_feed(client, url, source) for url, source in feeds))

    await declare_news_queue(broker)

    total_published = 0
    for (_url, source), items in zip(feeds, results, strict=True):
        new_items: List[NewsItem] = []
        batch_links: set[str] = set()
        for item in items:
            link = str(item.link)
            if link in batch_links or await seen_store.is_seen(source, link):
                continue
            batch_links.add(link)
            new_items.append(item)

        if not new_items:
            logger.info("Feed source=%s: no new items", source)
            continue

        confirmed = await publish_items(broker, new_items)
        for item in confirmed:
            try:
                await seen_store.mark_seen(source, str(item.link))
            except Exception:
                logger.exception("Failed to mark seen link=%s; may be republished", item.link)

        logger.info(
            "Feed source=%s: published %d/%d new items", source, len(confirmed), len(new_items)
        )
        total_published += len(confirmed)

    logger.info("Poll complete: %d new items published", total_published)
    return total_published
