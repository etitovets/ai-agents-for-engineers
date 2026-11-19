"""Shared Pydantic message schema for the news pipeline.

This contract is used by both the Monitor (publisher) and the Worker (consumer)
without either service importing the other's application internals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, HttpUrl


class NewsItem(BaseModel):
    """A single news article published from the Monitor to the Worker.

    Required fields: source, title, link, published_at, text.
    An optional stable entry id (guid) is carried for diagnostics.
    """

    source: str
    title: str
    link: HttpUrl
    published_at: datetime
    text: str
    guid: Optional[str] = None
