from dataclasses import FrozenInstanceError, replace
import unittest

from agent_memory import (ConflictError, IngestReceipt, Maintenance, ModelOutput,
                          Record, Store, ingest, ingest_batch, maintain, summarize)


class Source:
    def __init__(self, records):
        self.items = records

    def records(self):
        return iter(self.items)


class RestartReceiptTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.x = Record.create('first evidence', created_at=100, audience=('agent',))
        self.y = Record.create('second evidence', created_at=100, audience=('agent',))

    def dump(self):
        return tuple(self.store._db.iterdump())

    def test_batch_duplicates_and_repeated_source(self):
        source = Source([self.x, self.x, self.y])
        receipt = ingest_batch(self.store, source)
        self.assertEqual(receipt, IngestReceipt(
            ((self.x.id, 1), (self.y.id, 1)), ((self.x.id, 1),), ()))
        self.assertEqual(len(self.store.snapshot()), 2)
        before = self.dump()
        repeated = ingest_batch(self.store, source)
        self.assertEqual(repeated.inserted, ())
        self.assertEqual(repeated.duplicates,
                         ((self.x.id, 1), (self.x.id, 1), (self.y.id, 1)))
        self.assertEqual(repeated.conflicts, ())
        unique_repeat = ingest_batch(self.store, Source([self.x, self.y]))
        self.assertEqual(unique_repeat.duplicates, ((self.x.id, 1), (self.y.id, 1)))
        self.assertEqual(unique_repeat.inserted, ())
        self.assertEqual(self.dump(), before)
        with self.assertRaises(FrozenInstanceError):
            receipt.inserted = ()

    def test_metadata_conflicts_do_not_write_and_continue(self):
        saved = self.store.put(self.x)
        for changed in (replace(self.x, trust='trusted'),
                        replace(self.x, audience=('other',))):
            with self.subTest(record=changed):
                before = self.dump()
                receipt = ingest_batch(self.store, Source([changed]))
                self.assertEqual(receipt, IngestReceipt((), (), ((saved.id, 1),)))
                self.assertEqual(self.dump(), before)
                self.assertEqual(self.store.get(saved.id), saved)
        receipt = ingest_batch(self.store, Source([
            replace(self.x, audience=('other',)), self.y]))
        self.assertEqual(receipt.conflicts, ((saved.id, 1),))
        self.assertEqual(receipt.inserted, ((self.y.id, 1),))
        self.assertEqual(self.store.get(saved.id), saved)

    def test_created_at_and_revision_are_ignored_without_updates(self):
        saved = self.store.put(self.x)
        saved = self.store.put(replace(saved, text='current evidence'))
        before = self.dump()
        for revision in (0, 1, saved.revision, 99):
            with self.subTest(revision=revision):
                receipt = ingest_batch(self.store, Source([
                    replace(saved, revision=revision, created_at=200)]))
                self.assertEqual(receipt, IngestReceipt((), ((saved.id, 2),), ()))
                self.assertEqual(self.store.get(saved.id), saved)
                self.assertEqual(self.dump(), before)
                conflict = ingest_batch(self.store, Source([
                    replace(saved, revision=revision, text='different evidence')]))
                self.assertEqual(conflict, IngestReceipt((), (), ((saved.id, 2),)))
                self.assertEqual(self.dump(), before)

    def test_invalid_element_raises_and_preserves_prior_progress(self):
        with self.assertRaises(ValueError):
            ingest_batch(self.store, Source([self.x, object(), self.y]))
        self.assertEqual(self.store.get(self.x.id).revision, 1)
        self.assertIsNone(self.store.get(self.y.id))

    def test_source_failure_propagates_and_can_retry_saved_prefix(self):
        failure = RuntimeError('synthetic source interruption')

        def interrupted():
            yield self.x
            raise failure

        with self.assertRaises(RuntimeError) as caught:
            ingest_batch(self.store, Source(interrupted()))
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.store.get(self.x.id).revision, 1)
        receipt = ingest_batch(self.store, Source([self.x, self.y]))
        self.assertEqual(receipt.duplicates, ((self.x.id, 1),))
        self.assertEqual(receipt.inserted, ((self.y.id, 1),))

    def test_unsaved_id_with_nonzero_revision_is_inserted_as_first_revision(self):
        receipt = ingest_batch(self.store, Source([replace(self.x, revision=7)]))
        self.assertEqual(receipt, IngestReceipt(((self.x.id, 1),), (), ()))
        self.assertEqual(self.store.get(self.x.id).revision, 1)

    def test_legacy_ingest_still_stops_at_first_duplicate(self):
        with self.assertRaises(ConflictError):
            ingest(self.store, Source([self.x, self.x, self.y]))
        self.assertEqual(self.store.get(self.x.id).revision, 1)
        self.assertIsNone(self.store.get(self.y.id))

    def test_same_summary_text_distinguishes_parent_sets(self):
        class ConstantModel:
            def summarize(self, texts):
                return ModelOutput('identical summary')

        x = self.store.put(self.x)
        y = self.store.put(self.y)
        first, _ = summarize(self.store, [x], ConstantModel(), now=100)
        second, _ = summarize(self.store, [y], ConstantModel(), now=100)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.sources, second.sources)

    def test_non_conflict_store_errors_propagate(self):
        class BrokenStore(Store):
            def put(self, record, **kwargs):
                raise RuntimeError('synthetic storage failure')

        store = BrokenStore()
        self.addCleanup(store.close)
        with self.assertRaisesRegex(RuntimeError, 'synthetic storage failure'):
            ingest_batch(store, Source([self.x]))


