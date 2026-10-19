"""Rain-authored controls for AM-01/02/06; original tests remain unchanged."""
from dataclasses import replace
import unittest

import threading

from agent_memory import Record, SourceRef, Store, ModelOutput, derive, summarize, resolve_evidence


class RankClockRegressionTests(unittest.TestCase):
    def test_direct_rank_rejects_invalid_clock(self):
        from agent_memory.retrieval import rank
        record = Record(id='synthetic-rank', text='Synthetic evidence', created_at=0)
        for now in (True, False, float('nan'), float('inf'), -float('inf'),
                    None, '0'):
            with self.subTest(now=repr(now)):
                with self.assertRaisesRegex(ValueError, '^now must be a finite number$'):
                    rank(record, overlap=1, now=now)

    def test_direct_rank_preserves_valid_scores(self):
        import math
        from agent_memory.retrieval import rank
        record = Record(id='synthetic-rank', text='Synthetic evidence',
                        kind='event', confidence=0.5, created_at=0)
        for now in (-86400, -0.5, 0, 0.0):
            with self.subTest(now=now):
                self.assertAlmostEqual(rank(record, overlap=1, now=now), 1.7)
        for now in (604800, 604800.0):
            with self.subTest(now=now):
                self.assertAlmostEqual(rank(record, overlap=1, now=now),
                                       1.2 + 0.3 / math.e)


class ModelSnapshotRegressionTests(unittest.TestCase):
    def test_callback_cannot_replace_parent_policy_or_exact_refs(self):
        with Store() as store:
            parent = store.put(Record.create('Synthetic private evidence',
                                            audience=('alice',), trust='untrusted'))
            replacement = store.put(Record.create('Synthetic public evidence',
                                                 audience=('*',), trust='trusted'))
            parents = [parent]
            seen = []
            class Callback:
                def summarize(self, texts):
                    seen.append(texts)
                    parents[:] = [replacement]
                    return ModelOutput('Synthetic summary', input_tokens=0,
                                       output_tokens=1, cost=0)
            before = store.snapshot()
            candidate, output = summarize(store, parents, Callback(), now=100)
            self.assertEqual(seen, [(parent.text,)])
            self.assertEqual(candidate.sources, (SourceRef(parent.id, parent.revision),))
            self.assertEqual(candidate.audience, ('alice',))
            self.assertEqual(candidate.trust, 'untrusted')
            self.assertEqual(candidate.text, output.text)
            self.assertEqual(candidate.revision, 0)
            self.assertEqual(store.snapshot(), before)

    def test_callback_write_is_unlocked_and_invalidates_old_refs(self):
        with Store() as store:
            parent = store.put(Record.create('Synthetic original evidence'))
            errors, workers = [], []
            finished = threading.Event()
            class Callback:
                def summarize(self, texts):
                    def write():
                        try:
                            store.put(replace(parent, text='Synthetic changed evidence'))
                        except Exception as error:
                            errors.append(type(error).__name__)
                        finally:
                            finished.set()
                    worker = threading.Thread(target=write)
                    workers.append(worker)
                    worker.start()
                    if not finished.wait(2):
                        raise AssertionError('store lock spans callback')
                    return ModelOutput('Synthetic summary')
            try:
                candidate, _ = summarize(store, [parent], Callback(), now=100)
            finally:
                for worker in workers:
                    worker.join(3)
            self.assertEqual(errors, [])
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(candidate.sources, (SourceRef(parent.id, 1),))
            self.assertFalse(resolve_evidence(candidate, store.snapshot(), now=100).complete)
            self.assertEqual(len(store.snapshot()), 1)
            self.assertEqual(store.get(parent.id, revision=1), parent)

    def test_invalid_parent_preflight_never_invokes_callback(self):
        with Store() as store:
            active = store.put(Record.create('Synthetic active', created_at=0))
            retired = store.put(Record.create('Synthetic retired', status='retired'))
            expired = store.put(Record.create('Synthetic expired', expires_at=100))
            incomplete = store.put(Record.create('Synthetic incomplete', complete=False))
            dangling = store.put(derive('Synthetic dangling', [replace(active, revision=99)]))
            other = store.put(Record.create('Synthetic other namespace', namespace='other'))
            calls = []
            class Callback:
                def summarize(self, texts):
                    calls.append(texts)
                    return ModelOutput('Synthetic summary')
            before = store.snapshot()
            for parents in ([], [retired], [expired], [incomplete], [dangling],
                            [replace(active, text='Unsaved edit')], [active, other],
                            [Record.create('Synthetic unsaved')]):
                with self.subTest(parent_count=len(parents)):
                    with self.assertRaises(ValueError):
                        summarize(store, parents, Callback(), now=100)
            self.assertEqual(calls, [])
            self.assertEqual(store.snapshot(), before)
            candidate, _ = summarize(store, [active], Callback(), now=100)
            self.assertEqual(len(calls), 1)
            self.assertEqual(candidate.sources, (SourceRef(active.id, 1),))


