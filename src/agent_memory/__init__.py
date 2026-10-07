"""Witness Memory: agent memory with sources, revisions and boundaries."""
from .adapters import Model, ModelOutput, Source, ingest, summarize
from .context import Context, context
from .lifecycle import Maintenance, maintain
from .records import Evidence, Record, SourceRef, derive, resolve_evidence
from .retrieval import Hit, Recall, recall
from .store import ConflictError, FeedbackReceipt, Store

__all__ = ['ConflictError', 'FeedbackReceipt', 'Context', 'Evidence', 'Hit', 'Maintenance', 'Model',
           'ModelOutput', 'Recall', 'Record', 'Source', 'SourceRef', 'Store',
           'context', 'derive', 'ingest', 'maintain', 'recall', 'resolve_evidence', 'summarize']
__version__ = '0.1.1'

from .canonical import CanonicalReadAdapter, ReadResult, SnapshotError
__all__ += ["CanonicalReadAdapter", "ReadResult", "SnapshotError"]
from .consolidation import propose_consolidation
from .extraction import classify_kind
from .index import IndexReceipt, SearchIndex
from .redaction import redact_secret_like
__all__ += ['IndexReceipt', 'SearchIndex', 'classify_kind', 'propose_consolidation', 'redact_secret_like']
