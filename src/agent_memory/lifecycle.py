"""Explicit maintenance; retention is a status change, not physical erasure."""
from dataclasses import dataclass, replace
import time
from .records import _finite_number
from .store import Store, ConflictError


@dataclass(frozen=True)
class Maintenance:
    dry_run: bool
    candidates: tuple[tuple[str, int], ...]
    applied: tuple[tuple[str, int], ...]
    conflicted: tuple[tuple[str, int], ...] = ()


def maintain(store: Store, *, namespace: str = 'default', now: float | None = None,
             dry_run: bool = True) -> Maintenance:
    if type(dry_run) is not bool:
        raise ValueError('dry_run must be a boolean')
    clock = time.time() if now is None else now
    _finite_number(clock, 'now')
    candidates = [r for r in store.snapshot(namespace=namespace).values()
                  if r.status == 'active' and r.expires_at is not None and clock >= r.expires_at]
    applied = []
    conflicted = []
    if not dry_run:
        for record in candidates:
            try:
                saved = store.put(replace(record, status='retired'))
            except ConflictError:
                conflicted.append((record.id, record.revision))
                continue
            applied.append((saved.id, saved.revision))
    return Maintenance(dry_run, tuple((r.id, r.revision) for r in candidates),
                       tuple(applied), tuple(conflicted))
