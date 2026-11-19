"""RSS feed fetching and parsing utilities.

Feeds are downloaded with httpx (explicit timeout and User-Agent) and parsed
with feedparser. Each feed entry is parsed into a NewsItem. Malformed entries
are skipped with logging rather than crashing the whole poll. Text is taken from
a usable content/summary/description field; no full-article scraping is done.

Date policy: if a usable publication date is present it is converted to a
timezone-aware UTC datetime. If no date can be parsed, the current poll time
(UTC) is used and a warning is logged. Missing dates never block publication.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Union
from urllib.parse import urlparse

import feedparser
import httpx

from news_pipeline.config import settings
from news_pipeline.schemas import NewsItem

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_UNSAFE_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


class FeedFetchError(Exception):
    """A feed could not be downloaded (timeout, HTTP error, network error)."""


def normalize_whitespace(text: Optional[str]) -> str:
    """Collapse whitespace and strip, preserving Unicode (including Russian)."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


def strip_html(text: Optional[str]) -> str:
    """Strip HTML markup (dropping script/style blocks) and unescape entities."""
    if not text:
        return ""
    text = _UNSAFE_BLOCK_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return normalize_whitespace(text)


def _parse_date(entry: feedparser.FeedParserDict) -> datetime:
    """Best-effort publication date -> timezone-aware UTC datetime.

    Falls back to now(UTC) when no date is parseable (documented policy).
    """
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
    logger.warning("No parseable publication date; using poll time (UTC)")
    return datetime.now(timezone.utc)


def _extract_text(entry: feedparser.FeedParserDict) -> str:
    """Take text from content/summary/description, stripping HTML."""
    content = getattr(entry, "content", None)
    if content:
        for item in content:
            value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
            if value:
                return strip_html(value)
    for attr in ("summary", "description", "title"):
        value = getattr(entry, attr, None)
        if value:
            return strip_html(value)
    return ""


async def fetch_feed(client: httpx.AsyncClient, url: str) -> bytes:
    """Download a feed body. Raises FeedFetchError on any transport/HTTP failure."""
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise FeedFetchError(f"timeout fetching {url}") from exc
    except httpx.HTTPStatusError as exc:
        raise FeedFetchError(f"HTTP {exc.response.status_code} fetching {url}") from exc
    except httpx.HTTPError as exc:
        raise FeedFetchError(f"{exc.__class__.__name__} fetching {url}") from exc
    return response.content


def make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_request_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    )


def parse_feed(content: Union[bytes, str], url: str, source: str) -> List[NewsItem]:
    """Parse one RSS document into NewsItem objects, skipping malformed entries."""
    items: List[NewsItem] = []
    parsed = feedparser.parse(content)

    if getattr(parsed, "bozo", 0):
        logger.warning(
            "Feed %s may be malformed (%s); continuing with available entries",
            url,
            getattr(parsed, "bozo_exception", "unknown error"),
        )

    entries = getattr(parsed, "entries", []) or []
    for entry in entries:
        try:
            link = normalize_whitespace(getattr(entry, "link", None) or "")
            if not link:
                logger.warning("Skipping entry without a usable link in feed %s", url)
                continue

            title = strip_html(getattr(entry, "title", None) or "")
            if not title:
                logger.warning("Skipping entry without a title in feed %s (link=%s)", url, link)
                continue

            text = _extract_text(entry) or title
            guid = getattr(entry, "id", None) or getattr(entry, "guid", None)

            items.append(
                NewsItem(
                    source=source,
                    title=title,
                    link=link,
                    published_at=_parse_date(entry),
                    text=text,
                    guid=str(guid) if guid else None,
                )
            )
        except Exception:
            logger.exception("Skipping malformed entry in feed %s", url)

    logger.info("Feed %s: %d entries, %d usable", url, len(entries), len(items))
    return items


def iter_feed_urls() -> Iterable[tuple[str, str]]:
    """Yield (url, source) pairs from the configured FEED_URLS setting."""
    for raw in settings.feed_urls.split(","):
        url = raw.strip()
        if url:
            yield url, source_from_url(url)


def source_from_url(url: str) -> str:
    """Derive a short source name from a feed URL, e.g. 'ria_ru'."""
    host = urlparse(url).hostname or "unknown"
    return host.removeprefix("www.").replace(".", "_")
