# Release 0.2.1

## Included

- New frozen `IngestReceipt` and insert-only `ingest_batch` API report inserted records, duplicates and conflicts while continuing through classified conflicts. Legacy `ingest` remains fail-fast.
- Duplicate comparison ignores only `revision` and `created_at`; all other content and metadata must match. Existing records are never updated by `ingest_batch`, including when an input carries a nonzero revision. Duplicate/conflict receipts report the current observed stored revision; inserted receipts report the newly written revision. Repeated source occurrences remain separate receipt entries.
- `Maintenance.conflicted` reports (ID, snapshot revision) for candidates whose retirement encounters a CAS conflict. Other candidates continue; newer records are not overwritten. Dry runs leave `conflicted` empty, and strict boolean validation remains unchanged.
- The `summarize` docstring describes behavior without an obsolete version reference; summarization behavior is unchanged.

## Limits and compatibility

Each record write is atomic, but a batch is not. Receipts are returned only after successful source exhaustion. Iterator errors and non-conflict storage errors propagate; earlier writes remain committed. No transaction spans source iteration. These APIs introduce no model or network calls and no runtime dependencies.

Existing stored identities, revisions and schema are unchanged. `Maintenance.conflicted` is a trailing field with an empty default. A still-expired conflicting maintenance candidate can be retired by a subsequent run using a fresh snapshot.

## Deferred L2 work

Durable operation IDs and source cursors, automatic resumption after failure midway through source iteration, all-or-nothing small batches, and bounded maintenance pages remain deferred. This release makes no claim of improved recall quality or demonstrated scalability.
