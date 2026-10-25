# Release 0.2.2

## Included

- Opt-in `ingest_batch(store, source, checkpoint=..., namespace=...)` resumes an ordered `CheckpointSource` from its durable cursor. Frozen `IngestItem` supplies a stable operation ID and Record per position.
- Each item atomically commits its canonical insertion (if any), operation outcome and cursor. Classified duplicates and record conflicts advance the cursor; failures do not acknowledge an uncommitted item.
- Semantic SHA-256 fingerprints bind operation IDs to input. Matching replay preserves the original outcome and historical revision without another canonical write. Changed input raises `OperationConflictError`; a stale cursor raises `CheckpointConflictError` before replay or mutation.
- `Store.get_ingest_checkpoint` and `Store.get_ingest_operation` expose durable progress without writing. Receipt fields `checkpoint` and `operations` are trailing fields with defaults, preserving three-argument construction.

## Limits and compatibility

Legacy `ingest` and no-argument `ingest_batch` behavior remain unchanged, including mixed namespaces and insert-only duplicate comparison. Maintenance scope and behavior are unchanged. No dependencies, model calls or network calls are added.

Schema version 2 adds checkpoint and operation tables in the canonical database. Initialization accepts versions 0, 1 and 2 in one transaction, preserving existing payloads, identities, revisions, history, edges and feedback receipts. Unknown versions fail before DDL. Older releases cannot open schema 2; no downgrade migration is supplied.

The source must provide exactly the suffix requested by `records_from(cursor)`, preserve its committed prefix and redeliver uncommitted input. Unavailable offsets must raise. Completed prefixes are not reread or revalidated. Source callbacks, including iterator creation and exhaustion, run outside Store locks and transactions. Empty input creates no checkpoint row; later calls may consume appended input.

Fingerprints exclude only top-level revision and capture time (`created_at`). Capture time is volatile in this opt-in protocol even when explicitly supplied. Numeric confidence/event/expiry fields use exact rational encoding; all other serialized semantic fields remain included, including ordered source references and their revisions. See the [design contract](design.md) for exact encoding and validation.

Operation identities are shared across checkpoint names within a namespace, independent across namespaces and separate from feedback IDs. Fingerprints, receipts and canonical data share one database; ledgers are not evicted. Replays of original insertions or duplicates enter `duplicates`, while original conflicts remain `conflicts`, even if the canonical record has since changed. Receipt revisions are historical, not current-state guarantees.

An operation-ID conflict requires a fresh operation ID for changed semantic input at an uncommitted position, or redelivery of the original input. Never overwrite the old binding. There is no automatic retry on stale-cursor or SQLite busy errors: the caller decides when to start a fresh invocation from persisted progress. Other source/storage errors propagate without returning a partial receipt; earlier committed items remain durable, including when acknowledgement is lost after commit.

## Offline restart example

Run with the installed package. All input is synthetic and local.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from agent_memory import IngestItem, Record, Store, ingest_batch


class Feed:
    def __init__(self, items):
        self.items = items

    def records_from(self, cursor):
        if not 0 <= cursor <= len(self.items):
            raise ValueError('cursor unavailable')
        return iter(self.items[cursor:])


items = [
    IngestItem('event:1', Record(id='a', text='first fact', created_at=100)),
    IngestItem('event:2', Record(id='b', text='second fact', created_at=101)),
]
with TemporaryDirectory() as directory:
    path = Path(directory) / 'memory.sqlite'
    store = Store(path)
    try:
        first = ingest_batch(store, Feed(items), checkpoint='feed')
        assert first.inserted == (('a', 1), ('b', 1))
        assert store.get_ingest_checkpoint('feed').cursor == 2
    finally:
        store.close()

    # Keep the committed prefix unchanged and append new input.
    items.append(IngestItem(
        'event:3', Record(id='c', text='third fact', created_at=102)))
    store = Store(path)
    try:
        print(store.get_ingest_checkpoint('feed'))  # cursor=2
        suffix = ingest_batch(store, Feed(items), checkpoint='feed')
        assert suffix.inserted == (('c', 1),)
        assert suffix.checkpoint.cursor == 3
        assert len(suffix.operations) == 1
        print(suffix)
        assert store.get_ingest_operation('event:1').revision == 1
    finally:
        store.close()
```

## Deferred L2 work

All-or-nothing batches, bounded maintenance pages and maintenance checkpoints remain deferred. There are no opaque cursors, checkpoint reset/deletion, ledger pruning, leases, background jobs, automatic retries or external-source adapters.

SQLite transaction atomicity does not guarantee external-source durability or exactly-once external side effects. The synthetic restart, rollback and lost-acknowledgement controls do not establish physical power-loss durability, throughput or recall quality. The supported CI matrix remains separate from any local test run.
