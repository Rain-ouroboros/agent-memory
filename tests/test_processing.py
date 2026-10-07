from dataclasses import replace
import unittest
from agent_memory import (Record, SearchIndex, Store, classify_kind, derive,
                          propose_consolidation, recall, redact_secret_like)


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(); self.addCleanup(self.store.close)
        self.index = SearchIndex(); self.addCleanup(self.index.close)

    def save(self, text, **kwargs):
        return self.store.put(Record.create(text, created_at=100, audience=('agent',), **kwargs))

    def test_rebuildable_index_and_hybrid_routes(self):
        r = self.save('Atlas embeds SQLite')
        receipt = self.index.rebuild(self.store)
        self.assertEqual(receipt.revisions, ((r.id, 1),))
        result = recall(self.store, 'Atlas', principal='agent', index=self.index, now=100)
        self.assertEqual(result.hits[0].routes, ('fts5', 'lexical'))

    def test_stale_private_and_retired_index_hits_are_withheld(self):
        r = self.save('Atlas secret')
        self.index.rebuild(self.store)
        self.store.put(replace(r, audience=('other',)))
        self.assertEqual(recall(self.store, 'Atlas', principal='agent', index=self.index, now=100).hits, ())
        updated = self.store.get(r.id)
        self.store.put(replace(updated, audience=('agent',), status='retired'))
        self.assertEqual(recall(self.store, 'Atlas', principal='agent', index=self.index, now=100).hits, ())

    def test_updated_text_is_recalled_lexically_despite_stale_index(self):
        r = self.save('Original example')
        self.index.rebuild(self.store)
        self.store.put(replace(r, text='Atlas fresh evidence'))
        hit = recall(self.store, 'Atlas', principal='agent', index=self.index, now=100).hits[0]
        self.assertEqual(hit.routes, ('lexical',))

    def test_index_namespace_and_literal_query(self):
        self.save('Atlas SQLite')
        self.save('Atlas private', namespace='other')
        self.index.rebuild(self.store); self.index.rebuild(self.store, namespace='other')
        ids = self.index.candidates('Atlas OR " * NOT')
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.index.candidates('what did we do'), ())

    def test_deterministic_consolidation_proposals_never_write_or_raise_confidence(self):
        a = self.save('Atlas uses SQLite database storage', trust='trusted', confidence=.8)
        b = self.save('Atlas uses SQLite database storage safely', trust='trusted', confidence=.6)
        self.store.put(derive(a.text, [a], created_at=100))
        self.store.put(derive(b.text, [b], created_at=100))
        before = self.store.snapshot()
        proposals = propose_consolidation(self.store, now=100)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].revision, 0)
        self.assertEqual(proposals[0].confidence, .6)
        self.assertEqual(len(proposals[0].sources), 2)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(propose_consolidation(self.store, now=100), proposals)

    def test_primary_speech_and_unknown_trust_not_consolidated(self):
        a = self.save('Atlas uses SQLite database storage')
        b = self.save('Atlas uses SQLite database storage safely')
        self.store.put(derive(a.text, [a], created_at=100))
        self.store.put(derive(b.text, [b], created_at=100))
        self.assertEqual(propose_consolidation(self.store, now=100), ())

    def test_kind_classifier_is_heuristic_and_redaction_is_opt_in(self):
        self.assertEqual(classify_kind('We decided to use SQLite'), 'decision')
        self.assertEqual(classify_kind('Я предпочитаю локальное хранилище'), 'preference')
        self.assertEqual(classify_kind('I think the cache is stale'), 'belief')
        token = 'sk-' + 'syntheticFixture' * 3
        self.assertIn('[REDACTED_SECRET]', redact_secret_like('key '+token))
        r = self.save('Atlas '+token)
        self.assertIn(token, r.text)  # No silent alteration of evidence.
