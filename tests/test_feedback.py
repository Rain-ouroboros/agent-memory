from dataclasses import replace
from pathlib import Path
import os
import tempfile
import unittest
from agent_memory import ConflictError, Record, Store, derive, recall


class FeedbackTests(unittest.TestCase):
    def test_retraction_is_revision_bound_idempotent_and_persistent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'memory.sqlite'
            with Store(path) as store:
                parent = store.put(Record.create('Atlas storage', audience=('agent',), created_at=100))
                child = store.put(derive('Atlas derived storage', [parent], created_at=100))
                receipt = store.feedback(parent.id, expected_revision=1, operation_id='op:1', outcome='retract')
                self.assertFalse(receipt.duplicate)
                self.assertEqual(receipt.revision, 2)
                self.assertEqual(recall(store, 'Atlas', principal='agent', now=100).hits, ())
            with Store(path) as store:
                replay = store.feedback(parent.id, expected_revision=1, operation_id='op:1', outcome='retract')
                self.assertTrue(replay.duplicate)
                self.assertEqual(store.get(parent.id).revision, 2)
                self.assertEqual(store.get(parent.id, revision=1), parent)
                with self.assertRaises(ConflictError):
                    store.feedback(parent.id, expected_revision=2, operation_id='op:1', outcome='helpful')

    def test_helpful_does_not_raise_confidence_and_stale_feedback_fails(self):
        with Store() as store:
            record = store.put(Record.create('Atlas', confidence=.4))
            store.feedback(record.id, expected_revision=1, operation_id='first', outcome='helpful')
            self.assertEqual(store.get(record.id).confidence, .4)
            with self.assertRaises(ConflictError):
                store.feedback(record.id, expected_revision=1, operation_id='stale', outcome='incorrect')
            self.assertEqual(store.get(record.id).revision, 2)

    def test_feedback_capacity_does_not_discard_idempotency_keys(self):
        with Store() as store:
            record = store.put(Record.create('Atlas'))
            for n in range(64):
                store.feedback(record.id, expected_revision=n+1, operation_id=f'op{n}', outcome='irrelevant')
            with self.assertRaises(ValueError):
                store.feedback(record.id, expected_revision=65, operation_id='overflow', outcome='helpful')
            replay = store.feedback(record.id, expected_revision=1, operation_id='op0', outcome='irrelevant')
            self.assertTrue(replay.duplicate)
            self.assertEqual(store.get(record.id).revision, 65)

    @unittest.skipUnless(os.name == 'posix', 'POSIX file permission check')
    def test_new_database_is_owner_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'memory.sqlite'
            with Store(path): pass
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class InitializationTests(unittest.TestCase):
    def test_failed_initialization_always_closes_connection(self):
        import sqlite3
        from unittest.mock import MagicMock, patch
        for failure_at in range(3):
            with self.subTest(failure_at=failure_at):
                connection = MagicMock()
                cursor = MagicMock()
                cursor.fetchone.return_value = (0,)
                connection.execute.side_effect = [cursor] * failure_at + [sqlite3.DatabaseError('synthetic init error')]
                with patch('agent_memory.store.connect', return_value=connection):
                    with self.assertRaises(sqlite3.DatabaseError):
                        Store()
                connection.close.assert_called_once_with()
