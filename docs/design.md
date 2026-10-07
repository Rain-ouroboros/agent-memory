# Design contract

Witness Memory is an in-process library. It owns local record persistence and evidence selection; the host owns authentication, tool permissions, model consent, retention and delivery.

## Records and revisions

A `Record` separates text, source, explicit author, source trust, audience, namespace, kind, confidence, capture time (`created_at`), optional event time (`occurred_at`), expiry and lifecycle. Unknown author/event time remain `None`. `created_at` is observed capture time, not inferred event time. Timestamps are epoch seconds. Trust defaults to `unknown`, audience to empty/private, and status to active.

`Record.create` hashes a JSON tuple of namespace, source, anchor and the **entire** text with SHA-256. Identical input produces an identical ID. The namespace and ID are the persistence key. Updating text through `dataclasses.replace` retains identity and creates another revision; creating a fresh record from new text creates a different identity.

`Store.put` compares the expected revision inside `BEGIN IMMEDIATE`. New records expect zero. The default expected revision is `record.revision`. A mismatch raises `ConflictError`; it is never silently merged. Each successful write updates the current row and appends its immutable historical revision in the same SQLite transaction. Independent connections use SQLite's locking with a five-second busy timeout. Operational and corruption errors propagate.

The database schema version is 1. Unknown schema versions fail explicitly. There is no automatic schema migration or Rain backup importer. An ingestion batch and a maintenance run consist of individual atomic record writes; failures can leave earlier successful writes in place.

## Evidence and permissions

`derive` requires persisted parents in one namespace. It creates an unpersisted record, retaining exact parent revisions and intersecting audience permissions. Trust follows the least trusted source: `untrusted` dominates `unknown`, which dominates `trusted`. Confidence never exceeds the least confident parent.

Before recall, `resolve_evidence` walks the complete provenance closure. It detects missing sources, changed revisions, namespace crossings, inactive/expired or incomplete sources, cycles and traversal limits. A derived record with no sources is incomplete. Incomplete evidence is withheld, with no permissive audience fallback. Every ancestor participates in permission intersection; a summary cannot grant access unavailable at its sources.

The intersection has two special cases: `('*',)` is unrestricted, and `()` is private/denied. Concrete audience identifiers are supplied by trusted host code; using the literal `'*'` as a principal is rejected. Mixed wildcard audiences are rejected. An empty audience does not mean “only this agent”: specify that agent's principal explicitly.

Storage and low-level index methods are privileged and do not authorize reads. Retrieval diagnostics include aggregate counts of withheld records in the selected namespace, so do not expose them as a cross-tenant endpoint. Authorized revision receipts omit unauthorized record IDs. Treat every namespace and principal argument as authenticated host context, never as authority claimed inside source text.

A recall is a snapshot, not a delivery authorization lease. Concurrent changes after selection require host revalidation before delivery. Graph and record reads are separate snapshots; revision mismatch withholds edges conservatively. The library does not promise serializable read-plus-delivery across external authorization systems.

## Retrieval

Base recall uses the lexical term matcher and query-centered excerpt logic extracted from Rain. Stop words and a small transliteration table cover Russian and English. Russian inflection uses a prefix heuristic; Latin identifiers remain literal. Dotted versions are kept whole. One or two query terms require all matches; longer queries require at least half, never fewer than two. All-stopword queries return no hits.

Ranking combines lexical overlap, exponential confidence decay and hyperbolic recency. Decay periods are **e-folding time constants**, not half-lives. Confidence is a heuristic ranking value, not a calibrated probability of truth. The current implementation scans the selected namespace in memory; it is not designed or benchmarked for very large stores.

`SearchIndex` is an optional, disposable FTS5 projection with BM25 ordering. It uses Unicode tokenization and quoted literal OR terms. `rebuild` replaces one namespace's projection atomically. Index hits are intersected with authorized canonical records and exact revisions before inclusion. FTS contributes a small reciprocal-rank increment (`1 / (60 + rank)`) alongside lexical candidates. A stale index may miss FTS-only candidates; lexical matching still scans current canonical rows. FTS errors propagate. No vector retrieval or semantic similarity is implemented.

