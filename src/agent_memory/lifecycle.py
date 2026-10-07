"""Explicit maintenance; retention is a status change, not physical erasure."""
from dataclasses import dataclass, replace
import time
import math
from .store import Store


@dataclass(frozen=True)
class Maintenance:
    dry_run: bool
    candidates: tuple[tuple[str, int], ...]
    applied: tuple[tuple[str, int], ...]


def maintain(store: Store, *, namespace: str = 'default', now: float | None = None,
             dry_run: bool = True) -> Maintenance:
    clock = time.time() if now is None else now
    if not math.isfinite(clock):
        raise ValueError('now must be finite')
    candidates = [r for r in store.snapshot(namespace=namespace).values()
                  if r.status == 'active' and r.expires_at is not None and clock >= r.expires_at]
    applied = []
    if not dry_run:
        for record in candidates:
            saved = store.put(replace(record, status='retired'))
            applied.append((saved.id, saved.revision))
    return Maintenance(dry_run, tuple((r.id, r.revision) for r in candidates), tuple(applied))
