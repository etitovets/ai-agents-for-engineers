"""SpaCy NER processing for the Worker.

The Russian SpaCy model is loaded once at Worker startup (never per-message).
CPU-heavy inference runs in a worker thread via asyncio.to_thread so it does
not block the async event loop.

Entity occurrences are aggregated by (entity_text, label) within a single
article and the occurrence count is preserved.

Truncation policy: title and text are joined with a newline and cut to
``max_chars`` characters before inference.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")

Entity = Tuple[str, str, int]  # (entity_text, label, count)


def normalize_entity_text(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


class NERProcessor:
    """Wraps a SpaCy NER pipeline loaded once per process."""

    def __init__(self, model_name: str, max_chars: int, nlp: Optional[Any] = None) -> None:
        self.model_name = model_name
        self.max_chars = max_chars
        self._nlp = nlp

    def load(self) -> None:
        """Load the SpaCy model. Called once at Worker startup."""
        import spacy

        logger.info("Loading SpaCy model %s ...", self.model_name)
        self._nlp = spacy.load(self.model_name)
        logger.info("SpaCy model %s loaded", self.model_name)

    def extract_sync(self, title: str, text: str) -> List[Entity]:
        if self._nlp is None:
            raise RuntimeError("NER model not loaded")
        combined = f"{title}\n{text}"[: self.max_chars]
        if not combined.strip():
            return []
        doc = self._nlp(combined)
        counts: dict[Tuple[str, str], int] = {}
        for ent in doc.ents:
            entity_text = normalize_entity_text(ent.text)
            if not entity_text:
                continue
            key = (entity_text, ent.label_)
            counts[key] = counts.get(key, 0) + 1
        return [(entity_text, label, count) for (entity_text, label), count in counts.items()]

    async def extract(self, title: str, text: str) -> List[Entity]:
        """Run NER on title + text without blocking the event loop."""
        return await asyncio.to_thread(self.extract_sync, title, text)