Graph links bind a subject/relation/target to a current record revision. Recall traverses eligible links in both directions, with configurable depth (0–8), per-node fanout and visited-node limits. Path weights decay by 0.6 per depth. A graph relationship is an explicit host assertion backed by a record, not an independently verified logical implication. Updates leave old edges physically present but exclude them until relinked at the new revision.

## Context and models

`context` renders whole JSON evidence blocks with IDs, revisions, sources, authors, trust and primary witness IDs. The default counter is Unicode character length. A caller can supply a tokenizer; its counter must return a non-negative integer. The complete serialized envelope is measured after each candidate block. Oversized blocks are omitted, not clipped. If even the empty envelope does not fit, the result is empty with all hits omitted.

The result reports included/omitted IDs and budget usage. The caller reserves space for the remaining prompt and interprets these receipts. Excerpts may omit context; recovered passages contain offsets and an omission marker. Metadata is not regex-redacted automatically. JSON escaping protects structure, not the model's behavior.

The `Source` and `Model` protocols impose no provider dependencies. `ingest` explicitly consumes source records. `summarize` checks that its input records are current, invokes the supplied model once, then returns an unpersisted source-bound candidate and a usage object. Missing token counts or cost remain `None`. The host must authorize inputs, cap spending, handle provider errors and decide whether to persist a candidate. A race or source change during the model call makes the resulting candidate ineligible on later provenance validation.

## Maintenance

`maintain` inspects expired active records; dry run is the default. Applying the result retires each candidate through revision-checked writes. A conflict raises an error. Retirement and history retention are not physical erasure.

`propose_consolidation` adapts Rain's deterministic same-kind greedy Jaccard clustering (threshold 0.7). It considers complete, trusted, derived records, partitions them by effective audience, and returns unpersisted source-bound proposals. Primary speech and already consolidated records are excluded. At most 500 eligible candidates are processed by default; exceeding the cap raises rather than silently truncating. The algorithm can mistake negated sentences for duplicates. Host review is required; no source is automatically removed or retired, and confidence is not increased.

`classify_kind` is a small rule-based classifier, not a semantic extractor. `redact_secret_like` is an optional regex helper for known secret shapes, not a privacy guarantee. Apply a host-specific capture policy if raw secrets must never be stored; applying this helper after persistence does not remove historical data.

## Feedback

`Store.feedback` adapts Rain's revision-bound feedback design. Each command contains a record ID, expected integer revision, namespace, operation ID and outcome. The receipt and record/history revision are committed in the same SQLite transaction. Replaying the same command returns its original receipt with `duplicate=True`, including after restart or retirement; reusing its operation ID for a different command raises `ConflictError`.

Outcomes are `helpful`, `irrelevant`, `incorrect` and `retract`. The first two only record an observation, without increasing confidence or inventing usage. The latter two retire the record. Any new feedback revision conservatively invalidates derived records bound to the older revision. There is a 64-receipt per-record cap; additional commands fail instead of discarding old idempotency keys. Feedback is a privileged host write and does not turn an answer-level outcome into a verdict about every retrieved memory. Correction uses a normal revision-checked `Store.put`; automatic supersession is not implemented.

New database files are created with owner-only permissions on POSIX systems. Existing file permissions are not changed. Filesystem policy, backups and encryption remain host responsibilities.

## Existing stores

Rain's `CanonicalReadAdapter` works with host callbacks for latest canonical rows, metadata projection, revision identity, eligibility and exact-revision authorization. It materializes the snapshot to detect duplicate IDs before returning data. Unknown scopes deny. Callback errors propagate. Returned values are deep copies. Callbacks are trusted integration code, not instructions supplied by memory text.

This adapter does not fold an append log, authenticate the supplied audience, add missing provenance or lock an external store. Its input must be an already folded, consistent latest-revision snapshot. It is a useful seam for an existing store; it is not a storage migration by itself.

## Supported surface

Python 3.11+ and standard-library SQLite. Optional indexing requires FTS5 in the Python SQLite build. The core has no framework, model, network or environment-variable dependency and performs no work merely by import. Windows, macOS and Linux are CI targets. Coding and research integrations use synthetic sources; real framework certification, scale benchmarks, encryption, embedding providers, physical erasure and a lossless legacy migration are outside 0.1.0.
