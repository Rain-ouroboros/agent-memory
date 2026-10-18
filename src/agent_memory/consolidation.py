"""Deterministic near-duplicate proposals, adapted from Rain (MIT)."""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any
from .records import (Record, SourceRef, derive, resolve_evidence,
                      _finite_number, _integer_bound)
from .store import Store


@dataclass(frozen=True)
class SkippedProposal:
    sources: tuple[SourceRef, ...]
    reason: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class ConsolidationReceipt:
    proposals: tuple[Record, ...]
    skipped: tuple[SkippedProposal, ...]

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_JACCARD_MIN = .7
_MIN_CLUSTER = 2

def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(str(text or '').lower()) if len(t) > 2}

def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0

def _cluster(atoms: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Greedy deterministic clustering by kind + token Jaccard."""
    pool = sorted(atoms, key=lambda a: str(a.get('id')))
    toks = {a['id']: _tokens(a.get('text')) for a in pool}
    used: set[str] = set()
    clusters: list[list[dict[str, Any]]] = []
    for i, a in enumerate(pool):
        if a['id'] in used:
            continue
        group = [a]
        used.add(a['id'])
        for b in pool[i + 1:]:
            if b['id'] in used or str(b.get('kind')) != str(a.get('kind')):
                continue
            if _jaccard(toks[a['id']], toks[b['id']]) >= _JACCARD_MIN:
                group.append(b)
                used.add(b['id'])
        if len(group) >= _MIN_CLUSTER:
            clusters.append(group)
    return clusters


def propose_consolidation(store: Store, *, namespace: str = 'default', now: float,
                          max_records: int = 500, max_nodes: int = 4096,
                          max_depth: int = 32, with_receipt: bool = False
                          ) -> tuple[Record, ...] | ConsolidationReceipt:
    """Return ready unpersisted candidates, or an explicit receipt when requested.

    Only derived, trusted, valid records sharing the same audience are grouped.
    Parents and prospective candidates use the same snapshot, clock and budgets.
    Jaccard overlap can miss negation; a host must review each proposal.
    No records are written or retired.
    """
    _finite_number(now, 'now')
    _integer_bound(max_records, 'max_records')
    _integer_bound(max_nodes, 'max_nodes', minimum=1)
    _integer_bound(max_depth, 'max_depth')
    if type(with_receipt) is not bool:
        raise ValueError('with_receipt must be boolean')
    snapshot = store.snapshot(namespace=namespace)
    candidates = []
    audiences = {}
    for record in snapshot.values():
        ev = resolve_evidence(record, snapshot, now=now,
                              max_nodes=max_nodes, max_depth=max_depth)
        if record.derived and ev.complete and ev.trust == 'trusted' and record.source != 'consolidation':
            candidates.append(record)
            audiences[record.id] = ev.audience
    if len(candidates) > max_records:
        raise ValueError('consolidation candidate limit exceeded')
    groups: dict[tuple[str, ...], list[Record]] = {}
    for record in candidates:
        groups.setdefault(audiences[record.id], []).append(record)
    proposals = []
    skipped = []
    for pool in groups.values():
        rows = [record.to_dict() for record in pool]
        by_id = {record.id: record for record in pool}
        for cluster in _cluster(rows):
            members = [by_id[row['id']] for row in cluster]
            representative = max(members, key=lambda record: (record.confidence, len(record.text), record.id))
            # Membership and source revisions participate in the candidate ID.
            anchor = '|'.join(f'{record.id}:{record.revision}' for record in members)
            candidate = derive(representative.text, members, source='consolidation',
                               anchor=anchor, kind=representative.kind, created_at=now)
            evidence = resolve_evidence(candidate, snapshot, now=now,
                                        max_nodes=max_nodes, max_depth=max_depth)
            if not evidence.complete:
                reason = 'provenance_limit' if 'limit' in evidence.issues else 'provenance_invalid'
                skipped.append(SkippedProposal(candidate.sources, reason, evidence.issues))
                continue
            proposals.append(candidate)
    ready = tuple(proposals)
    return ConsolidationReceipt(ready, tuple(skipped)) if with_receipt else ready