class RacingStore(Store):
    race_id = None

    def snapshot(self, **kwargs):
        records = super().snapshot(**kwargs)
        if self.race_id is not None:
            record = records[self.race_id]
            self.race_id = None
            self.put(replace(record, text='concurrent revision'))
        return records


class MaintenanceReceiptTests(unittest.TestCase):
    def setUp(self):
        self.store = RacingStore()
        self.addCleanup(self.store.close)
        self.records = [self.store.put(Record.create(
            text, created_at=100, expires_at=101))
            for text in ('before race', 'racing record', 'after race')]

    def test_stale_candidate_is_reported_and_next_run_retires_it(self):
        first, racing, last = self.records
        self.store.race_id = racing.id
        receipt = maintain(self.store, now=200, dry_run=False)
        self.assertEqual(set(receipt.candidates), {(r.id, 1) for r in self.records})
        self.assertEqual(set(receipt.applied), {(first.id, 2), (last.id, 2)})
        self.assertEqual(receipt.conflicted, ((racing.id, 1),))
        current = self.store.get(racing.id)
        self.assertEqual(current.revision, 2)
        self.assertEqual(current.text, 'concurrent revision')
        self.assertEqual(current.status, 'active')
        for record in (first, last):
            self.assertEqual(self.store.get(record.id).status, 'retired')
        repeated = maintain(self.store, now=200, dry_run=False)
        self.assertEqual(repeated.candidates, ((racing.id, 2),))
        self.assertEqual(repeated.applied, ((racing.id, 3),))
        self.assertEqual(repeated.conflicted, ())
        self.assertEqual(self.store.get(racing.id).status, 'retired')

    def test_dry_run_has_no_conflicts_or_writes(self):
        before = tuple(self.store._db.iterdump())
        receipt = maintain(self.store, now=200, dry_run=True)
        self.assertEqual(receipt.applied, ())
        self.assertEqual(receipt.conflicted, ())
        self.assertEqual(len(receipt.candidates), 3)
        self.assertEqual(tuple(self.store._db.iterdump()), before)
        self.assertEqual(Maintenance(True, (), ()).conflicted, ())

    def test_invalid_dry_run_rejected_before_snapshot(self):
        self.store.race_id = self.records[1].id
        for invalid in (0, 1, None, 'false'):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                maintain(self.store, now=200, dry_run=invalid)
        self.assertEqual(self.store.race_id, self.records[1].id)
        self.assertEqual(self.store.get(self.records[1].id).revision, 1)

    def test_other_maintenance_errors_are_not_swallowed(self):
        class BrokenMaintenanceStore(Store):
            def put(self, record, **kwargs):
                if record.status == 'retired':
                    raise RuntimeError('synthetic maintenance failure')
                return super().put(record, **kwargs)

        store = BrokenMaintenanceStore()
        self.addCleanup(store.close)
        store.put(Record.create('expired', created_at=100, expires_at=101))
        with self.assertRaisesRegex(RuntimeError, 'synthetic maintenance failure'):
            maintain(store, now=200, dry_run=False)


if __name__ == '__main__':
    unittest.main()
