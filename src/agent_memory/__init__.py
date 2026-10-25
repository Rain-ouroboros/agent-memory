"""Witness Memory: agent memory with sources, revisions and boundaries."""
from .adapters import IngestReceipt, Model, ModelOutput, Source, ingest, ingest_batch, summarize
from .context import Context, context
from .lifecycle import Maintenance, maintain
from .records import Evidence, Record, SourceRef, derive, resolve_evidence
from .retrieval import Hit, Recall, recall
from .store import ConflictError, FeedbackReceipt, Store

__all__ = ['ConflictError', 'FeedbackReceipt', 'Context', 'Evidence', 'Hit', 'Maintenance', 'Model',
           'ModelOutput', 'Recall', 'Record', 'Source', 'SourceRef', 'Store',
           'context', 'derive', 'ingest', 'ingest_batch', 'IngestReceipt',
           'maintain', 'recall', 'resolve_evidence', 'summarize']
from .adapters import CheckpointSource, IngestItem
from .store import (CheckpointConflictError, OperationConflictError,
                    IngestCheckpoint, IngestOperation)
__all__ += ['CheckpointSource', 'IngestItem', 'CheckpointConflictError',
            'OperationConflictError', 'IngestCheckpoint', 'IngestOperation']
__version__ = '0.2.2'

from .canonical import CanonicalReadAdapter, ReadResult, SnapshotError
__all__ += ["CanonicalReadAdapter", "ReadResult", "SnapshotError"]
from .consolidation import ConsolidationReceipt, SkippedProposal, propose_consolidation
__all__ += ['ConsolidationReceipt', 'SkippedProposal']
from .extraction import classify_kind
from .index import IndexReceipt, SearchIndex
from .redaction import redact_secret_like
__all__ += ['IndexReceipt', 'SearchIndex', 'classify_kind', 'propose_consolidation', 'redact_secret_like']
