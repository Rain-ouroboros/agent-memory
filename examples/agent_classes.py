"""Two host styles sharing the same library, with isolated namespaces."""
from agent_memory import ModelOutput, Record, Store, context, ingest, recall, summarize


class ResearchSource:
    def records(self):
        yield Record.create('Synthetic paper: Atlas supports local persistence.',
                            namespace='research', source='paper-excerpt',
                            audience=('researcher',), trust='untrusted')


class OfflineSummarizer:
    def summarize(self, texts):
        return ModelOutput('Atlas supports local persistence.', input_tokens=12, output_tokens=6)


def coding_agent_context(memory):
    return context(recall(memory, 'Atlas storage', namespace='coding', principal='coder'))


def research_agent_context(memory):
    return context(recall(memory, 'Atlas persistence', namespace='research', principal='researcher'))


with Store() as memory:
    memory.put(Record.create('Atlas storage uses SQLite.', namespace='coding',
                            source='tool-result', audience=('coder',), trust='trusted'))
    papers = list(ingest(memory, ResearchSource()))
    candidate, usage = summarize(memory, papers, OfflineSummarizer())
    assert candidate.trust == 'untrusted' and usage.cost is None
    memory.put(candidate)  # Explicit persistence chosen by the host.
    coding = coding_agent_context(memory)
    research = research_agent_context(memory)
    assert coding.included and research.included
    assert 'paper-excerpt' not in coding.text
    assert 'tool-result' not in research.text
    print('Coding and research integrations passed; no network calls.')