class ModelOutputRegressionTests(unittest.TestCase):
    def test_invalid_outputs_do_not_leak_or_persist(self):
        private_text = 'Synthetic private output marker'
        outputs = [
            private_text, {'text': private_text}, None,
            ModelOutput(''), ModelOutput('   '), ModelOutput(123),
        ]
        for field in ('input_tokens', 'output_tokens'):
            for value in (True, False, -1, 1.5, '1', float('inf')):
                outputs.append(ModelOutput(private_text, **{field: value}))
        for value in (True, False, -1, '1', float('nan'), float('inf')):
            outputs.append(ModelOutput(private_text, cost=value))
        with Store() as store:
            parent = store.put(Record.create('Synthetic source'))
            before = store.snapshot()
            class Callback:
                def summarize(self, texts):
                    return self.output
            callback = Callback()
            for output in outputs:
                callback.output = output
                with self.subTest(output_type=type(output)):
                    with self.assertRaises(ValueError) as caught:
                        summarize(store, [parent], callback, now=100)
                    self.assertNotIn(private_text, str(caught.exception))
                    self.assertEqual(store.snapshot(), before)
            callback.output = ModelOutput(private_text, input_tokens=0,
                                          output_tokens=0, cost=0.0)
            candidate, result = summarize(store, [parent], callback, now=100)
            self.assertIs(result, callback.output)
            self.assertEqual(candidate.text, private_text)
            self.assertEqual(store.snapshot(), before)


class ProvenanceBoundaryRegressionTests(unittest.TestCase):
    def test_shared_ancestor_counts_once_at_exact_node_budget(self):
        with Store() as store:
            root = store.put(Record.create('Synthetic root', trust='trusted',
                                           audience=('alice',)))
            left = store.put(derive('Synthetic left', [root]))
            right = store.put(derive('Synthetic right', [root]))
            candidate = derive('Synthetic diamond', [left, right])
            snapshot = store.snapshot()
            exact = resolve_evidence(candidate, snapshot, now=100,
                                     max_nodes=4, max_depth=2)
            self.assertTrue(exact.complete)
            self.assertEqual(exact.witnesses, (root.id,))
            self.assertEqual(exact.audience, ('alice',))
            self.assertEqual(exact.trust, 'trusted')
            for bounds in ({'max_nodes': 3, 'max_depth': 2},
                           {'max_nodes': 4, 'max_depth': 1}):
                evidence = resolve_evidence(candidate, snapshot, now=100, **bounds)
                self.assertFalse(evidence.complete)
                self.assertIn('limit', evidence.issues)
                self.assertEqual(evidence.audience, ())

    def test_long_shared_path_is_checked_in_both_parent_orders(self):
        # Regression scenario supplied by Codex's independent review;
        # this implementation of the control is Rain-authored.
        with Store() as store:
            root = store.put(Record.create('Synthetic terminal witness', trust='trusted'))
            shared = store.put(derive('Synthetic shared', [root]))
            chain = shared
            for index in range(3):
                chain = store.put(derive(f'Synthetic chain {index}', [chain]))
            snapshot = store.snapshot()
            before = dict(snapshot)
            for parents in ([shared, chain], [chain, shared]):
                candidate = derive('Synthetic candidate', parents)
                rejected = resolve_evidence(candidate, snapshot, now=100,
                                            max_nodes=6, max_depth=3)
                self.assertFalse(rejected.complete)
                self.assertIn('limit', rejected.issues)
                accepted = resolve_evidence(candidate, snapshot, now=100,
                                            max_nodes=6, max_depth=5)
                self.assertTrue(accepted.complete)
                self.assertEqual(accepted.witnesses, (root.id,))
                self.assertEqual(accepted.trust, 'trusted')
            self.assertEqual(store.snapshot(), before)

    def test_zero_depth_accepts_leaf_but_rejects_source_edge(self):
        with Store() as store:
            root = store.put(Record.create('Synthetic leaf'))
            snapshot = store.snapshot()
            self.assertTrue(resolve_evidence(root, snapshot, now=0,
                                             max_nodes=1, max_depth=0).complete)
            candidate = derive('Synthetic derived', [root])
            evidence = resolve_evidence(candidate, snapshot, now=0,
                                        max_nodes=2, max_depth=0)
            self.assertFalse(evidence.complete)
            self.assertIn('limit', evidence.issues)


