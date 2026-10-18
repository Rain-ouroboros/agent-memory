"""Explicit records, provenance and access policy. No agent identity is assumed."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math
import time
from typing import Literal

Trust = Literal['trusted', 'unknown', 'untrusted']
Status = Literal['active', 'retired']


def _finite_number(value: object, field: str) -> None:
    if type(value) not in (int, float):
        raise ValueError(f'{field} must be a finite number')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f'{field} must be a finite number')


def _integer_bound(value: object, field: str, *, minimum: int = 0,
                   maximum: int | None = None) -> None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{field} must be an integer within bounds')


@dataclass(frozen=True)
class SourceRef:
    id: str
    revision: int


@dataclass(frozen=True)
class Record:
    id: str
    text: str
    namespace: str = 'default'
    source: str = 'manual'
    author: str | None = None
    kind: str = 'fact'
    trust: Trust = 'unknown'
    audience: tuple[str, ...] = ()
    sources: tuple[SourceRef, ...] = ()
    derived: bool = False
    complete: bool = True
    confidence: float = 1.0
    created_at: float = 0.0
    occurred_at: float | None = None
    expires_at: float | None = None
    status: Status = 'active'
    revision: int = 0

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id or not isinstance(self.namespace, str) or not self.namespace or not isinstance(self.text, str):
            raise ValueError('id, namespace and text are required')
        if type(self.complete) is not bool or type(self.derived) is not bool:
            raise ValueError('complete and derived must be booleans')
        if self.author is not None and not isinstance(self.author, str):
            raise ValueError('author must be text or unknown')
        if not isinstance(self.source, str) or not self.source or not isinstance(self.kind, str) or not self.kind:
            raise ValueError('source and kind must be non-empty strings')
        if self.trust not in ('trusted', 'unknown', 'untrusted'):
            raise ValueError('invalid trust')
        if self.status not in ('active', 'retired'):
            raise ValueError('invalid status')
        for field in ('confidence', 'created_at', 'occurred_at', 'expires_at'):
            value = getattr(self, field)
            if value is None and field in ('occurred_at', 'expires_at'):
                continue
            _finite_number(value, field)
        if not 0 <= self.confidence <= 1:
            raise ValueError('confidence must be within [0, 1]')
        if not isinstance(self.audience, (list, tuple)):
            raise ValueError('audience must be a list or tuple')
        if not isinstance(self.sources, (list, tuple)):
            raise ValueError('sources must be a list or tuple')
        if any(not isinstance(a, str) or not a.strip() for a in self.audience):
            raise ValueError('audience must contain explicit non-empty identifiers')
        if '*' in self.audience and len(self.audience) != 1:
            raise ValueError('wildcard audience must stand alone')
        if type(self.revision) is not int or self.revision < 0 or any(not isinstance(ref, SourceRef) or type(ref.revision) is not int or ref.revision < 1 or not isinstance(ref.id, str) or not ref.id for ref in self.sources):
            raise ValueError('invalid revision')
        object.__setattr__(self, 'audience', tuple(sorted(set(self.audience))))
        object.__setattr__(self, 'sources', tuple(self.sources))

    @classmethod
    def create(cls, text: str, *, namespace: str = 'default', source: str = 'manual',
               anchor: str = '', **kwargs) -> Record:
        # Full content, including a late suffix, participates in the identity.
        identity = json.dumps([namespace, source, anchor, text], ensure_ascii=False)
        return cls(id=sha256(identity.encode()).hexdigest(), text=text,
                   namespace=namespace, source=source, created_at=kwargs.pop('created_at', time.time()), **kwargs)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Record:
        if not isinstance(data, dict):
            raise ValueError('record data must be a dictionary')
        payload = dict(data)
        sources = payload.get('sources', [])
        audience = payload.get('audience', [])
        if not isinstance(sources, (list, tuple)):
            raise ValueError('sources must be a list or tuple')
        if not isinstance(audience, (list, tuple)):
            raise ValueError('audience must be a list or tuple')
        refs = []
        for ref in sources:
            if not isinstance(ref, dict) or set(ref) != {'id', 'revision'}:
                raise ValueError('sources must contain id/revision dictionaries')
            if not isinstance(ref['id'], str) or not ref['id'] or type(ref['revision']) is not int or ref['revision'] < 1:
                raise ValueError('sources contain an invalid id or revision')
            refs.append(SourceRef(ref['id'], ref['revision']))
        payload['sources'] = tuple(refs)
        payload['audience'] = audience
        return cls(**payload)


def meet_audience(audiences: list[tuple[str, ...]]) -> tuple[str, ...]:
    """Intersection; '*' is unrestricted, an empty audience is private."""
    result: set[str] | None = None
    for audience in audiences:
        if '*' in audience:
            continue
        result = set(audience) if result is None else result.intersection(audience)
    return tuple(sorted(result)) if result is not None else ('*',)


@dataclass(frozen=True)
class Evidence:
    complete: bool
    trust: Trust
    audience: tuple[str, ...]
    witnesses: tuple[str, ...]
    issues: tuple[str, ...]


def resolve_evidence(record: Record, records: dict[str, Record], *, now: float,
                     max_nodes: int = 4096, max_depth: int = 32) -> Evidence:
    """Revalidate revision-bound provenance, including every ancestor's policy."""
    _finite_number(now, 'now')
    _integer_bound(max_nodes, 'max_nodes', minimum=1)
    _integer_bound(max_depth, 'max_depth')
    issues: set[str] = set()
    witnesses: set[str] = set()
    visited: set[str] = set()
    deepest: dict[str, int] = {}
    trusts: list[str] = []
    audiences: list[tuple[str, ...]] = []

    def walk(node: Record, path: frozenset[str], depth: int):
        if node.id in path:
            issues.add('cycle'); return
        if depth > max_depth:
            issues.add('limit'); return
        if node.id not in visited:
            if len(visited) >= max_nodes:
                issues.add('limit'); return
            visited.add(node.id)
            trusts.append(node.trust); audiences.append(node.audience)
        elif depth <= deepest[node.id]:
            return
        # A longer route to a shared ancestor can exceed the depth budget
        # even when a shorter route has already validated its policy.
        deepest[node.id] = depth
        if node.status != 'active' or (node.expires_at is not None and now >= node.expires_at):
            issues.add('inactive')
        if not node.complete:
            issues.add('incomplete')
        if node.derived and not node.sources:
            issues.add('missing_provenance')
        if not node.sources and not node.derived:
            witnesses.add(node.id)
        for ref in node.sources:
            parent = records.get(ref.id)
            if parent is None:
                issues.add('missing_source'); continue
            if parent.namespace != record.namespace:
                issues.add('namespace'); continue
            if parent.revision != ref.revision:
                issues.add('changed_source')
            walk(parent, path | {node.id}, depth + 1)
    walk(record, frozenset(), 0)
    complete = not issues
    trust: Trust = 'untrusted' if 'untrusted' in trusts else ('unknown' if not complete or 'unknown' in trusts else 'trusted')
    return Evidence(complete, trust, meet_audience(audiences) if complete else (),
                    tuple(sorted(witnesses)), tuple(sorted(issues)))


def derive(text: str, parents: list[Record], *, source: str = 'derived', **kwargs) -> Record:
    if not parents or any(p.revision < 1 for p in parents):
        raise ValueError('persist sources before deriving a record')
    if len({p.namespace for p in parents}) != 1:
        raise ValueError('sources must share a namespace')
    trust: Trust = 'untrusted' if any(p.trust == 'untrusted' for p in parents) else ('unknown' if any(p.trust == 'unknown' for p in parents) else 'trusted')
    return Record.create(text, namespace=parents[0].namespace, source=source,
                         sources=tuple(SourceRef(p.id, p.revision) for p in parents), derived=True,
                         audience=meet_audience([p.audience for p in parents]), trust=trust,
                         confidence=min(p.confidence for p in parents), **kwargs)
