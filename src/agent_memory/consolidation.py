"""Deterministic near-duplicate proposals, adapted from Rain (MIT)."""
from __future__ import annotations
import re
from typing import Any
from .records import Record, derive, resolve_evidence
from .store import Store
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
                          max_records: int = 500) -> tuple[Record, ...]:
    """Return unpersisted candidates; never retire primary speech or assert truth.

    Only derived, trusted, valid records sharing the same audience are grouped.
    Jaccard overlap can miss negation; a host must review each proposal.
    """
    snapshot = store.snapshot(namespace=namespace)
    candidates = []
    for record in snapshot.values():
        ev = resolve_evidence(record, snapshot, now=now)
        if record.derived and ev.complete and ev.trust == 'trusted' and record.source != 'consolidation':
            candidates.append(record)
    if len(candidates) > max_records:
        raise ValueError('consolidation candidate limit exceeded')
    groups: dict[tuple[str, ...], list[Record]] = {}
    for record in candidates:
        groups.setdefault(resolve_evidence(record, snapshot, now=now).audience, []).append(record)
    proposals = []
    for pool in groups.values():
        rows = [record.to_dict() for record in pool]
        by_id = {record.id: record for record in pool}
        for cluster in _cluster(rows):
            members = [by_id[row['id']] for row in cluster]
            representative = max(members, key=lambda record: (record.confidence, len(record.text), record.id))
            # Membership and source revisions participate in the candidate ID.
            anchor = '|'.join(f'{record.id}:{record.revision}' for record in members)
            proposals.append(derive(representative.text, members, source='consolidation',
                                    anchor=anchor, kind=representative.kind, created_at=now))
    return tuple(proposals)
