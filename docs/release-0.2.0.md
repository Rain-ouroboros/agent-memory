# Release 0.2.0 — memory integrity

This bounded release implements Q1, Q2, Q3 and L1. It does not replace storage or establish improved live retrieval or answer quality.

## Included

- **Q1 — feedback capacity:** the per-record cap applies to 64 nonterminal helpful/irrelevant receipts. Terminal incorrect/retract remain available at capacity, subject to revision and lifecycle checks. Duplicate operations are resolved before lifecycle, capacity and CAS checks. Terminal revision and receipt commit atomically. A feedback(namespace, record_id) index supports lookup; old receipts are retained.
- **Q2 — explicit maintenance intent:** `dry_run` must be an actual bool. Invalid values raise `ValueError` before snapshot access or writes. `True` and `False` retain their existing meanings.
- **Q3 — derived identity:** new derived IDs hash versioned structured JSON containing namespace, source, caller anchor, full text and sorted unique exact parent ID/revision pairs. Both `derive` and direct `Record.create(derived=True, sources=...)` use this identity. Explicit anchors remain meaningful; parent order is irrelevant.
- **L1 — observations are not evidence edits:** helpful/irrelevant write only the feedback ledger and return the observed evidence revision. Record history, descendants, FTS and graph bindings remain unchanged. Terminal feedback creates a lifecycle revision.

## Compatibility and integration

Existing databases remain readable. Primary IDs, persisted derived IDs and SourceRefs are not rewritten. Historical feedback receipts replay unchanged, including revisions created by older observation behavior; there is no renumbering or reversal of historical invalidation. Recreating a derived candidate can produce a new ID under the new identity scheme. Deterministic IDs do not imply upsert: stale or duplicate inserts still raise `ConflictError`.

The native runtime already separates evidence revisions from CAS revisions; the public ledger-only change must not be copied as a replacement for that contract. Public receipt replay and native current-CAS replay are distinct integration semantics.

Activation is an explicit host upgrade to 0.2.0 after review and release. Upgrade all writer processes together for the ledger-only observation contract: older 0.1.x writers can still advance evidence revisions on helpful/irrelevant feedback even though the database remains readable. Back up stores before upgrading, retain historical receipts, and test application assumptions about observation revisions. This library does not activate or migrate a native runtime. Runtime dependencies remain zero.

## Verification and deferred work

Regression tests use unittest-compatible cases and synthetic temporary stores. They cover capacity and terminal feedback, replay/reopen/conflicts, independent-connection stale CAS, unchanged evidence and retrieval bindings, historical receipts, strict maintenance arguments, exact-parent derived identities and legacy IDs. Existing observation-revision expectations are explicitly updated rather than dropping their conflict and capacity coverage.

L2, O1, A1 and P2 remain subsequent work requiring separate measurements and acceptance criteria. This release does not claim those broader improvements, scale gains, or measured memory-quality benefits.
