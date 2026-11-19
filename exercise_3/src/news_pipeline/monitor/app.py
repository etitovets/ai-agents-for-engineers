"""Monitor FastAPI application.

Endpoints:
    GET  /health   -> {"status":"ok"}
    POST /refresh  -> polls all feeds, returns {"new_items": N}

A background scheduler polls all feeds once at startup and then every
POLL_INTERVAL_SECONDS (300s). A single asyncio.Lock makes polls single-flight:
a /refresh that arrives during another poll waits for it to finish and then
runs its own poll (which usually finds 0 new items). Manual refreshes never
reset or disable the periodic schedule.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator, Dict

from fastapi import FastAPI, Request
from faststream.rabbit import RabbitBroker

from news_pipeline.config import configure_logging, settings
from news_pipeline.monitor.dedup import SeenStore
from news_pipeline.monitor.feeds import make_http_client
from news_pipeline.monitor.poller import poll_all_feeds
from news_pipeline.monitor.publisher import declare_news_queue

configure_logging()
logger = logging.getLogger(__name__)


async def run_poll(app: FastAPI) -> int:
    async with app.state.poll_lock:
        return await poll_all_feeds(app.state.broker, app.state.seen_store, app.state.http)


async def _scheduler_loop(app: FastAPI) -> None:
    while True:
        try:
            await run_poll(app)
        except Exception:
            logger.exception("Scheduled poll failed")
        await asyncio.sleep(settings.poll_interval_seconds)


async def _connect_broker_with_retry(broker: RabbitBroker) -> None:
    attempts = settings.startup_retry_attempts
    for attempt in range(1, attempts + 1):
        try:
            await broker.connect()
            return
        except Exception as exc:
            if attempt == attempts:
                raise
            logger.warning(
                "RabbitMQ not ready (attempt %d/%d): %s", attempt, attempts, exc.__class__.__name__
            )
            await asyncio.sleep(settings.startup_retry_delay_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    broker = RabbitBroker(settings.rabbitmq_url)
    await _connect_broker_with_retry(broker)
    await declare_news_queue(broker)

    seen_store = SeenStore()
    await seen_store.open()

    app.state.broker = broker
    app.state.seen_store = seen_store
    app.state.http = make_http_client()
    app.state.poll_lock = asyncio.Lock()

    scheduler = asyncio.create_task(_scheduler_loop(app), name="feed-scheduler")
    logger.info("Monitor started; polling every %ss", settings.poll_interval_seconds)
    try:
        yield
    finally:
        scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler
        await app.state.http.aclose()
        await seen_store.close()
        await broker.close()
        logger.info("Monitor stopped")


app = FastAPI(title="News Pipeline Monitor", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/refresh")
async def refresh(request: Request) -> Dict[str, int]:
    return {"new_items": await run_poll(request.app)}
