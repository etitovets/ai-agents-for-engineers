"""RabbitMQ publisher for the Monitor.

Publishes persistent JSON messages to a durable queue through FastStream's
RabbitBroker. The channel uses publisher confirms, so ``broker.publish`` only
returns after RabbitMQ has accepted the message.
"""

from __future__ import annotations

import logging
from typing import List

from faststream.rabbit import RabbitBroker

from news_pipeline.schemas import NewsItem
from news_pipeline.transport import message_id_for, news_queue

logger = logging.getLogger(__name__)


async def declare_news_queue(broker: RabbitBroker) -> None:
    """Declare the durable queue so publishing works before the Worker starts."""
    await broker.declare_queue(news_queue())


async def publish_item(broker: RabbitBroker, item: NewsItem) -> None:
    """Publish one item; raises if RabbitMQ does not confirm it."""
    await broker.publish(
        item,
        queue=news_queue(),
        persist=True,
        message_id=message_id_for(str(item.link)),
    )


async def publish_items(broker: RabbitBroker, items: List[NewsItem]) -> List[NewsItem]:
    """Publish items one by one; returns the items RabbitMQ confirmed."""
    confirmed: List[NewsItem] = []
    for item in items:
        try:
            await publish_item(broker, item)
        except Exception:
            logger.exception("Failed to publish link=%s", item.link)
            continue
        confirmed.append(item)
    return confirmed
