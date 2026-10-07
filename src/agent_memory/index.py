"""Optional rebuildable FTS5/BM25 projection of canonical records.

The index is never the authority for text, revisions, access or provenance.
Errors propagate; an unavailable index is not evidence of absent memories.
"""
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
from .store import Store
from ._sqlite import connect
from .terms import content_terms


@dataclass(frozen=True)
class IndexReceipt:
    namespace: str
    records: int
    revisions: tuple[tuple[str, int], ...]


class SearchIndex:
    def __init__(self, path: str | Path = ':memory:', *, timeout: float = 5):
        self._lock = threading.RLock()
        self._db = connect(path, timeout=timeout)
        try:
            with self._db:
                self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(text, id UNINDEXED, namespace UNINDEXED, revision UNINDEXED, tokenize='unicode61 remove_diacritics 2')")
        except BaseException:
            self._db.close(); raise

    def close(self):
        with self._lock: self._db.close()

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

    def rebuild(self, store: Store, *, namespace: str = 'default') -> IndexReceipt:
        records = store.snapshot(namespace=namespace)
        with self._lock, self._db:
            self._db.execute('DELETE FROM memory_fts WHERE namespace=?', (namespace,))
            self._db.executemany('INSERT INTO memory_fts(text,id,namespace,revision) VALUES (?,?,?,?)',
                                 [(r.text, r.id, r.namespace, r.revision) for r in records.values()
                                  if r.status == 'active' and r.text.strip()])
        return IndexReceipt(namespace, len(records), tuple(sorted((r.id, r.revision) for r in records.values())))

    def candidates(self, query: str, *, namespace: str = 'default', limit: int = 100) -> tuple[tuple[str, int, float], ...]:
        if not 0 <= limit <= 10000: raise ValueError('limit must be within [0, 10000]')
        terms = content_terms(query[:1024])[:16]
        if not terms: return ()
        # Quote every literal; user input never becomes FTS query syntax.
        expression = ' OR '.join('"'+term.replace('"', '""')+'"' for term in terms)
        with self._lock:
            rows = self._db.execute('SELECT id,revision,bm25(memory_fts) FROM memory_fts WHERE memory_fts MATCH ? AND namespace=? ORDER BY bm25(memory_fts),id LIMIT ?',
                                    (expression, namespace, limit)).fetchall()
        return tuple((key, int(revision), float(score)) for key, revision, score in rows)
