"""Compatibility and prospective-summary regression controls."""
from collections import UserDict
from enum import IntEnum
import json

import pytest

from agent_memory import Record, Store, ModelOutput, derive, summarize, resolve_evidence
from agent_memory.retrieval import recall
from agent_memory.lifecycle import maintain
from agent_memory.context import context
from agent_memory.index import SearchIndex


@pytest.mark.parametrize('limit', [True, False, 1.5, float('nan'), float('inf')])
def test_index_rejects_invalid_limit_even_for_empty_query(limit):
    with SearchIndex() as index:
        with pytest.raises(ValueError, match='limit'):
            index.candidates('', limit=limit)


@pytest.mark.parametrize('budget', [True, False, 1.5, float('nan'), float('inf')])
def test_context_invalid_budget_does_not_invoke_measure(budget):
    calls = []
    with Store() as store:
        recalled = recall(store, 'synthetic', principal='reader', now=100)
        with pytest.raises(ValueError, match='budget'):
            context(recalled, budget=budget,
                    measure=lambda text: calls.append(text) or 0)
    assert calls == []


@pytest.mark.parametrize('amount', [True, False, 1.5, float('nan'), float('inf'), -1])
def test_context_rejects_invalid_measure_result(amount):
    with Store() as store:
        recalled = recall(store, 'synthetic', principal='reader', now=100)
        with pytest.raises(ValueError, match='measure result'):
            context(recalled, measure=lambda text: amount)


@pytest.mark.parametrize('output_kind', ['decimal_cost', 'duck_typed'])
def test_output_restrictions_are_checked_after_model_call(output_kind):
    from decimal import Decimal
    from types import SimpleNamespace

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
        with pytest.raises(ValueError):
            summarize(store, [parent], Callback(), now=100)
        assert calls == [(parent.text,)]
        assert store.snapshot() == before


class Model:
    def __init__(self):
        self.calls = 0

    def summarize(self, texts):
        self.calls += 1
        return ModelOutput('Synthetic summary')


def test_legacy_boolean_payload_remains_readable_by_consumers():
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
        assert parent.confidence == 1
        assert type(parent.confidence) is not bool
        assert parent.created_at == 0
        assert type(parent.created_at) is not bool
        assert store.get(saved.id) == parent
        result = recall(store, 'legacy', principal='reader', now=100)
        assert saved.id in [hit.record.id for hit in result.hits]
        assert maintain(store, now=100).candidates == ()
        model = Model()
        candidate, _ = summarize(store, [parent], model, now=100)
        assert model.calls == 1
        assert resolve_evidence(candidate, store.snapshot(), now=100).complete
        assert store._db.execute('SELECT payload FROM records').fetchone()[0] == before


def test_numeric_subclasses_preserve_strict_boolean_boundary():
    class Count(IntEnum):
        ONE = 1

    class Real(float):
        pass

    record = Record.create('Synthetic numeric', confidence=Real(.5),
                           created_at=Count.ONE)
    assert record.confidence == .5
    assert record.created_at == 1
    for field in ('confidence', 'created_at'):
        with pytest.raises(ValueError):
            Record.create('Synthetic invalid', **{field: True})


@pytest.mark.parametrize('audience', [set(['alice']), frozenset(['alice'])])
def test_audience_collections_and_mapping_deserialization(audience):
    record = Record.create('Synthetic audience', audience=audience)
    payload = record.to_dict()
    payload['audience'] = audience
    restored = Record.from_dict(UserDict(payload))
    assert set(restored.audience) == {'alice'}
    assert restored.id == record.id


def test_prospective_depth_rejected_before_model_call():
    with Store() as store:
        parent = store.put(Record.create('Synthetic root', created_at=0))
        for number in range(32):
            parent = store.put(derive('Synthetic level ' + str(number), [parent]))
        assert resolve_evidence(parent, store.snapshot(), now=100).complete
        model = Model()
        with pytest.raises(ValueError, match='prospective summary'):
            summarize(store, [parent], model, now=100)
        assert model.calls == 0


def test_combined_parent_budget_includes_prospective_root():
    with Store() as store:
        parents = [store.put(Record.create('Synthetic parent ' + str(i)))
                   for i in range(2)]
        model = Model()
        with pytest.raises(ValueError, match='prospective summary'):
            summarize(store, parents, model, now=100, max_nodes=2)
        assert model.calls == 0
        candidate, _ = summarize(store, parents, model, now=100,
                                 max_nodes=3, max_depth=1)
        assert model.calls == 1
        assert resolve_evidence(candidate, store.snapshot(), now=100,
                                max_nodes=3, max_depth=1).complete


@pytest.mark.parametrize('weight', [True, False, float('nan'), float('inf')])
def test_link_rejects_invalid_weight_without_writing(weight):
    with Store() as store:
        parent = store.put(Record.create('Synthetic link'))
        with pytest.raises(ValueError, match='weight'):
            store.link(parent, 'subject', 'relation', 'target', weight=weight)
        assert store.edges() == []
