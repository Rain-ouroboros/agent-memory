"""Synthetic quickstart. No models, credentials or external services."""
from agent_memory import Record, Store, context, derive, recall

with Store(':memory:') as memory:
    source = memory.put(Record.create(
        'Project Atlas uses SQLite for local persistence.',
        namespace='atlas', source='project-docs',
        audience=('coding-agent',), trust='trusted',
    ))
    memory.put(derive('Atlas persistence is local.', [source]))
    recalled = recall(memory, 'Atlas persistence', namespace='atlas', principal='coding-agent')
    bundle = context(recalled, budget=2000)
    print(bundle.text)
    assert recalled.hits and bundle.included
    assert len(bundle.text) <= 2000
