"""Offline controls for durable ingestion positions and operation receipts."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent_memory as api
from agent_memory import (
    CheckpointConflictError, CheckpointSource, IngestCheckpoint, IngestItem,
    IngestOperation, IngestReceipt, OperationConflictError, Record, SourceRef,
    Store, ingest_batch,
)
from agent_memory.store import _ingest_fingerprint


class Feed:
    def __init__(self, items):
        self.items = items
        self.offsets = []
        self.requested = []

    def records_from(self, cursor):
        self.offsets.append(cursor)
        if not 0 <= cursor <= len(self.items):
            raise ValueError('unavailable cursor')
        def suffix():
            for index in range(cursor, len(self.items)):
                self.requested.append(index)
                yield self.items[index]
        return suffix()


class LegacySource:
    def __init__(self, records):
        self.values = records

    def records(self):
        return iter(self.values)


class Crash(BaseException):
    pass


class IngestCheckpointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'memory.sqlite'

    def item(self, name, **changes):
        return IngestItem(name, Record(id=name, text='evidence ' + name,
                                       created_at=100, **changes))

    def count(self, store, table):
        return store._db.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def dump(self, store):
        return tuple(store._db.iterdump())

    def test_public_exports_and_frozen_defaults(self):
        names = ('CheckpointConflictError', 'OperationConflictError',
                 'IngestCheckpoint', 'IngestOperation', 'IngestItem', 'CheckpointSource')
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, api.__all__)
                self.assertIsNotNone(getattr(api, name))
        objects = (
            IngestCheckpoint('default', 'feed', 0),
            IngestOperation('default', 'op', 'a' * 64, 'r', 'inserted', 1),
            self.item('r'), IngestReceipt((), (), ()),
        )
        for obj in objects:
            field = next(iter(obj.__dataclass_fields__))
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field, None)
        self.assertIsNone(objects[-1].checkpoint)
        self.assertEqual(objects[-1].operations, ())
        self.assertFalse(objects[1].replayed)
        self.assertTrue(issubclass(CheckpointConflictError, api.ConflictError))
        self.assertTrue(issubclass(OperationConflictError, api.ConflictError))

    def test_legacy_batch_does_not_create_checkpoint_state(self):
        first = self.item('a', revision=7).record
        other = replace(first, namespace='other')
        with Store(self.path) as store:
            receipt = ingest_batch(store, LegacySource([
                first, other, first, replace(first, created_at=200),
                replace(first, trust='trusted'),
            ]))
            self.assertEqual(receipt.inserted, (('a', 1), ('a', 1)))
            self.assertEqual(receipt.duplicates, (('a', 1), ('a', 1)))
            self.assertEqual(receipt.conflicts, (('a', 1),))
            self.assertIsNone(receipt.checkpoint)
            self.assertEqual(receipt.operations, ())
            self.assertEqual(self.count(store, 'ingest_checkpoints'), 0)
            self.assertEqual(self.count(store, 'ingest_operations'), 0)

    def test_arguments_are_validated_before_source_access(self):
        invalid = ('', 'private payload!', 'a' * 101, True, 1, [], None)
        with Store(self.path) as store:
            sql = []
            store._db.set_trace_callback(sql.append)
            for value in invalid:
                for method in (store.get_ingest_checkpoint, store.get_ingest_operation):
                    with self.subTest(value=value, method=method.__name__):
                        sql.clear()
                        with self.assertRaises(ValueError) as caught:
                            method(value)
                        self.assertEqual(sql, [])
                        self.assertNotIn('private payload!', str(caught.exception))
                if value is not None:
                    sql.clear()
                    with self.assertRaises(ValueError):
                        ingest_batch(store, object(), checkpoint=value)
                    self.assertEqual(sql, [])
            for namespace in ('', True, 7, []):
                sql.clear()
                with self.assertRaises(ValueError):
                    ingest_batch(store, object(), checkpoint='feed', namespace=namespace)
                self.assertEqual(sql, [])
                for method in (store.get_ingest_checkpoint, store.get_ingest_operation):
                    with self.assertRaises(ValueError):
                        method('valid', namespace=namespace)
                    self.assertEqual(sql, [])
            sql.clear()
            with self.assertRaises(ValueError):
                ingest_batch(store, object(), namespace='default')
            self.assertEqual(sql, [])
            for identifier in ('a', 'a' * 100, 'A-z_0.9:'):
                self.assertEqual(store.get_ingest_checkpoint(identifier).cursor, 0)
                self.assertIsNone(store.get_ingest_operation(identifier))
                receipt = ingest_batch(store, Feed([IngestItem(identifier, self.item(identifier).record)]),
                                       checkpoint=identifier)
                self.assertEqual(receipt.checkpoint.cursor, 1)
            spaced = ingest_batch(store, Feed([self.item('space', namespace=' ')]),
                                  checkpoint='space', namespace=' ')
            self.assertEqual(spaced.checkpoint.namespace, ' ')

    def test_invalid_item_preserves_committed_prefix(self):
        bad_items = (object(), IngestItem('bad', object()),
                     IngestItem('bad value!', self.item('bad').record),
                     self.item('bad', namespace='other'))
        for index, bad in enumerate(bad_items):
            with self.subTest(index=index), Store(self.path.parent / ('bad%d.sqlite' % index)) as store:
                statements = []
                def source():
                    yield self.item('first')
                    statements.clear()
                    yield bad
                    self.fail('later item requested')
                class Source:
                    def records_from(self, cursor):
                        return source()
                store._db.set_trace_callback(statements.append)
                with self.assertRaises(ValueError):
                    ingest_batch(store, Source(), checkpoint='feed')
                self.assertFalse(any(s.startswith('BEGIN') for s in statements))
                self.assertEqual(store.get_ingest_checkpoint('feed').cursor, 1)
                self.assertEqual(self.count(store, 'ingest_operations'), 1)
                self.assertEqual(self.count(store, 'history'), 1)
                self.assertIsNone(store.get('bad'))

    def test_checkpointed_categories_preserve_order_and_occurrences(self):
        first = self.item('a', revision=7)
        items = [first, IngestItem('fresh', first.record),
                 IngestItem('different', replace(first.record, trust='trusted')),
                 first, self.item('b'), IngestItem('again', first.record)]
        with Store(self.path) as store:
            receipt = ingest_batch(store, Feed(items), checkpoint='feed')
            self.assertEqual(receipt.inserted, (('a', 1), ('b', 1)))
            self.assertEqual(receipt.duplicates, (('a', 1), ('a', 1), ('a', 1)))
            self.assertEqual(receipt.conflicts, (('a', 1),))
            self.assertEqual([o.operation_id for o in receipt.operations],
                             [i.operation_id for i in items])
            self.assertEqual([o.outcome for o in receipt.operations],
                             ['inserted', 'duplicate', 'conflict', 'inserted', 'inserted', 'duplicate'])
            self.assertEqual([o.replayed for o in receipt.operations],
                             [False, False, False, True, False, False])
            self.assertEqual(receipt.checkpoint.cursor, 6)
            self.assertEqual(self.count(store, 'history'), 2)
            self.assertEqual(self.count(store, 'ingest_operations'), 5)

    def test_resume_requests_only_uncommitted_suffix(self):
        items = [self.item('a'), self.item('b')]
        with Store(self.path) as store:
            ingest_batch(store, Feed(items), checkpoint='feed')
        items.append(self.item('c'))
        source = Feed(items)
        with Store(self.path) as store:
            receipt = ingest_batch(store, source, checkpoint='feed')
            self.assertEqual(source.offsets, [2])
            self.assertEqual(source.requested, [2])
            self.assertEqual(receipt.inserted, (('c', 1),))
            empty = ingest_batch(store, source, checkpoint='feed')
            self.assertEqual(source.offsets, [2, 3])
            self.assertEqual((empty.inserted, empty.duplicates, empty.conflicts, empty.operations),
                             ((), (), (), ()))
            self.assertEqual(empty.checkpoint.cursor, 3)
            self.assertEqual(self.count(store, 'history'), 3)

    def test_empty_source_and_read_methods_do_not_write(self):
        with Store(self.path) as store:
            before = self.dump(store)
            self.assertEqual(store.get_ingest_checkpoint('feed'), IngestCheckpoint('default', 'feed', 0))
            self.assertIsNone(store.get_ingest_operation('missing'))
            receipt = ingest_batch(store, Feed([]), checkpoint='feed')
            self.assertEqual(receipt.checkpoint.cursor, 0)
            self.assertEqual(self.dump(store), before)
            source = Feed([self.item('a')])
            ingest_batch(store, source, checkpoint='feed')
            before = self.dump(store)
            receipt = ingest_batch(store, source, checkpoint='feed')
            self.assertEqual(receipt.checkpoint.cursor, 1)
            self.assertEqual(self.dump(store), before)

    def test_replay_survives_restart_and_canonical_change(self):
        original = self.item('a')
        with Store(self.path) as store:
            ingest_batch(store, Feed([original]), checkpoint='feed')
            current = store.get('a')
            store.put(replace(current, text='edited', status='retired'))
        replay = IngestItem('a', replace(original.record, created_at=900, revision=99))
        with Store(self.path) as store:
            before = self.count(store, 'history')
            receipt = ingest_batch(store, Feed([original, replay]), checkpoint='feed')
            self.assertEqual(receipt.inserted, ())
            self.assertEqual(receipt.duplicates, (('a', 1),))
            self.assertEqual(receipt.operations[0].outcome, 'inserted')
            self.assertTrue(receipt.operations[0].replayed)
            self.assertEqual(receipt.checkpoint.cursor, 2)
            self.assertEqual(self.count(store, 'history'), before)
            self.assertEqual(store.get('a').text, 'edited')
            self.assertEqual(store.get('a').status, 'retired')
            operation = store.get_ingest_operation('a')
            self.assertFalse(operation.replayed)
            self.assertEqual(operation.revision, 1)

    def test_record_conflict_is_durable_and_replayable(self):
        item = self.item('a')
        with Store(self.path) as store:
            store.put(replace(item.record, text='different'))
            receipt = ingest_batch(store, Feed([item, self.item('b')]), checkpoint='feed')
            self.assertEqual(receipt.conflicts, (('a', 1),))
            self.assertEqual(receipt.inserted, (('b', 1),))
            self.assertEqual(receipt.checkpoint.cursor, 2)
            store.put(replace(item.record, revision=1))
            ledger = store.get_ingest_operation('a')
            history = self.count(store, 'history')
            receipt = ingest_batch(store, Feed([item]), checkpoint='replay')
            self.assertEqual(receipt.conflicts, (('a', 1),))
            self.assertTrue(receipt.operations[0].replayed)
            self.assertEqual(receipt.checkpoint.cursor, 1)
            self.assertEqual(store.get_ingest_operation('a'), ledger)
            self.assertEqual(self.count(store, 'history'), history)
            self.assertEqual(store.get('a').revision, 2)

    def test_changed_operation_payload_conflicts_without_advancing(self):
        original = self.item('a')
        with Store(self.path) as store:
            ingest_batch(store, Feed([original]), checkpoint='feed')
        variants = (
            {'text': 'changed'}, {'trust': 'trusted'}, {'audience': ('reader',)},
            {'sources': (SourceRef('parent', 1),)}, {'id': 'new-target'},
        )
        with Store(self.path) as store:
            before = self.dump(store)
            for changes in variants:
                for checkpoint in ('feed', 'fresh'):
                    with self.subTest(changes=changes, checkpoint=checkpoint):
                        changed = IngestItem('a', replace(original.record, **changes))
                        items = ([original] if checkpoint == 'feed' else []) + [changed, self.item('later')]
                        source = Feed(items)
                        with self.assertRaises(OperationConflictError) as caught:
                            ingest_batch(store, source, checkpoint=checkpoint)
                        self.assertEqual(str(caught.exception),
                                         'operation_id already names different ingestion')
                        self.assertEqual(source.requested, [1 if checkpoint == 'feed' else 0])
                        self.assertEqual(self.dump(store), before)
                        self.assertIsNone(store.get('new-target'))

    def test_fingerprint_golden_vector_and_numeric_equivalence(self):
        record = Record(id='r', text='evidence', created_at=100)
        golden = '1d2bb3369b7902a786a7c9b9e48d08ce94b7715f878127b671ac080c97a1e9a9'
        self.assertEqual(_ingest_fingerprint(record), golden)
        self.assertEqual(_ingest_fingerprint(replace(record, revision=91, created_at=999)), golden)
        for field in ('confidence', 'occurred_at', 'expires_at'):
            for integer, floating in ((0, 0.0), (0, -0.0), (1, 1.0)):
                with self.subTest(field=field, value=floating):
                    self.assertEqual(
                        _ingest_fingerprint(replace(record, **{field: integer})),
                        _ingest_fingerprint(replace(record, **{field: floating})))
            self.assertNotEqual(
                _ingest_fingerprint(replace(record, **{field: 0})),
                _ingest_fingerprint(replace(record, **{field: 0.5})))
        for field in ('occurred_at', 'expires_at'):
            self.assertNotEqual(
                _ingest_fingerprint(replace(record, **{field: None})),
                _ingest_fingerprint(replace(record, **{field: 0})))

    def test_fingerprint_includes_every_semantic_field(self):
        record = Record(id='r', text='evidence', created_at=100)
        variants = {
            'id': 'other', 'namespace': 'other', 'text': 'evidence suffix',
            'source': 'import', 'author': 'writer', 'kind': 'preference',
            'trust': 'trusted', 'audience': ('reader',),
            'sources': (SourceRef('p', 1),), 'derived': True,
            'complete': False, 'confidence': 0.5, 'occurred_at': 1,
            'expires_at': 2, 'status': 'retired',
        }
        baseline = _ingest_fingerprint(record)
        for field, value in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(baseline, _ingest_fingerprint(replace(record, **{field: value})))
        refs = (SourceRef('p', 1), SourceRef('q', 2))
        with_refs = replace(record, sources=refs)
        for changed in (refs[::-1], refs + (refs[0],),
                        (SourceRef('p', 2), refs[1]),
                        (refs[0], SourceRef('q', 3))):
            with self.subTest(sources=changed):
                self.assertNotEqual(_ingest_fingerprint(with_refs),
                                    _ingest_fingerprint(replace(record, sources=changed)))
        self.assertEqual(
            _ingest_fingerprint(replace(record, audience=('b', 'a', 'a'))),
            _ingest_fingerprint(replace(record, audience=('a', 'b'))))

    def test_source_io_never_holds_a_transaction(self):
        with Store(self.path) as store, Store(self.path, timeout=0) as other:
            class TrackingLock:
                def __init__(self, lock):
                    self.lock = lock
                    self.depth = 0

                def __enter__(self):
                    self.lock.acquire()
                    self.depth += 1
                    return self

                def __exit__(self, *exc):
                    self.depth -= 1
                    self.lock.release()

            store._lock = TrackingLock(store._lock)
            events = []
            items = [self.item('a'), self.item('b')]

            def observe(event):
                self.assertFalse(store._db.in_transaction)
                self.assertEqual(store._lock.depth, 0)
                events.append(event)
                other.put(Record(id='callback-%d' % len(events), text='callback', created_at=100))

            class Iterator:
                def __init__(self):
                    self.position = 0

                def __iter__(self):
                    observe('iter')
                    return self

                def __next__(self):
                    observe('next' if self.position < len(items) else 'exhaustion')
                    if self.position == len(items):
                        raise StopIteration
                    item = items[self.position]
                    self.position += 1
                    return item

            class Source:
                def records_from(self, cursor):
                    observe('creation')
                    return Iterator()

            receipt = ingest_batch(store, Source(), checkpoint='feed')
            self.assertEqual(events, ['creation', 'iter', 'next', 'next', 'exhaustion'])
            self.assertEqual(receipt.checkpoint.cursor, 2)
            self.assertEqual(self.count(store, 'records'), 7)

    def test_crash_after_commit_before_receipt_resumes_correctly(self):
        class LostAcknowledgementStore(Store):
            def _ingest_item(self, record, *, operation_id, checkpoint):
                result = super()._ingest_item(record, operation_id=operation_id,
                                              checkpoint=checkpoint)
                if checkpoint.cursor == 1:
                    raise Crash('lost acknowledgement')
                return result

        items = [self.item('a'), self.item('b'), self.item('c')]
        source = Feed(items)
        with LostAcknowledgementStore(self.path) as store:
            with self.assertRaises(Crash):
                ingest_batch(store, source, checkpoint='feed')
            self.assertEqual(source.requested, [0, 1])
        with Store(self.path) as store:
            self.assertEqual(store.get_ingest_checkpoint('feed').cursor, 2)
            for name in ('a', 'b'):
                self.assertEqual(store.get(name).revision, 1)
                self.assertEqual(store.get_ingest_operation(name).revision, 1)
            resumed = Feed(items)
            receipt = ingest_batch(store, resumed, checkpoint='feed')
            self.assertEqual(resumed.offsets, [2])
            self.assertEqual(resumed.requested, [2])
            self.assertEqual(receipt.inserted, (('c', 1),))
            self.assertEqual(receipt.checkpoint.cursor, 3)
            self.assertEqual(self.count(store, 'history'), 3)
            self.assertEqual(self.count(store, 'ingest_operations'), 3)
            self.assertEqual(store._db.execute('SELECT revision FROM history').fetchall(),
                             [(1,), (1,), (1,)])

    def test_failure_inside_canonical_write_rolls_back_item(self):
        class FailedWriteStore(Store):
            def _write(self, record, revision):
                result = super()._write(record, revision)
                if record.id == 'b':
                    raise Crash('interrupted write')
                return result

        items = [self.item('a'), self.item('b'), self.item('c')]
        source = Feed(items)
        with FailedWriteStore(self.path) as store:
            with self.assertRaises(Crash):
                ingest_batch(store, source, checkpoint='feed')
            self.assertEqual(source.requested, [0, 1])
            self.assertFalse(store._db.in_transaction)
            self.assertEqual(store.get_ingest_checkpoint('feed').cursor, 1)
            self.assertIsNone(store.get('b'))
            self.assertIsNone(store.get('b', revision=1))
            self.assertIsNone(store.get_ingest_operation('b'))
            self.assertEqual(self.count(store, 'history'), 1)
            self.assertEqual(self.count(store, 'ingest_operations'), 1)
        with Store(self.path) as store:
            resumed = Feed(items)
            receipt = ingest_batch(store, resumed, checkpoint='feed')
            self.assertEqual(resumed.offsets, [1])
            self.assertEqual(receipt.inserted, (('b', 1), ('c', 1)))
            self.assertEqual(receipt.checkpoint.cursor, 3)
            self.assertEqual(self.count(store, 'history'), 3)
            self.assertEqual(self.count(store, 'ingest_operations'), 3)

    def test_two_connection_busy_does_not_advance_cursor(self):
        with Store(self.path, timeout=0) as first, Store(self.path, timeout=0) as second:
            first._db.execute('BEGIN IMMEDIATE')
            source = Feed([self.item('a'), self.item('later')])
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ingest_batch(second, source, checkpoint='feed')
                self.assertEqual(source.requested, [0])
                self.assertEqual(second.get_ingest_checkpoint('feed').cursor, 0)
                self.assertIsNone(second.get_ingest_operation('a'))
                self.assertIsNone(second.get('a'))
                self.assertEqual(self.count(second, 'history'), 0)
                self.assertFalse(second._db.in_transaction)
            finally:
                first._db.rollback()
            receipt = ingest_batch(second, Feed([self.item('a')]), checkpoint='feed')
            self.assertEqual(receipt.inserted, (('a', 1),))
            self.assertEqual(receipt.checkpoint.cursor, 1)
            self.assertEqual(self.count(second, 'records'), 1)
            self.assertEqual(self.count(second, 'history'), 1)
            self.assertEqual(self.count(second, 'ingest_operations'), 1)

    def test_two_connection_stale_cursor_is_rejected(self):
        items = [self.item('a'), self.item('b')]
        with Store(self.path, timeout=0) as first, Store(self.path, timeout=0) as second:
            offsets = []

            class RacingSource:
                def records_from(self, cursor):
                    offsets.append(cursor)
                    ingest_batch(second, Feed(items[:1]), checkpoint='feed')
                    return iter(items[cursor:])

            with self.assertRaises(CheckpointConflictError) as caught:
                ingest_batch(first, RacingSource(), checkpoint='feed')
            self.assertEqual(str(caught.exception), 'ingestion checkpoint changed')
            self.assertEqual(offsets, [0])
            self.assertEqual(first.get_ingest_checkpoint('feed').cursor, 1)
            self.assertEqual(self.count(first, 'history'), 1)
            self.assertEqual(self.count(first, 'ingest_operations'), 1)
            self.assertIsNone(first.get('b'))
            source = Feed(items)
            receipt = ingest_batch(first, source, checkpoint='feed')
            self.assertEqual(source.offsets, [1])
            self.assertEqual(source.requested, [1])
            self.assertEqual(receipt.inserted, (('b', 1),))
            self.assertEqual(receipt.checkpoint.cursor, 2)

    def legacy_fixture(self, path, version=1):
        connection = sqlite3.connect(path)
        try:
            statements = (
                'CREATE TABLE records (namespace TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,id))',
                'CREATE TABLE history (namespace TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,id,revision))',
                'CREATE TABLE edges (namespace TEXT NOT NULL, source TEXT NOT NULL, relation TEXT NOT NULL, target TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL, weight REAL NOT NULL, PRIMARY KEY(namespace,source,relation,target,record_id), FOREIGN KEY(namespace,record_id) REFERENCES records(namespace,id))',
                'CREATE TABLE feedback (namespace TEXT NOT NULL, operation_id TEXT NOT NULL, record_id TEXT NOT NULL, command TEXT NOT NULL, outcome TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(namespace,operation_id))',
                'CREATE INDEX feedback_record_idx ON feedback(namespace,record_id)',
            )
            for statement in statements:
                connection.execute(statement)
            original = Record(id='legacy', text='legacy evidence', created_at=100, revision=1)
            retired = replace(original, status='retired', revision=2)
            for record in (original, retired):
                payload = json.dumps(record.to_dict(), ensure_ascii=False, allow_nan=False)
                connection.execute('INSERT INTO history VALUES (?,?,?,?)',
                                   ('default', record.id, record.revision, payload))
            connection.execute('INSERT INTO records VALUES (?,?,?,?)',
                               ('default', retired.id, 2, payload))
            connection.execute('INSERT INTO edges VALUES (?,?,?,?,?,?,?)',
                               ('default', 'subject', 'supports', 'target', 'legacy', 1, .5))
            connection.execute('INSERT INTO feedback VALUES (?,?,?,?,?,?)',
                               ('default', 'feedback:1', 'legacy',
                                json.dumps(['legacy', 1, 'retract']), 'retract', 2))
            connection.execute('PRAGMA user_version = %d' % version)
            connection.commit()
            return {table: connection.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('records', 'history', 'edges', 'feedback')}
        finally:
            connection.close()

    def test_migrate_v1_preserves_history_edges_and_feedback(self):
        original = self.legacy_fixture(self.path)
        with Store(self.path) as store:
            self.assertEqual(store._db.execute('PRAGMA user_version').fetchone()[0], 2)
            for table, rows in original.items():
                self.assertEqual(store._db.execute('SELECT * FROM ' + table).fetchall(), rows)
            self.assertEqual(self.count(store, 'ingest_checkpoints'), 0)
            self.assertEqual(self.count(store, 'ingest_operations'), 0)
            receipt = store.feedback('legacy', expected_revision=1,
                                     operation_id='feedback:1', outcome='retract')
            self.assertTrue(receipt.duplicate)
            self.assertEqual(receipt.revision, 2)
            for table, rows in original.items():
                self.assertEqual(store._db.execute('SELECT * FROM ' + table).fetchall(), rows)
            ingest_batch(store, Feed([self.item('new')]), checkpoint='feed')
            expected = self.dump(store)
        for _ in range(2):
            with Store(self.path) as store:
                self.assertEqual(self.dump(store), expected)
                self.assertEqual(store.get_ingest_checkpoint('feed').cursor, 1)
                self.assertEqual(store.get_ingest_operation('new').revision, 1)

    def test_schema_versions_and_idempotent_reopen(self):
        with Store(self.path) as store:
            self.assertEqual(store._db.execute('PRAGMA user_version').fetchone()[0], 2)
            ingest_batch(store, Feed([self.item('a')]), checkpoint='feed')
            expected = self.dump(store)
        with Store(self.path) as store:
            self.assertEqual(self.dump(store), expected)
            self.assertEqual(store.get_ingest_checkpoint('feed').cursor, 1)
        legacy_path = self.path.parent / 'legacy-zero.sqlite'
        original = self.legacy_fixture(legacy_path, version=0)
        with Store(legacy_path) as store:
            self.assertEqual(store._db.execute('PRAGMA user_version').fetchone()[0], 2)
            for table, rows in original.items():
                self.assertEqual(store._db.execute('SELECT * FROM ' + table).fetchall(), rows)
            expected = self.dump(store)
        with Store(legacy_path) as store:
            self.assertEqual(self.dump(store), expected)
        unknown_path = self.path.parent / 'unknown.sqlite'
        connection = sqlite3.connect(unknown_path)
        try:
            connection.execute('CREATE TABLE sentinel (value TEXT)')
            connection.execute("INSERT INTO sentinel VALUES ('preserved')")
            connection.execute('PRAGMA user_version = 99')
            connection.commit()
            before = tuple(connection.iterdump())
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            Store(unknown_path)
        connection = sqlite3.connect(unknown_path)
        try:
            self.assertEqual(tuple(connection.iterdump()), before)
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 99)
        finally:
            connection.close()
