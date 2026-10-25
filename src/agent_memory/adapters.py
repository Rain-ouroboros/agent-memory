"""Host integration protocols; providers and network activity are never implicit."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Iterable, Protocol
import time
from .records import Record, derive, resolve_evidence, _finite_number, _integer_bound
from .store import Store, ConflictError


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


@dataclass(frozen=True)
class IngestReceipt:
    inserted: tuple[tuple[str, int], ...]
    duplicates: tuple[tuple[str, int], ...]
    conflicts: tuple[tuple[str, int], ...]


def ingest_batch(store: Store, source: Source) -> IngestReceipt:
    """Insert independently, reporting duplicates and conflicts without updates.

    Comparison ignores only revision and created_at. Receipt entries preserve
    source order and occurrences within each category. Source exceptions escape;
    earlier writes remain committed, but no receipt is returned in that case.
    """
    inserted = []
    duplicates = []
    conflicts = []
    for record in source.records():
        if not isinstance(record, Record):
            raise ValueError('source elements must be Record instances')
        try:
            saved = store.put(record, expected_revision=0)
        except ConflictError:
            current = store.get(record.id, namespace=record.namespace)
            if current is None:
                raise
            identical = (replace(current, revision=0, created_at=0)
                         == replace(record, revision=0, created_at=0))
            target = duplicates if identical else conflicts
            target.append((current.id, current.revision))
        else:
            inserted.append((saved.id, saved.revision))
    return IngestReceipt(tuple(inserted), tuple(duplicates), tuple(conflicts))


def summarize(store: Store, parents: list[Record], model: Model, *,
              now: float | None = None, max_nodes: int = 4096,
              max_depth: int = 32) -> tuple[Record, ModelOutput]:
    """Explicit invocation on a fixed snapshot of eligible persisted parents.

    Inactive, expired or incomplete provenance is rejected before calling the
    model. No store lock spans the callback. The returned candidate is not
    persisted; its exact source revisions must be revalidated before use.
    A caller owns consent, credentials and budget.
    """
    clock = time.time() if now is None else now
    _finite_number(clock, 'now')
    _integer_bound(max_nodes, 'max_nodes', minimum=1)
    _integer_bound(max_depth, 'max_depth')
    if not isinstance(parents, (list, tuple)):
        raise ValueError('parents must be a list or tuple')
    frozen = tuple(parents)
    if not frozen or any(not isinstance(p, Record) for p in frozen):
        raise ValueError('parents must contain records')
    if len({p.namespace for p in frozen}) != 1:
        raise ValueError('parents must share a namespace')
    records = store.snapshot(namespace=frozen[0].namespace)
    for parent in frozen:
        if parent.revision < 1 or records.get(parent.id) != parent:
            raise ValueError('model inputs must be current persisted sources')
        evidence = resolve_evidence(parent, records, now=clock,
                                    max_nodes=max_nodes, max_depth=max_depth)
        if not evidence.complete:
            raise ValueError('model inputs must have complete active provenance')
    # Validate the prospective root, not just each parent's separate graph.
    # Keep its temporary identity distinct from every persisted record.
    anchor = 'summary-preflight'
    prospective = derive('', frozen, source='model-summary', anchor=anchor)
    while prospective.id in records:
        anchor += ':'
        prospective = derive('', frozen, source='model-summary', anchor=anchor)
    evidence = resolve_evidence(prospective, records, now=clock,
                                max_nodes=max_nodes, max_depth=max_depth)
    if not evidence.complete:
        raise ValueError('prospective summary must have complete active provenance within limits')
    result = model.summarize(tuple(p.text for p in frozen))
    if not isinstance(result, ModelOutput):
        raise ValueError('model result must be ModelOutput')
    if not isinstance(result.text, str) or not result.text.strip():
        raise ValueError('model result text must be non-empty')
    for field in ('input_tokens', 'output_tokens'):
        value = getattr(result, field)
        if value is not None:
            _integer_bound(value, field)
    if result.cost is not None:
        _finite_number(result.cost, 'cost')
        if result.cost < 0:
            raise ValueError('cost must be non-negative')
    return derive(result.text, frozen, source='model-summary'), result