class ConsolidationReadinessRegressionTests(unittest.TestCase):
    def test_prospective_candidate_limits_and_compatible_receipt(self):
        from agent_memory import propose_consolidation
        from agent_memory.consolidation import ConsolidationReceipt
        with Store() as store:
            roots = [
                store.put(Record.create(f'Synthetic primary {index}',
                                        trust='trusted', audience=('alice',),
                                        created_at=0))
                for index in range(2)
            ]
            parents = [
                store.put(derive('Synthetic identical summary evidence', [root],
                                 anchor=str(index), created_at=0))
                for index, root in enumerate(roots)
            ]
            before = store.snapshot()
            # Each parent needs two nodes and one edge; the proposal needs
            # five unique nodes and two edges. No provider is involved.
            for bounds in ({'max_nodes': 4, 'max_depth': 2},
                           {'max_nodes': 5, 'max_depth': 1}):
                for parent in parents:
                    self.assertTrue(resolve_evidence(
                        parent, before, now=100, **bounds).complete)
                plain = propose_consolidation(store, now=100, **bounds)
                self.assertIsInstance(plain, tuple)
                self.assertEqual(plain, ())
                receipt = propose_consolidation(
                    store, now=100, with_receipt=True, **bounds)
                self.assertIsInstance(receipt, ConsolidationReceipt)
                self.assertEqual(receipt.proposals, ())
                self.assertEqual(len(receipt.skipped), 1)
                skipped = receipt.skipped[0]
                self.assertEqual(skipped.reason, 'provenance_limit')
                self.assertIn('limit', skipped.issues)
                self.assertEqual(set(skipped.sources),
                                 {SourceRef(p.id, p.revision) for p in parents})
                self.assertEqual(store.snapshot(), before)
            accepted = propose_consolidation(
                store, now=100, max_nodes=5, max_depth=2, with_receipt=True)
            self.assertEqual(accepted.skipped, ())
            self.assertEqual(len(accepted.proposals), 1)
            candidate = accepted.proposals[0]
            self.assertEqual(candidate.revision, 0)
            self.assertEqual(candidate.created_at, 100)
            self.assertEqual(candidate.audience, ('alice',))
            self.assertTrue(resolve_evidence(candidate, before, now=100,
                                             max_nodes=5, max_depth=2).complete)
            self.assertEqual(propose_consolidation(
                store, now=100, max_nodes=5, max_depth=2), accepted.proposals)
            self.assertEqual(store.snapshot(), before)


class NumericAPIRegressionTests(unittest.TestCase):
    def test_invalid_numbers_fail_before_snapshot_or_callback(self):
        from agent_memory import maintain, propose_consolidation, recall

        class UnreadStore:
            def snapshot(self, **kwargs):
                raise AssertionError('invalid input reached snapshot')

        class UncalledModel:
            def summarize(self, texts):
                raise AssertionError('invalid input reached model')

        store = UnreadStore()
        record = Record.create('Synthetic numeric control')
        invalid_numbers = (True, False, '100', float('nan'), float('inf'))
        for value in invalid_numbers:
            calls = (
                lambda: resolve_evidence(record, {}, now=value),
                lambda: recall(store, 'synthetic', principal='alice', now=value),
                lambda: maintain(store, now=value),
                lambda: propose_consolidation(store, now=value),
                lambda: summarize(store, [record], UncalledModel(), now=value),
            )
            for call in calls:
                with self.subTest(value_type=type(value)):
                    with self.assertRaises(ValueError):
                        call()

        for value in (True, False, 1.5, '1', -1):
            for field in ('max_nodes', 'max_depth'):
                with self.subTest(api='resolver', field=field, value=value):
                    with self.assertRaises(ValueError):
                        resolve_evidence(record, {}, now=100, **{field: value})
            for field in ('limit', 'graph_depth', 'graph_fanout',
                          'graph_max_nodes', 'excerpt_chars'):
                with self.subTest(api='recall', field=field, value=value):
                    with self.assertRaises(ValueError):
                        recall(store, 'synthetic', principal='alice',
                               now=100, **{field: value})
            for field in ('max_records', 'max_nodes', 'max_depth'):
                with self.subTest(api='proposals', field=field, value=value):
                    with self.assertRaises(ValueError):
                        propose_consolidation(store, now=100, **{field: value})
            for field in ('max_nodes', 'max_depth'):
                with self.subTest(api='summarize', field=field, value=value):
                    with self.assertRaises(ValueError):
                        summarize(store, [record], UncalledModel(), now=100,
                                  **{field: value})

    def test_revision_validation_preserves_history_and_cas(self):
        from agent_memory import ConflictError
        with Store() as store:
            initial = Record.create('Synthetic revision source')
            saved = store.put(initial, expected_revision=0)
            before = store.snapshot()
            for value in (True, False, 1.5, '1', -1):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        store.put(saved, expected_revision=value)
                    with self.assertRaises(ValueError):
                        store.get(saved.id, revision=value)
                    self.assertEqual(store.snapshot(), before)
            with self.assertRaises(ValueError):
                store.get(saved.id, revision=0)
            updated = store.put(replace(saved, text='Synthetic revised source'),
                                expected_revision=1)
            self.assertEqual(updated.revision, 2)
            self.assertEqual(store.get(saved.id, revision=1), saved)
            self.assertEqual(store.get(saved.id, revision=2), updated)
            with self.assertRaises(ConflictError):
                store.put(saved, expected_revision=1)
            self.assertEqual(store.get(saved.id), updated)

    def test_valid_zero_clock_and_recall_boundaries(self):
        from agent_memory import maintain, propose_consolidation, recall
        with Store() as store:
            source = store.put(Record.create('Synthetic boundary source',
                                             audience=('alice',), created_at=0))
            before = store.snapshot()
            result = recall(store, 'synthetic', principal='alice', now=0,
                            limit=0, graph_depth=0, graph_fanout=1,
                            graph_max_nodes=1, excerpt_chars=1)
            self.assertEqual(result.hits, ())
            maintain(store, now=0)
            self.assertEqual(propose_consolidation(
                store, now=0, max_records=1, max_nodes=1, max_depth=0), ())
            self.assertTrue(resolve_evidence(
                source, before, now=0, max_nodes=1, max_depth=0).complete)
            self.assertEqual(store.snapshot(), before)


