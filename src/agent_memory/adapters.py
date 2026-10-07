"""Host integration protocols; providers and network activity are never implicit."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Protocol
from .records import Record, derive
from .store import Store


class Source(Protocol):
    def records(self) -> Iterable[Record]: ...


@dataclass(frozen=True)
class ModelOutput:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None


class Model(Protocol):
    def summarize(self, texts: tuple[str, ...]) -> ModelOutput: ...


def ingest(store: Store, source: Source) -> tuple[Record, ...]:
    """Each record is atomic; the batch is not an all-or-nothing transaction."""
    return tuple(store.put(record) for record in source.records())


def summarize(store: Store, parents: list[Record], model: Model) -> tuple[Record, ModelOutput]:
    """Explicit model invocation. A caller owns consent, credentials and budget.

    The result is a candidate, not persisted. Revalidate provenance before use.
    External summaries retain source trust and intersected permissions.
    """
    if not parents or any(store.get(p.id, namespace=p.namespace) != p for p in parents):
        raise ValueError('model inputs must be current persisted sources')
    result = model.summarize(tuple(p.text for p in parents))
    return derive(result.text, parents, source='model-summary'), result
