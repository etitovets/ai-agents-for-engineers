"""Worker tests: NER aggregation, ack/reject policy, FastStream handler wiring."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from faststream.exceptions import RejectMessage
from faststream.rabbit import TestRabbitBroker
from faststream.rabbit.message import RabbitMessage
from pydantic import ValidationError

from news_pipeline.schemas import NewsItem
from news_pipeline.transport import news_queue
from news_pipeline.worker import app as worker_app
from news_pipeline.worker.ner import NERProcessor


class FakeEntity:
    def __init__(self, text: str, label: str) -> None:
        self.text = text
        self.label_ = label


class FakeDoc:
    def __init__(self, ents):
        self.ents = ents


class FakeNlp:
    """Deterministic NER: tags every known word found in the input."""

    LEXICON = {"Путин": "PER", "Газпром": "ORG", "Москва": "LOC"}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> FakeDoc:
        self.calls.append(text)
        ents = []
        for token in text.replace("\n", " ").split(" "):
            word = token.strip(".,")
            if word in self.LEXICON:
                ents.append(FakeEntity(word, self.LEXICON[word]))
        return FakeDoc(ents)


class FakeRepo:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[NewsItem, list]] = []

    async def insert_news_and_entities(self, item, entities) -> bool:
        self.calls.append((item, entities))
        if self.error:
            raise self.error
        return True


def make_ner(nlp=None, max_chars: int = 10_000) -> NERProcessor:
    return NERProcessor("fake", max_chars, nlp=nlp or FakeNlp())


async def test_ner_runs_on_title_and_text_and_aggregates_counts():
    ner = make_ner()
    entities = await ner.extract("Путин и Газпром", "Путин  приехал в Москва.")

    assert sorted(entities) == [("Газпром", "ORG", 1), ("Москва", "LOC", 1), ("Путин", "PER", 2)]


async def test_ner_normalizes_entity_whitespace():
    class SpacedNlp:
        def __call__(self, text):
            return FakeDoc(
                [FakeEntity("Владимир\n  Путин", "PER"), FakeEntity("Владимир Путин", "PER")]
            )

    entities = await make_ner(SpacedNlp()).extract("t", "x")
    assert entities == [("Владимир Путин", "PER", 2)]


async def test_ner_truncates_input():
    nlp = FakeNlp()
    await make_ner(nlp, max_chars=10).extract("Заголовок", "очень длинный текст статьи")
    assert len(nlp.calls[0]) == 10


async def test_ner_empty_input_skips_model():
    nlp = FakeNlp()
    assert await make_ner(nlp).extract("", " ") == []
    assert nlp.calls == []


def test_ner_with_real_russian_model():
    pytest.importorskip("ru_core_news_sm")
    ner = NERProcessor("ru_core_news_sm", 10_000)
    ner.load()
    labels = {label for _, label, _ in ner.extract_sync("Путин в Москве", "Глава Газпрома.")}
    assert {"PER", "LOC", "ORG"} <= labels


def test_message_validation():
    with pytest.raises(ValidationError):
        NewsItem.model_validate({"source": "x", "title": "t", "link": "not-a-url", "text": "a"})


async def test_process_news_stores_entities(news_item):
    repo = FakeRepo()
    assert await worker_app.process_news(news_item, make_ner(), repo) is True
    item, entities = repo.calls[0]
    assert item == news_item
    assert ("Путин", "PER", 2) in entities


async def test_process_news_ner_failure_rejects_without_db_write(news_item):
    class BrokenNlp:
        def __call__(self, text):
            raise RuntimeError("model crashed")

    repo = FakeRepo()
    with pytest.raises(RejectMessage):
        await worker_app.process_news(news_item, make_ner(BrokenNlp()), repo)
    assert repo.calls == []


async def test_process_news_db_failure_propagates_for_redelivery(news_item):
    repo = FakeRepo(error=ConnectionError("db down"))
    with pytest.raises(ConnectionError):
        await worker_app.process_news(news_item, make_ner(), repo)


@pytest.fixture
async def worker_broker(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(worker_app.state, "ner", make_ner())
    monkeypatch.setattr(worker_app.state, "repo", repo)
    async with TestRabbitBroker(worker_app.broker) as broker:
        yield broker, repo


async def test_subscriber_consumes_typed_message(worker_broker, news_item):
    broker, repo = worker_broker
    await broker.publish(news_item, queue=news_queue())

    assert len(repo.calls) == 1
    assert repo.calls[0][0] == news_item
    worker_app.handle_news.mock.assert_called_once()


async def test_subscriber_rejects_invalid_payload(worker_broker, monkeypatch):
    broker, repo = worker_broker
    rejected: list[str] = []

    async def record_reject(self, **kwargs):
        rejected.append(self.message_id)

    monkeypatch.setattr(RabbitMessage, "reject", record_reject)
    await broker.publish(
        {"source": "x", "title": "t", "published_at": datetime.now(timezone.utc).isoformat()},
        queue=news_queue(),
        message_id="bad-1",
    )
    assert rejected == ["bad-1"]
    assert repo.calls == []