class MetadataRegressionTests(unittest.TestCase):
    def build(self, route, **fields):
        if route == 'direct':
            return Record(id='fixture', text='Synthetic source', **fields)
        if route == 'create':
            return Record.create('Synthetic source', **fields)
        payload = {'id': 'fixture', 'text': 'Synthetic source'}
        payload.update(fields)
        return Record.from_dict(payload)

    def test_invalid_containers_are_rejected_without_consumption(self):
        for route in ('direct', 'create', 'dict'):
            for field in ('audience', 'sources'):
                consumed = []
                def values():
                    consumed.append(True)
                    yield 'private-fixture-value'
                for value in ('private-fixture-value', b'private-fixture-value',
                              {'private-fixture-value': 1}, None, values()):
                    with self.subTest(route=route, field=field, kind=type(value)):
                        with self.assertRaises(ValueError) as caught:
                            self.build(route, **{field: value})
                        self.assertIn(field, str(caught.exception))
                        self.assertNotIn('private-fixture-value', str(caught.exception))
                        self.assertEqual(consumed, [])

    def test_valid_container_boundaries_and_round_trip(self):
        for route in ('direct', 'create', 'dict'):
            for container in (list, tuple):
                for audience in ((), ('*',), ('alice', 'bob', 'alice')):
                    refs = ({'id': 'parent', 'revision': 1},) if route == 'dict' else (
                        SourceRef('parent', 1),)
                    record = self.build(route, audience=container(audience),
                                        sources=container(refs))
                    self.assertEqual(record.audience, tuple(sorted(set(audience))))
                    self.assertEqual(record.sources, (SourceRef('parent', 1),))
                    self.assertEqual(Record.from_dict(record.to_dict()), record)
                with self.assertRaises(ValueError):
                    self.build(route, audience=container(('*', 'alice')))

    def test_numeric_metadata_rejects_bool_strings_and_nonfinite(self):
        for route in ('direct', 'create', 'dict'):
            for field in ('confidence', 'created_at', 'occurred_at', 'expires_at'):
                for value in (True, False, 'private-fixture-value',
                              float('nan'), float('inf'), -float('inf')):
                    with self.subTest(route=route, field=field, value_type=type(value)):
                        if route == 'dict' and isinstance(value, bool):
                            record = self.build(route, **{field: value})
                            self.assertEqual(getattr(record, field), int(value))
                            self.assertNotIsInstance(getattr(record, field), bool)
                            continue
                        with self.assertRaises(ValueError) as caught:
                            self.build(route, **{field: value})
                        self.assertIn(field, str(caught.exception))
                        self.assertNotIn('private-fixture-value', str(caught.exception))
            for revision in (True, False, 1.5, -1, '1'):
                with self.assertRaises(ValueError):
                    self.build(route, revision=revision)
            record = self.build(route, confidence=0, created_at=0,
                                occurred_at=None, expires_at=None, revision=0)
            self.assertEqual(record.confidence, 0)
            self.assertEqual(replace(record, confidence=1).confidence, 1)

    def test_source_revisions_are_positive_integers(self):
        for route in ('direct', 'create', 'dict'):
            for revision in (True, False, 0, -1, 1.5, '1'):
                ref = {'id': 'parent', 'revision': revision} if route == 'dict' else (
                    SourceRef('parent', revision))
                with self.subTest(route=route, revision_type=type(revision)):
                    with self.assertRaises(ValueError):
                        self.build(route, sources=[ref])
