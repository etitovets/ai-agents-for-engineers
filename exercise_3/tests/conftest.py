"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from news_pipeline.schemas import NewsItem


@pytest.fixture
def news_item() -> NewsItem:
    return NewsItem(
        source="ria_ru",
        title="Путин провёл переговоры в Москве",
        link="https://ria.ru/20240115/article.html",
        published_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        text="Владимир Путин встретился с главой Газпрома в Москве.",
    )
