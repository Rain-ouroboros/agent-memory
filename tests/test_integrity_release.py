"""Public 0.2.0 integrity contracts; executable with unittest discovery."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_memory import ConflictError, Record, Store, derive, recall
from agent_memory.index import SearchIndex


class FeedbackIntegrityTests(unittest.TestCase):
    def test_capacity_terminal_replay_and_reopen(self):
        for outcome in ('incorrect', 'retract'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'memory.sqlite'
                with Store(path) as store:
                    parent = store.put(Record.create('Synthetic capacity evidence'))
                    for n in range(64):
                        receipt = store.feedback(parent.id, expected_revision=1,
                                                 operation_id=f'op{n}', outcome='helpful')
                        self.assertEqual(receipt.revision, 1)
                    before = list(store._db.iterdump())
                    with self.assertRaises(ValueError):
                        store.feedback(parent.id, expected_revision=1,
                                       operation_id='overflow', outcome='irrelevant')
                    self.assertEqual(list(store._db.iterdump()), before)
                    terminal = store.feedback(parent.id, expected_revision=1,
                                              operation_id='terminal', outcome=outcome)
                    self.assertEqual(terminal.revision, 2)
                    self.assertEqual(store.get(parent.id).status, 'retired')
                    self.assertEqual(store.get(parent.id, revision=1), parent)
                    self.assertEqual(store._db.execute('SELECT count(*) FROM feedback').fetchone()[0], 65)
                with Store(path) as store:
                    for operation, kind, revision in [('op0', 'helpful', 1),
                                                      ('terminal', outcome, 2)]:
                        replay = store.feedback(parent.id, expected_revision=1,
                                                operation_id=operation, outcome=kind)
                        self.assertTrue(replay.duplicate)
                        self.assertEqual(replay.revision, revision)
                    with self.assertRaises(ConflictError):
                        store.feedback(parent.id, expected_revision=1,
                                       operation_id='op0', outcome='irrelevant')
                    with self.assertRaises(ValueError):
                        store.feedback(parent.id, expected_revision=2,
                                       operation_id='new-terminal', outcome=outcome)

    def test_independent_connection_stale_cas(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'memory.sqlite'
            with Store(path) as first, Store(path) as second:
                stale = first.put(Record.create('Synthetic original'))
                second.put(replace(second.get(stale.id), text='Synthetic changed'))
                before = list(first._db.iterdump())
                for outcome in ('helpful', 'incorrect', 'retract'):
                    with self.assertRaises(ConflictError):
                        first.feedback(stale.id, expected_revision=stale.revision,
                                       operation_id=outcome, outcome=outcome)
                self.assertEqual(list(first._db.iterdump()), before)

    def test_observations_preserve_evidence_history_index_and_graph(self):
        with Store() as store, SearchIndex() as index:
            parent = store.put(Record.create('Synthetic lighthouse evidence', created_at=0,
                                             audience=('reader',)))
            child = store.put(derive('Synthetic descendant', [parent], created_at=0))
            store.link(parent, 'beacon', 'connects', 'shore')
            store.link(child, 'shore', 'connects', 'harbour')
            index.rebuild(store)
            before = recall(store, 'lighthouse', principal='reader', now=100, index=index)
            hits = {hit.record.id: hit for hit in before.hits}
            self.assertIn('fts5', hits[parent.id].routes)
            self.assertIn('graph', hits[child.id].routes)
            snapshot = store.snapshot()
            history = store._db.execute('SELECT * FROM history ORDER BY id,revision').fetchall()
            edges = store.edges()
            candidates = index.candidates('lighthouse')
            for outcome in ('helpful', 'irrelevant'):
                receipt = store.feedback(parent.id, expected_revision=1,
                                         operation_id=outcome, outcome=outcome)
                self.assertEqual(receipt.revision, 1)
                self.assertEqual(store.snapshot(), snapshot)
                self.assertEqual(store._db.execute('SELECT * FROM history ORDER BY id,revision').fetchall(), history)
                self.assertEqual(store.edges(), edges)
                self.assertEqual(index.candidates('lighthouse'), candidates)
                self.assertEqual(recall(store, 'lighthouse', principal='reader', now=100, index=index), before)
            store.feedback(parent.id, expected_revision=1, operation_id='retire', outcome='retract')
            after = recall(store, 'lighthouse', principal='reader', now=100, index=index)
            self.assertFalse(after.hits)

    def test_terminal_receipt_failure_rolls_back_revision(self):
        with Store() as store:
            parent = store.put(Record.create('Synthetic atomic evidence'))
            store._db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON feedback BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
            before = list(store._db.iterdump())
            with self.assertRaises(sqlite3.IntegrityError):
                store.feedback(parent.id, expected_revision=1,
                               operation_id='terminal', outcome='retract')
            self.assertEqual(list(store._db.iterdump()), before)
            self.assertEqual(store.get(parent.id), parent)

    def test_legacy_bumped_receipt_replays_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'legacy.sqlite'
            with Store(path) as store:
                parent = store.put(Record.create('Synthetic legacy evidence'))
                bumped = store.put(parent)
                # Fixture for pre-0.2.0 observations, which bumped evidence CAS.
                store._db.execute('INSERT INTO feedback VALUES (?,?,?,?,?,?)',
                                  ('default', 'legacy', parent.id,
                                   json.dumps([parent.id, 1, 'helpful']), 'helpful', 2))
                store._db.commit()
                store.put(replace(bumped, text='Synthetic later edit'))
                store._db.execute('DROP INDEX feedback_record_idx')
                store._db.commit()
            with Store(path) as store:
                before = list(store._db.iterdump())
                receipt = store.feedback(parent.id, expected_revision=1,
                                         operation_id='legacy', outcome='helpful')
                self.assertTrue(receipt.duplicate)
                self.assertEqual(receipt.revision, 2)
                self.assertEqual(store.get(parent.id).revision, 3)
                self.assertEqual(store.get(parent.id, revision=2), bumped)
                self.assertEqual(list(store._db.iterdump()), before)
                columns = store._db.execute('PRAGMA index_info(feedback_record_idx)').fetchall()
                self.assertEqual([row[2] for row in columns], ['namespace', 'record_id'])


class MaintenanceIntegrityTests(unittest.TestCase):
    def test_invalid_dry_run_never_reads_snapshot(self):
        from agent_memory import maintain

        class UnreadStore:
            def snapshot(self, **kwargs):
                raise AssertionError('invalid dry_run reached snapshot')

        for value in (None, 0, 1, '', 'false', [], {}, 0.0):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, 'dry_run must be a boolean'):
                    maintain(UnreadStore(), dry_run=value)

    def test_boolean_dry_run_preserves_preview_and_apply(self):
        from agent_memory import maintain
        with Store() as store:
            record = store.put(Record.create('Synthetic expired', expires_at=1))
            preview = maintain(store, now=2, dry_run=True)
            self.assertEqual(preview.candidates, ((record.id, 1),))
            self.assertEqual(preview.applied, ())
            self.assertEqual(store.get(record.id), record)
            applied = maintain(store, now=2, dry_run=False)
            self.assertEqual(applied.applied, ((record.id, 2),))
            self.assertEqual(store.get(record.id).status, 'retired')


class DerivedIdentityIntegrityTests(unittest.TestCase):
    def test_exact_parent_sets_revisions_order_and_direct_creation(self):
        from agent_memory import SourceRef
        left = Record(id='left', text='Synthetic left', revision=1)
        right = Record(id='right', text='Synthetic right', revision=1)
        base = derive('Synthetic summary', [left, right], anchor='caller')
        reordered = derive('Synthetic summary', [right, left, left], anchor='caller')
        self.assertEqual(base.id, reordered.id)
        self.assertEqual(reordered.sources,
                         (SourceRef('right', 1), SourceRef('left', 1), SourceRef('left', 1)))
        direct = Record.create('Synthetic summary', derived=True, source=base.source,
                               anchor='caller', sources=base.sources)
        self.assertEqual(base.id, direct.id)
        variants = (
            derive('Synthetic summary', [left], anchor='caller'),
            derive('Synthetic summary', [replace(left, revision=2), right], anchor='caller'),
            derive('Synthetic summary', [left, right], anchor='different'),
            derive('Synthetic summary', [left, right], anchor=''),
            Record.create('Synthetic summary', derived=True, source='other',
                          anchor='caller', sources=base.sources),
            Record.create('Synthetic summary', derived=True, namespace='other',
                          source=base.source, anchor='caller', sources=base.sources),
            Record.create('Synthetic summary', derived=True, source=base.source,
                          anchor='caller', sources=(SourceRef('different', 1),)),
        )
        self.assertEqual(len({base.id, *(record.id for record in variants)}), 8)
        self.assertEqual(base.id, derive('Synthetic summary', [left, right], anchor='caller').id)

    def test_full_suffix_and_structured_identity(self):
        from agent_memory import SourceRef
        refs = (SourceRef('a:b', 1), SourceRef('c', 2))
        kwargs = dict(derived=True, sources=refs, anchor='explicit')
        first = Record.create('x' * 1000 + 'A', **kwargs)
        second = Record.create('x' * 1000 + 'B', **kwargs)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(
            Record.create('text', derived=True, source='a:b', anchor='c', sources=refs).id,
            Record.create('text', derived=True, source='a', anchor='b:c', sources=refs).id)

    def test_primary_hash_legacy_read_and_distinct_provenance_persistence(self):
        from hashlib import sha256
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'identities.sqlite'
            with Store(path) as store:
                left = store.put(Record.create('Synthetic left'))
                right = store.put(Record.create('Synthetic right'))
                expected = sha256(json.dumps(['default', 'manual', '', left.text],
                                             ensure_ascii=False).encode()).hexdigest()
                self.assertEqual(left.id, expected)
                first = store.put(derive('Same summary', [left]))
                second = store.put(derive('Same summary', [right]))
                self.assertNotEqual(first.id, second.id)
                with self.assertRaises(ConflictError):
                    store.put(derive('Same summary', [left]))
                legacy_id = sha256(json.dumps(['default', 'summary', 'old', 'Legacy summary'],
                                              ensure_ascii=False).encode()).hexdigest()
                legacy = store.put(replace(derive('Legacy summary', [left]), id=legacy_id))
            with Store(path) as store:
                self.assertEqual(store.get(first.id), first)
                self.assertEqual(store.get(second.id), second)
                self.assertEqual(store.get(legacy_id), legacy)
                self.assertEqual(store.get(legacy_id).sources, legacy.sources)
