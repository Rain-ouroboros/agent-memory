"""Compatibility and prospective-summary regression controls."""
from collections import UserDict
from enum import IntEnum
import json
import unittest

from agent_memory import Record, Store, ModelOutput, derive, summarize, resolve_evidence
from agent_memory.retrieval import recall
from agent_memory.lifecycle import maintain
from agent_memory.context import context
from agent_memory.index import SearchIndex


class Model:
    def __init__(self):
        self.calls = 0

    def summarize(self, texts):
        self.calls += 1
        return ModelOutput('Synthetic summary')


class CompatibilityRegressions(unittest.TestCase):
    def test_index_rejects_invalid_limit_even_for_empty_query(self):
        for limit in (True, False, 1.5, float('nan'), float('inf')):
            with self.subTest(limit=limit):
                with SearchIndex() as index:
                    with self.assertRaisesRegex(ValueError, 'limit'):
                        index.candidates('', limit=limit)

    def test_context_invalid_budget_does_not_invoke_measure(self):
        for budget in (True, False, 1.5, float('nan'), float('inf')):
            with self.subTest(budget=budget):
                calls = []
                with Store() as store:
                    recalled = recall(store, 'synthetic', principal='reader', now=100)
                    with self.assertRaisesRegex(ValueError, 'budget'):
                        context(recalled, budget=budget,
                                measure=lambda text: calls.append(text) or 0)
                self.assertEqual(calls, [])

    def test_context_rejects_invalid_measure_result(self):
        for amount in (True, False, 1.5, float('nan'), float('inf'), -1):
            with self.subTest(amount=amount):
                with Store() as store:
                    recalled = recall(store, 'synthetic', principal='reader', now=100)
                    with self.assertRaisesRegex(ValueError, 'measure result'):
                        context(recalled, measure=lambda text: amount)

    def test_output_restrictions_are_checked_after_model_call(self):
        from decimal import Decimal
        from types import SimpleNamespace

        for output_kind in ('decimal_cost', 'duck_typed'):
            with self.subTest(output_kind=output_kind):
                calls = []

                class Callback:
                    def summarize(self, texts):
                        calls.append(texts)
                        if output_kind == 'decimal_cost':
                            return ModelOutput('Synthetic summary', cost=Decimal('0.01'))
                        return SimpleNamespace(text='Synthetic summary')

                with Store() as store:
                    parent = store.put(Record.create('Synthetic source', created_at=0))
                    before = store.snapshot()
                    with self.assertRaises(ValueError):
                        summarize(store, [parent], Callback(), now=100)
                    self.assertEqual(calls, [(parent.text,)])
                    self.assertEqual(store.snapshot(), before)

    def test_legacy_boolean_payload_remains_readable_by_consumers(self):
        with Store() as store:
            saved = store.put(Record.create('Synthetic legacy evidence', created_at=0,
                                            audience=('reader',)))
            payload = saved.to_dict()
            payload['confidence'] = True
            payload['created_at'] = False
            # Reproduce a persisted legacy payload without weakening new writes.
            with store._db:
                store._db.execute(
                    'UPDATE records SET payload=? WHERE namespace=? AND id=?',
                    (json.dumps(payload), saved.namespace, saved.id))
            before = store._db.execute('SELECT payload FROM records').fetchone()[0]
            parent = store.snapshot()[saved.id]
            self.assertEqual(parent.confidence, 1)
            self.assertIsNot(type(parent.confidence), bool)
            self.assertEqual(parent.created_at, 0)
            self.assertIsNot(type(parent.created_at), bool)
            self.assertEqual(store.get(saved.id), parent)
            result = recall(store, 'legacy', principal='reader', now=100)
            self.assertIn(saved.id, [hit.record.id for hit in result.hits])
            self.assertEqual(maintain(store, now=100).candidates, ())
            model = Model()
            candidate, _ = summarize(store, [parent], model, now=100)
            self.assertEqual(model.calls, 1)
            self.assertTrue(resolve_evidence(candidate, store.snapshot(), now=100).complete)
            self.assertEqual(
                store._db.execute('SELECT payload FROM records').fetchone()[0], before)

    def test_corrupt_confidence_identifies_row_without_disclosing_payload(self):
        import traceback

        for reader in ('get', 'history', 'snapshot', 'recall'):
            for bad_confidence in (float('nan'), 'PRIVATE_BAD_VALUE'):
                with self.subTest(reader=reader, bad_confidence=bad_confidence):
                    with Store() as store:
                        saved = store.put(Record.create('PRIVATE_RECORD_TEXT', created_at=0,
                                                        audience=('reader',)))
                        payload = saved.to_dict()
                        payload['confidence'] = bad_confidence
                        # The diagnostic must use the database key, not the payload's ID.
                        payload['id'] = 'PRIVATE_PAYLOAD_ID'
                        encoded = json.dumps(payload)
                        with store._db:
                            for table in ('records', 'history'):
                                store._db.execute(
                                    f'UPDATE {table} SET payload=? WHERE namespace=? AND id=?',
                                    (encoded, saved.namespace, saved.id))
                        with self.assertRaises(ValueError) as caught:
                            try:
                                if reader == 'get':
                                    store.get(saved.id)
                                elif reader == 'history':
                                    store.get(saved.id, revision=saved.revision)
                                elif reader == 'snapshot':
                                    store.snapshot()
                                else:
                                    recall(store, 'record', principal='reader', now=100)
                            except ValueError as exc:
                                # Capture the real traceback before assertRaises clears it.
                                rendered = ''.join(traceback.format_exception(
                                    type(exc), exc, exc.__traceback__))
                                raise
                        self.assertEqual(
                            str(caught.exception),
                            f'record {saved.id!r}: invalid field confidence')
                        for private in ('PRIVATE_RECORD_TEXT', 'PRIVATE_BAD_VALUE',
                                        'PRIVATE_PAYLOAD_ID', 'NaN'):
                            self.assertNotIn(private, rendered)
                        self.assertEqual(
                            store._db.execute('SELECT payload FROM records').fetchone()[0],
                            encoded)
                        self.assertEqual(
                            store._db.execute('SELECT payload FROM history').fetchone()[0],
                            encoded)

    def test_numeric_subclasses_preserve_strict_boolean_boundary(self):
        class Count(IntEnum):
            ONE = 1

        class Real(float):
            pass

        record = Record.create('Synthetic numeric', confidence=Real(.5),
                               created_at=Count.ONE)
        self.assertEqual(record.confidence, .5)
        self.assertEqual(record.created_at, 1)
        for field in ('confidence', 'created_at'):
            with self.assertRaises(ValueError):
                Record.create('Synthetic invalid', **{field: True})

    def test_audience_collections_and_mapping_deserialization(self):
        for audience in (set(['alice']), frozenset(['alice'])):
            with self.subTest(audience=audience):
                record = Record.create('Synthetic audience', audience=audience)
                payload = record.to_dict()
                payload['audience'] = audience
                restored = Record.from_dict(UserDict(payload))
                self.assertEqual(set(restored.audience), {'alice'})
                self.assertEqual(restored.id, record.id)

    def test_prospective_depth_rejected_before_model_call(self):
        with Store() as store:
            parent = store.put(Record.create('Synthetic root', created_at=0))
            for number in range(32):
                parent = store.put(derive('Synthetic level ' + str(number), [parent]))
            self.assertTrue(resolve_evidence(parent, store.snapshot(), now=100).complete)
            model = Model()
            with self.assertRaisesRegex(ValueError, 'prospective summary'):
                summarize(store, [parent], model, now=100)
            self.assertEqual(model.calls, 0)

    def test_combined_parent_budget_includes_prospective_root(self):
        with Store() as store:
            parents = [store.put(Record.create('Synthetic parent ' + str(i)))
                       for i in range(2)]
            model = Model()
            with self.assertRaisesRegex(ValueError, 'prospective summary'):
                summarize(store, parents, model, now=100, max_nodes=2)
            self.assertEqual(model.calls, 0)
            candidate, _ = summarize(store, parents, model, now=100,
                                     max_nodes=3, max_depth=1)
            self.assertEqual(model.calls, 1)
            self.assertTrue(resolve_evidence(candidate, store.snapshot(), now=100,
                                            max_nodes=3, max_depth=1).complete)

    def test_link_rejects_invalid_weight_without_writing(self):
        for weight in (True, False, float('nan'), float('inf')):
            with self.subTest(weight=weight):
                with Store() as store:
                    parent = store.put(Record.create('Synthetic link'))
                    with self.assertRaisesRegex(ValueError, 'weight'):
                        store.link(parent, 'subject', 'relation', 'target', weight=weight)
                    self.assertEqual(store.edges(), [])
