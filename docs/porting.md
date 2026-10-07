# From an agent runtime to an independent library

This repository contains reusable memory mechanisms extracted and adapted from an agent runtime, plus a standalone persistence and integration layer. The public package contains code and synthetic examples.

The upstream application repository is private; this extracted package is public.

Source reference: `e76d84ec8f28bc4ce680ec977c6eadaddbb289a0` (upstream 4.15.96). Source hashes for directly reused algorithms are recorded in [NOTICE](../NOTICE).

| Original source | Standalone destination | Treatment |
| :--- | :--- | :--- |
| `ouroboros/tools/recall_terms.py` | `agent_memory.terms` | Lexical algorithms retained; private historical commentary removed |
| `ouroboros/tools/recall_excerpt.py` | `agent_memory.excerpt` | Contiguous passage selection retained; import relocated |
| `ouroboros/memory/core.py` | `agent_memory.redaction` | Secret-pattern helper extracted; no automatic capture policy implied |
| `ouroboros/memory/sleep/phases/extract.py` | `agent_memory.extraction` | Pure kind classifier retained; source-specific confidence policy omitted |
| `ouroboros/memory/sleep/phases/consolidate.py` | `agent_memory.consolidation` | Token/Jaccard clustering retained; candidates require provenance, trust and audience checks; no automatic deletion or confidence increase |
| `ouroboros/memory/records.py`, `evidence.py` | `agent_memory.records` | Provenance closure and least-permission semantics adapted to explicit typed records; host actors and transport inferences removed |
| `ouroboros/memory/graph.py` | `Store.link` + bounded traversal | Algorithmic design adapted; new revision-bound SQLite links replace process-global JSON snapshots |
| `ouroboros/memory/retrieval.py`, `sleep/scoring.py` | `agent_memory.index`, `retrieval` | FTS5/BM25 projection and decay design adapted; host paths, archive/person projections, traces and environment switches removed |
| `ouroboros/memory/context_envelope.py`, `context_selection.py` | `agent_memory.context` | Evidence-envelope design adapted to complete JSON blocks with a measured aggregate budget |
| `ouroboros/memory/sleep/atoms.py`, `transaction.py` | `agent_memory.store` | Persistence rewritten as revision-checked SQLite transactions; not a byte-compatible log/file transaction port |
| `ouroboros/memory/feedback.py` | `Store.feedback` | Revision-bound idempotency and bounded receipts adapted to atomic SQLite writes; no answer-wide correctness inference |
| Host-adapter contribution | `agent_memory.canonical` | Explicit host adapter, integrated with its synthetic tests |

The root MIT license was inspected and retained. Directly reused modules have no imports outside the Python standard library after the excerpt import relocation. The standalone package has no runtime dependency licenses to bundle. The upstream scoring file references another project's defaults; this repository does not copy that module or its prose and implements the mathematical ranking design separately.

## Intentional contract changes

- New IDs hash the full text with SHA-256 rather than a truncated normalized prefix. Existing IDs can be represented explicitly with `Record(id=...)`, but no lossless legacy importer is shipped.
- Record revisions are monotonically increasing integers, with exact source references. No host-specific actor, Telegram lane or private/public inference occurs.
- Audience permissions are explicit principal sets with intersection, rather than an agent-specific transport scope vocabulary.
- SQLite manages atomic current/history writes and concurrent writers. JSONL folding, POSIX lock files and graph snapshot saves are not copied into the public core.
- Consolidation only proposes a candidate. It never changes primary speech, automatically retires members, upgrades trust or raises confidence.
- Context budgeting covers the full serialized output. Unknown model cost stays unknown.
- Errors remain errors. Corrupt stores and unavailable FTS5 are not treated as empty memory.

## Components owned by the host agent

Personality and identity files, desires, consciousness, private dialogue lanes, Telegram ingestion, people pages, supervisor settings, task scheduling, operational logs, release activation and goal/budget machinery remain in the source application. Sleep phases that depend on those systems are not advertised as portable features. A network service, embedding backend and certified framework bindings are not bundled.

## Attribution

Source attribution and contributor credits are retained in [NOTICE](../NOTICE). The independent package was reviewed against its concrete source tree and tested with synthetic host integrations.

All examples and fixtures are synthetic. Source snapshots used for extraction were kept outside the public repository. The public Git history starts with this independent package.
