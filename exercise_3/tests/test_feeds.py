"""Offline tests for RSS fetching and parsing."""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest

from news_pipeline.monitor.feeds import (
    FeedFetchError,
    fetch_feed,
    normalize_whitespace,
    parse_feed,
    source_from_url,
    strip_html,
)
from tests.fixtures import EMPTY_RSS, RSS_MALFORMED_XML, RSS_MISSING_FIELDS, VALID_RSS


def test_normalize_whitespace_preserves_russian():
    assert normalize_whitespace("  Привет,\n\t мир!  ") == "Привет, мир!"
    assert normalize_whitespace(None) == ""


def test_strip_html_removes_tags_and_scripts():
    assert strip_html("<p>Привет <b>мир</b></p><script>alert(1)</script>") == "Привет мир"
    assert strip_html("Газпром &amp; Роснефть") == "Газпром & Роснефть"
    assert strip_html(None) == ""


def test_parse_valid_feed():
    items = parse_feed(VALID_RSS.encode(), "url", "ria_ru")

    assert len(items) == 3
    first = items[0]
    assert first.title == "Заголовок новости на русском"
    assert str(first.link) == "https://ria.ru/2024/01/15/article1.html"
    assert first.text == "Описание новости с HTML разметкой"
    assert first.guid == "ria-1"
    assert first.published_at.tzinfo == timezone.utc
    assert first.published_at.hour == 9  # +0300 converted to UTC
    assert str(items[1].link) == "https://tass.ru/2024/01/15/article2.html?id=42"


def test_missing_fields_skip_entries_and_default_date():
    items = parse_feed(RSS_MISSING_FIELDS, "url", "src")

    assert [i.title for i in items] == ["Нет даты публикации"]
    assert items[0].published_at.tzinfo is not None


def test_malformed_xml_keeps_parseable_entries():
    items = parse_feed(RSS_MALFORMED_XML, "url", "src")
    assert [i.title for i in items] == ["Хорошая новость"]


def test_empty_feed():
    assert parse_feed(EMPTY_RSS, "url", "src") == []


def test_source_from_url():
    assert source_from_url("https://www.kommersant.ru/RSS/news.xml") == "kommersant_ru"
    assert source_from_url("https://tass.ru/rss/v2.xml") == "tass_ru"


async def test_fetch_feed_sends_user_agent():
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, content=VALID_RSS.encode())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "TestAgent/1.0"}
    ) as client:
        body = await fetch_feed(client, "https://ria.ru/rss")

    assert body.startswith(b"<?xml")
    assert seen_headers["user-agent"] == "TestAgent/1.0"


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503),
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down", request=request)),
    ],
    ids=["http-error", "timeout", "connect-error"],
)
async def test_fetch_feed_failures_raise_feed_fetch_error(handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FeedFetchError):
            await fetch_feed(client, "https://ria.ru/rss")
