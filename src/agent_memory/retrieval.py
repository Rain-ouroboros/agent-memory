"""Inspectable lexical recall and bounded, evidence-bound graph expansion."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import time

from .excerpt import query_excerpt
from .records import Evidence, Record, resolve_evidence
from .store import Store
from .index import SearchIndex
from .terms import content_term_patterns, required_matches


@dataclass(frozen=True)
class Hit:
    record: Record
    score: float
    evidence: Evidence
    routes: tuple[str, ...]
    excerpt: str


@dataclass(frozen=True)
class Recall:
    hits: tuple[Hit, ...]
    scanned: int
    eligible: int
    excluded: dict[str, int]
    namespace: str
    principal: str
    now: float
    # Exact source revisions let a consumer bind evaluation to this snapshot.
    revisions: tuple[tuple[str, int], ...]


DECAY_DAYS = {'event': 7, 'decision': 90, 'preference': 90, 'belief': 365, 'fact': 365}


def rank(record: Record, overlap: int, now: float) -> float:
    """Lexical overlap + exponential confidence + hyperbolic recency.

    The time constant is an e-folding period, not a statistical half-life.
    Confidence and trust are independent; neither asserts factual truth.
    """
    age = max(0.0, now - record.created_at) / 86400
    period = DECAY_DAYS.get(record.kind, 365)
    return overlap + .6 * record.confidence * math.exp(-age / period) + .4 * period / (period + age)


def recall(store: Store, query: str, *, principal: str, namespace: str = 'default',
           limit: int = 10, now: float | None = None, graph_depth: int = 2,
           graph_fanout: int = 25, graph_max_nodes: int = 256, excerpt_chars: int = 1200,
           index: SearchIndex | None = None) -> Recall:
    if not principal or principal == '*':
        raise ValueError('principal must identify a concrete audience')
    if limit < 0 or not 0 <= graph_depth <= 8 or graph_fanout < 1 or graph_max_nodes < 1:
        raise ValueError('invalid recall bounds')
    clock = time.time() if now is None else now
    if not math.isfinite(clock):
        raise ValueError('now must be finite')
    records = store.snapshot(namespace=namespace)
    excluded: dict[str, int] = {}
    eligible: dict[str, tuple[Record, Evidence]] = {}
    for record in records.values():
        evidence = resolve_evidence(record, records, now=clock)
        reason = None
        if not evidence.complete:
            reason = 'provenance'
        elif '*' not in evidence.audience and principal not in evidence.audience:
            reason = 'access'
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            eligible[record.id] = (record, evidence)
    patterns = content_term_patterns(query[:1024])
    patterns = dict(list(patterns.items())[:16])
    scores: dict[str, float] = {}
    routes: dict[str, set[str]] = {}
    if patterns:
        for key, (record, _) in eligible.items():
            overlap = sum(bool(pattern.search(record.text)) for pattern in patterns.values())
            if overlap >= required_matches(len(patterns)):
                scores[key] = rank(record, overlap, clock); routes[key] = {'lexical'}
    if index is not None:
        # Indexed hits are a shortlist, never permission or revision authority.
        for position, (key, revision, _) in enumerate(index.candidates(query, namespace=namespace), 1):
            if key not in eligible or eligible[key][0].revision != revision:
                continue
            scores[key] = scores.get(key, 0) + 1 / (60 + position)
            routes.setdefault(key, set()).add('fts5')
    if graph_depth and scores:
        # Only currently authorized, revision-matching evidence can bridge a path.
        adjacency: dict[str, list[tuple[str, str, float]]] = {}
        seeds: set[str] = set()
        for source, _, target, key, revision, weight in store.edges(namespace=namespace):
            if key not in eligible or eligible[key][0].revision != revision:
                continue
            adjacency.setdefault(source, []).append((target, key, weight))
            adjacency.setdefault(target, []).append((source, key, weight))
            if key in scores:
                seeds.update((source, target))
        queue = deque((seed, 0, 1.0) for seed in sorted(seeds)[:graph_max_nodes])
        visited = set(seed for seed, _, _ in queue)
        while queue:
            node, depth, path_weight = queue.popleft()
            if depth >= graph_depth:
                continue
            edges = sorted(adjacency.get(node, []), key=lambda x: (-x[2], x[0], x[1]))[:graph_fanout]
            for target, key, weight in edges:
                graph_score = path_weight * weight * (.6 ** depth)
                scores[key] = max(scores.get(key, 0), graph_score)
                routes.setdefault(key, set()).add('graph')
                if target not in visited and len(visited) < graph_max_nodes:
                    visited.add(target); queue.append((target, depth + 1, path_weight * weight))
    selected = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    hits = tuple(Hit(eligible[key][0], scores[key], eligible[key][1], tuple(sorted(routes[key])),
                     query_excerpt(eligible[key][0].text, query, excerpt_chars)) for key in selected)
    return Recall(hits, len(records), len(eligible), excluded, namespace, principal, clock,
                  tuple(sorted((key, record.revision) for key, (record, _) in eligible.items())))
