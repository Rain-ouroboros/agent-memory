"""Transactional SQLite storage with optimistic revisions and explicit scopes."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
import threading
import re

from .records import Record
from ._sqlite import connect


class ConflictError(RuntimeError):
    """The expected revision no longer matches the persisted revision."""


@dataclass(frozen=True)
class FeedbackReceipt:
    record_id: str
    operation_id: str
    outcome: str
    revision: int
    duplicate: bool


class Store:
    """One connection per instance; SQLite coordinates independent writers."""
    def __init__(self, path: str | Path = ':memory:'):
        self._lock = threading.RLock()
        self._db = connect(path)
        try:
            self._db.execute('PRAGMA foreign_keys = ON')
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f'unsupported database schema {version}')
            with self._db:
                self._db.execute('CREATE TABLE IF NOT EXISTS records (namespace TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,id))')
                self._db.execute('CREATE TABLE IF NOT EXISTS history (namespace TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,id,revision))')
                self._db.execute('CREATE TABLE IF NOT EXISTS edges (namespace TEXT NOT NULL, source TEXT NOT NULL, relation TEXT NOT NULL, target TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL, weight REAL NOT NULL, PRIMARY KEY(namespace,source,relation,target,record_id), FOREIGN KEY(namespace,record_id) REFERENCES records(namespace,id))')
                self._db.execute('CREATE TABLE IF NOT EXISTS feedback (namespace TEXT NOT NULL, operation_id TEXT NOT NULL, record_id TEXT NOT NULL, command TEXT NOT NULL, outcome TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(namespace,operation_id))')
                self._db.execute('PRAGMA user_version = 1')
        except BaseException:
            self._db.close()
            raise

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def put(self, record: Record, *, expected_revision: int | None = None) -> Record:
        expected = record.revision if expected_revision is None else expected_revision
        with self._lock:
            try:
                self._db.execute('BEGIN IMMEDIATE')
                row = self._db.execute('SELECT revision FROM records WHERE namespace=? AND id=?', (record.namespace, record.id)).fetchone()
                current = row[0] if row else 0
                if current != expected:
                    raise ConflictError(f'expected revision {expected}; found {current}')
                saved = self._write(record, current + 1)
                self._db.commit()
                return saved
            except BaseException:
                self._db.rollback(); raise

    def get(self, record_id: str, *, namespace: str = 'default', revision: int | None = None) -> Record | None:
        with self._lock:
            if revision is None:
                row = self._db.execute('SELECT payload FROM records WHERE namespace=? AND id=?', (namespace, record_id)).fetchone()
            else:
                row = self._db.execute('SELECT payload FROM history WHERE namespace=? AND id=? AND revision=?', (namespace, record_id, revision)).fetchone()
            return Record.from_dict(json.loads(row[0])) if row else None

    def snapshot(self, *, namespace: str = 'default') -> dict[str, Record]:
        with self._lock:
            rows = self._db.execute('SELECT payload FROM records WHERE namespace=? ORDER BY id', (namespace,)).fetchall()
        records = [Record.from_dict(json.loads(row[0])) for row in rows]
        return {r.id: r for r in records}

    def link(self, record: Record, subject: str, relation: str, target: str, *, weight: float = .5):
        if not subject.strip() or not relation.strip() or not target.strip() or not 0 < weight <= 1:
            raise ValueError('non-empty concepts and weight in (0, 1] are required')
        with self._lock:
            try:
                self._db.execute('BEGIN IMMEDIATE')
                row = self._db.execute('SELECT revision FROM records WHERE namespace=? AND id=?', (record.namespace, record.id)).fetchone()
                if row is None or row[0] != record.revision:
                    raise ConflictError('link requires the current persisted record')
                self._db.execute('INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?,?)',
                                 (record.namespace, subject.casefold(), relation, target.casefold(), record.id, record.revision, weight))
                self._db.commit()
            except BaseException:
                self._db.rollback(); raise

    def edges(self, *, namespace: str = 'default') -> list[tuple]:
        with self._lock:
            return self._db.execute('SELECT source,relation,target,record_id,revision,weight FROM edges WHERE namespace=? ORDER BY source,relation,target,record_id', (namespace,)).fetchall()

    def _write(self, record: Record, revision: int) -> Record:
        saved = replace(record, revision=revision)
        payload = json.dumps(saved.to_dict(), ensure_ascii=False, allow_nan=False)
        self._db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(namespace,id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload', (saved.namespace, saved.id, saved.revision, payload))
        self._db.execute('INSERT INTO history VALUES (?,?,?,?)', (saved.namespace, saved.id, saved.revision, payload))
        return saved

    def feedback(self, record_id: str, *, expected_revision: int, operation_id: str,
                 outcome: str, namespace: str = 'default') -> FeedbackReceipt:
        """Revision-bound, durable idempotent feedback; a privileged host write.

        Helpful/irrelevant are observations, not confidence upgrades. Incorrect
        and retract retire the target. Receipts remain valid across restarts.
        """
        if outcome not in {'helpful', 'irrelevant', 'incorrect', 'retract'}:
            raise ValueError('invalid feedback outcome')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', operation_id) or type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('invalid feedback identity or revision')
        command = json.dumps([record_id, expected_revision, outcome])
        with self._lock:
            try:
                self._db.execute('BEGIN IMMEDIATE')
                old = self._db.execute('SELECT command,revision FROM feedback WHERE namespace=? AND operation_id=?', (namespace, operation_id)).fetchone()
                if old:
                    if old[0] != command:
                        raise ConflictError('operation ID already names different feedback')
                    self._db.commit()
                    return FeedbackReceipt(record_id, operation_id, outcome, old[1], True)
                record = self.get(record_id, namespace=namespace)
                if record is None or record.revision != expected_revision:
                    raise ConflictError('feedback target changed or is missing')
                if record.status != 'active':
                    raise ValueError('feedback requires an active record')
                count = self._db.execute('SELECT count(*) FROM feedback WHERE namespace=? AND record_id=?', (namespace, record_id)).fetchone()[0]
                if count >= 64:
                    raise ValueError('feedback receipt capacity reached')
                changed = replace(record, status='retired') if outcome in {'incorrect', 'retract'} else record
                saved = self._write(changed, record.revision + 1)
                self._db.execute('INSERT INTO feedback VALUES (?,?,?,?,?,?)', (namespace, operation_id, record_id, command, outcome, saved.revision))
                self._db.commit()
                return FeedbackReceipt(record_id, operation_id, outcome, saved.revision, False)
            except BaseException:
                self._db.rollback(); raise
