"""Monitor tests: API, dedup-before-publish, publish failures, single-flight polls.

RSS feeds are served by httpx.MockTransport and RabbitMQ is replaced by
FastStream's in-memory TestRabbitBroker, so no network is required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List

import httpx
import pytest
from faststream.rabbit import RabbitBroker, TestRabbitBroker

from news_pipeline.config import settings
from news_pipeline.monitor import app as monitor_app
from news_pipeline.monitor import publisher
from news_pipeline.monitor.dedup import SeenStore
from news_pipeline.monitor.poller import poll_all_feeds
from news_pipeline.schemas import NewsItem
from news_pipeline.transport import message_id_for, news_queue
from tests.fixtures import VALID_RSS, single_item_rss

FEEDS = {
    "https://ria.test/rss": single_item_rss("Новость РИА", "https://ria.test/1.html"),
    "https://tass.test/rss": single_item_rss("Новость ТАСС", "https://tass.test/1.html"),
    "https://kommersant.test/rss": single_item_rss("Новость Ъ", "https://kommersant.test/1.html"),
}


@dataclass
class MonitorEnv:
    broker: RabbitBroker
    store: SeenStore
    http: httpx.AsyncClient
    feeds: Dict[str, str]
    received: List[NewsItem] = field(default_factory=list)

    async def poll(self) -> int:
        return await poll_all_feeds(self.broker, self.store, self.http)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    feeds = dict(FEEDS)
    monkeypatch.setattr(settings, "feed_urls", ",".join(feeds))

    def serve(request: httpx.Request) -> httpx.Response:
        body = feeds.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body.encode())

    broker = RabbitBroker()
    store = SeenStore(str(tmp_path / "seen.sqlite"))
    await store.open()
    http = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    test_env = MonitorEnv(broker=broker, store=store, http=http, feeds=feeds)

    @broker.subscriber(news_queue())
    async def collect(item: NewsItem) -> None:
        test_env.received.append(item)

    async with TestRabbitBroker(broker) as test_broker:
        test_env.broker = test_broker
        yield test_env

    await http.aclose()
    await store.close()


async def test_poll_publishes_all_feeds_then_nothing(env):
    assert await env.poll() == 3
    assert {i.source for i in env.received} == {"ria_test", "tass_test", "kommersant_test"}

    assert await env.poll() == 0
    assert len(env.received) == 3


async def test_seen_state_survives_store_reopen(env, tmp_path):
    assert await env.poll() == 3
    await env.store.close()

    reopened = SeenStore(env.store.db_path)
    await reopened.open()
    try:
        assert await poll_all_feeds(env.broker, reopened, env.http) == 0
    finally:
        await reopened.close()


async def test_duplicate_links_within_feed_published_once(env):
    env.feeds.clear()
    env.feeds["https://ria.test/rss"] = VALID_RSS

    assert await env.poll() == 2
    assert len(env.received) == 2


async def test_failed_feed_does_not_block_others(env):
    env.feeds.pop("https://tass.test/rss")
    env.feeds["https://kommersant.test/rss"] = "<rss><channel><item><title>broken"

    assert await env.poll() == 1
    assert [i.source for i in env.received] == ["ria_test"]


async def test_failed_publish_is_not_marked_seen(env, monkeypatch):
    real_publish = publisher.publish_item

    async def flaky_publish(broker, item):
        if "tass" in str(item.link):
            raise ConnectionError("broker unavailable")
        await real_publish(broker, item)

    monkeypatch.setattr(publisher, "publish_item", flaky_publish)
    assert await env.poll() == 2
    assert not await env.store.is_seen("tass_test", "https://tass.test/1.html")

    monkeypatch.setattr(publisher, "publish_item", real_publish)
    assert await env.poll() == 1
    assert [i.source for i in env.received].count("tass_test") == 1


async def test_published_message_is_persistent_json(env, monkeypatch):
    calls = []
    real = env.broker.publish

    async def spy(message, **kwargs):
        calls.append((message, kwargs))
        return await real(message, **kwargs)

    monkeypatch.setattr(env.broker, "publish", spy)
    await env.poll()

    message, kwargs = calls[0]
    assert isinstance(message, NewsItem)
    assert kwargs["persist"] is True
    assert kwargs["message_id"] == message_id_for(str(message.link))
    assert kwargs["queue"].name == settings.rabbitmq_queue
    assert kwargs["queue"].durable is True


@pytest.fixture
async def api(env):
    monitor_app.app.state.broker = env.broker
    monitor_app.app.state.seen_store = env.store
    monitor_app.app.state.http = env.http
    monitor_app.app.state.poll_lock = asyncio.Lock()
    transport = httpx.ASGITransport(app=monitor_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_health(api):
    resp = await api.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_refresh_counts_then_zero(api):
    first = await api.post("/refresh")
    assert first.status_code == 200
    assert first.json() == {"new_items": 3}

    second = await api.post("/refresh")
    assert second.json() == {"new_items": 0}


async def test_concurrent_refreshes_are_serialized(api, monkeypatch):
    active = 0
    max_active = 0
    real_poll = monitor_app.poll_all_feeds

    async def tracking_poll(*args):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        try:
            return await real_poll(*args)
        finally:
            active -= 1

    monkeypatch.setattr(monitor_app, "poll_all_feeds", tracking_poll)
    responses = await asyncio.gather(*(api.post("/refresh") for _ in range(3)))

    assert max_active == 1
    assert sorted(r.json()["new_items"] for r in responses) == [0, 0, 3]
