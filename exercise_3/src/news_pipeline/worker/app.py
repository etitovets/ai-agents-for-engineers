"""FastStream Worker application.

Consumes NewsItem messages from the durable queue, runs Russian SpaCy NER on
title + text, and persists results to PostgreSQL idempotently.

Acknowledgement policy:
- success (including idempotent duplicate) -> ack after the DB commit;
- invalid payload -> reject immediately (not requeued);
- NER failure -> reject (deterministic, retrying would not help);
- database/other errors -> nack + requeue, at most WORKER_MAX_RETRIES times
  per process, then reject.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import asyncpg
from faststream import FastStream
from faststream.exceptions import RejectMessage
from faststream.rabbit import RabbitBroker
from faststream.rabbit.message import RabbitMessage
from pydantic import ValidationError

from news_pipeline.config import configure_logging, settings
from news_pipeline.schemas import NewsItem
from news_pipeline.transport import news_queue
from news_pipeline.worker.ner import NERProcessor
from news_pipeline.worker.repository import NewsRepository

configure_logging()
logger = logging.getLogger(__name__)


@dataclass
class WorkerState:
    ner: Optional[NERProcessor] = None
    pool: Optional[asyncpg.Pool] = None
    repo: Optional[NewsRepository] = None


state = WorkerState()

broker = RabbitBroker(settings.rabbitmq_url)
app = FastStream(broker)


@app.on_startup
async def startup() -> None:
    ner = NERProcessor(settings.ner_model, settings.ner_max_chars)
    await asyncio.to_thread(ner.load)
    state.ner = ner
    state.pool = await create_pool_with_retry()
    state.repo = NewsRepository(state.pool)
    logger.info("Worker startup complete")


@app.on_shutdown
async def shutdown() -> None:
    if state.pool is not None:
        await state.pool.close()
        logger.info("PostgreSQL pool closed")


async def create_pool_with_retry() -> asyncpg.Pool:
    attempts = settings.startup_retry_attempts
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.create_pool(
                settings.database_url,
                min_size=settings.db_pool_min_size,
                max_size=settings.db_pool_max_size,
            )
        except (OSError, asyncpg.PostgresError) as exc:
            if attempt == attempts:
                raise
            logger.warning(
                "PostgreSQL not ready (attempt %d/%d): %s",
                attempt,
                attempts,
                exc.__class__.__name__,
            )
            await asyncio.sleep(settings.startup_retry_delay_seconds)
    raise RuntimeError("unreachable")


async def reject_invalid_payload(
    call_next: Callable[[RabbitMessage], Awaitable[Any]], msg: RabbitMessage
) -> Any:
    """Reject malformed payloads immediately instead of retrying them.

    Runs as a subscriber middleware (inside FastStream's ack scope); a custom
    decoder would raise before that scope and leave the message unacked.
    """
    try:
        return await call_next(msg)
    except ValidationError as exc:
        logger.error("Rejecting invalid message message_id=%s: %s", msg.message_id, exc)
        raise RejectMessage() from exc


async def process_news(item: NewsItem, ner: NERProcessor, repo: NewsRepository) -> bool:
    try:
        entities = await ner.extract(item.title, item.text)
    except Exception as exc:
        logger.exception("NER failed for link=%s; rejecting message", item.link)
        raise RejectMessage() from exc

    try:
        inserted = await repo.insert_news_and_entities(item, entities)
    except Exception:
        logger.exception("Database write failed for link=%s; will be redelivered", item.link)
        raise

    if inserted:
        logger.info("Stored link=%s source=%s entities=%d", item.link, item.source, len(entities))
    else:
        logger.info("Duplicate link=%s ignored (idempotent replay)", item.link)
    return inserted


@broker.subscriber(
    news_queue(), retry=settings.worker_max_retries, middlewares=[reject_invalid_payload]
)
async def handle_news(item: NewsItem) -> None:
    if state.ner is None or state.repo is None:
        raise RuntimeError("Worker not initialized")
    await process_news(item, state.ner, state.repo)
