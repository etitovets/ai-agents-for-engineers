"""Shared RabbitMQ transport contract used by both the Monitor and the Worker."""

from __future__ import annotations

import hashlib

from faststream.rabbit import RabbitQueue

from news_pipeline.config import settings


def news_queue() -> RabbitQueue:
    """Durable queue carrying NewsItem messages."""
    return RabbitQueue(settings.rabbitmq_queue, durable=True)


def message_id_for(link: str) -> str:
    """Deterministic AMQP message_id, so redeliveries of one article share an id.

    FastStream's bounded retry counter is keyed by message_id.
    """
    return hashlib.sha256(link.encode("utf-8")).hexdigest()
