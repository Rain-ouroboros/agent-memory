"""Host integration seam for an extracted revisioned memory store.

No storage implementation, implicit host discovery or model calls.
Callbacks are trusted host code, not functions supplied by memory content.
The host must provide a consistent snapshot of latest canonical revisions.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

Row = Mapping[str, Any]


class SnapshotError(ValueError):
    """The canonical snapshot is ambiguous or malformed."""


@dataclass(frozen=True)
class ReadResult:
    records: tuple[dict[str, Any], ...]
    truncated: bool
    # This is an authorized selection, never a statement of global absence.
    coverage: str = "authorized_snapshot_selection"
    actual_write_performed: bool = False


class CanonicalReadAdapter:
    """Read-only seam; authorization is mandatory, never inferred from text.

    latest_rows: existing store's folded canonical snapshot, not FTS hits.
    metadata: extracted record_metadata or an equivalent reviewed projector.
    revision: extracted source_revision or an equivalent canonical revision.
    authorize: host ACL check for this audience AND exact row revision.
    eligible: host lifecycle check, including retraction/expiry policy.

    Audience identifiers must come from authenticated host context.
    A result may contain private text for its authorized audience. It is not
    anonymized, safe to publish, or safe to treat as executable instructions.
    """

    def __init__(
        self,
        *,
        latest_rows: Callable[[], Iterable[Row]],
        metadata: Callable[[Row], Mapping[str, Any]],
        revision: Callable[[Row], str],
        authorize: Callable[[Row, Mapping[str, Any], str, str], bool],
        eligible: Callable[[Row], bool],
    ) -> None:
        self._latest_rows = latest_rows
        self._metadata = metadata
        self._revision = revision
        self._authorize = authorize
        self._eligible = eligible

    def select(self, *, audience: str, limit: int = 20) -> ReadResult:
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError("An explicit authenticated audience is required")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")

        # Materialize before returning anything: a later duplicate cannot make
        # an already-returned earlier revision appear canonical.
        snapshot: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in self._latest_rows():
            if not isinstance(raw, Mapping):
                raise SnapshotError("Expected canonical row mapping")
            row = deepcopy(dict(raw))
            rid = row.get("id")
            if not isinstance(rid, str) or not rid.strip():
                raise SnapshotError("Missing canonical record id")
            if rid in seen:
                raise SnapshotError("Duplicate id in canonical snapshot")
            seen.add(rid)
            snapshot.append(row)

        selected: list[dict[str, Any]] = []
        truncated = False
        for row in snapshot:
            if self._eligible(deepcopy(row)) is not True:
                continue
            text = row.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            projected = self._metadata(deepcopy(row))
            if not isinstance(projected, Mapping):
                raise SnapshotError("Expected record metadata mapping")
            meta = deepcopy(dict(projected))
            scope = meta.get("access_scope")
            # Unknown legacy visibility must be resolved by the host before
            # it can be exposed; no permissive default here.
            if not isinstance(scope, str) or not scope.strip():
                continue
            if scope.strip().lower() in {"unknown", "unspecified"}:
                continue
            rev = self._revision(deepcopy(row))
            if not isinstance(rev, str) or not rev.strip():
                raise SnapshotError("Missing canonical revision")
            allowed = self._authorize(
                deepcopy(row), deepcopy(meta), audience, rev
            )
            if allowed is not True:
                continue
            if len(selected) == limit:
                truncated = True
                break
            selected.append({
                "record": row,
                "metadata": meta,
                "revision": rev,
            })
        return ReadResult(tuple(selected), truncated)
