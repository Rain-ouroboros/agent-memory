"""Transactional SQLite storage with optimistic revisions and explicit scopes."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
import threading
import re
import hashlib
from typing import Literal

from .records import Record, _integer_bound, _finite_number
from ._sqlite import connect


class ConflictError(RuntimeError):
    """The expected revision no longer matches the persisted revision."""


class CheckpointConflictError(ConflictError):
    """Another call advanced this source checkpoint."""


class OperationConflictError(ConflictError):
    """An ingestion operation ID already binds different semantic input."""


@dataclass(frozen=True)
class IngestCheckpoint:
    namespace: str
    name: str
    cursor: int


@dataclass(frozen=True)
class IngestOperation:
    namespace: str
    operation_id: str
    fingerprint: str
    record_id: str
    outcome: Literal['inserted', 'duplicate', 'conflict']
    revision: int
    replayed: bool = False


def _validate_ingest_identity(value: str, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value) is None:
        raise ValueError(f'invalid {field}')


def _validate_ingest_namespace(namespace: str) -> None:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError('invalid namespace')


def _ingest_fingerprint(record: Record) -> str:
    """Hash semantic input under the versioned ingestion protocol."""
    payload = record.to_dict()
    del payload['revision']
    del payload['created_at']
    for field in ('confidence', 'occurred_at', 'expires_at'):
        value = payload[field]
        if value is not None:
            payload[field] = ([int(value), 1] if isinstance(value, int)
                              else list(float(value).as_integer_ratio()))
    encoded = json.dumps(
        {'schema': 'agent-memory-ingest-v1', 'record': payload},
        ensure_ascii=True, sort_keys=True, separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class FeedbackReceipt:
    record_id: str
    operation_id: str
    outcome: str
    revision: int
    duplicate: bool


class Store:
    """One connection per instance; SQLite coordinates independent writers."""
    def __init__(self, path: str | Path = ':memory:', *, timeout: float = 5):
        self._lock = threading.RLock()
        self._db = connect(path, timeout=timeout)
        try:
            self._db.execute('PRAGMA foreign_keys = ON')
            self._db.execute('BEGIN IMMEDIATE')
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError(f'unsupported database schema {version}')
            self._db.execute('CREATE TABLE IF NOT EXISTS records (namespace TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,id))')
            self._db.execute('CREATE TABLE IF NOT EXISTS history (namespace TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,id,revision))')
            self._db.execute('CREATE TABLE IF NOT EXISTS edges (namespace TEXT NOT NULL, source TEXT NOT NULL, relation TEXT NOT NULL, target TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL, weight REAL NOT NULL, PRIMARY KEY(namespace,source,relation,target,record_id), FOREIGN KEY(namespace,record_id) REFERENCES records(namespace,id))')
            self._db.execute('CREATE TABLE IF NOT EXISTS feedback (namespace TEXT NOT NULL, operation_id TEXT NOT NULL, record_id TEXT NOT NULL, command TEXT NOT NULL, outcome TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(namespace,operation_id))')
            self._db.execute('CREATE INDEX IF NOT EXISTS feedback_record_idx ON feedback(namespace,record_id)')
            self._db.execute("""CREATE TABLE IF NOT EXISTS ingest_checkpoints (
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                cursor INTEGER NOT NULL CHECK(typeof(cursor) = 'integer' AND cursor >= 0),
                PRIMARY KEY(namespace, name)
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS ingest_operations (
                namespace TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64),
                record_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('inserted', 'duplicate', 'conflict')),
                revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision >= 1),
                PRIMARY KEY(namespace, operation_id)
            )""")
            self._db.execute('PRAGMA user_version = 2')
            self._db.commit()
        except BaseException:
            try:
                self._db.rollback()
            finally:
                self._db.close()
            raise

    def get_ingest_checkpoint(self, name: str, *,
                              namespace: str = 'default') -> IngestCheckpoint:
        _validate_ingest_identity(name, 'checkpoint')
        _validate_ingest_namespace(namespace)
        with self._lock:
            row = self._db.execute(
                'SELECT cursor FROM ingest_checkpoints WHERE namespace=? AND name=?',
                (namespace, name),
            ).fetchone()
            return IngestCheckpoint(namespace, name, row[0] if row else 0)

    def get_ingest_operation(self, operation_id: str, *,
                             namespace: str = 'default') -> IngestOperation | None:
        _validate_ingest_identity(operation_id, 'operation_id')
        _validate_ingest_namespace(namespace)
        with self._lock:
            row = self._db.execute(
                'SELECT fingerprint,record_id,outcome,revision FROM ingest_operations '
                'WHERE namespace=? AND operation_id=?',
                (namespace, operation_id),
            ).fetchone()
            return IngestOperation(namespace, operation_id, *row) if row else None

    def _ingest_item(self, record: Record, *, operation_id: str,
                     checkpoint: IngestCheckpoint) -> IngestOperation:
        fingerprint = _ingest_fingerprint(record)
        namespace = checkpoint.namespace
        with self._lock:
            try:
                self._db.execute('BEGIN IMMEDIATE')
                row = self._db.execute(
                    'SELECT cursor FROM ingest_checkpoints WHERE namespace=? AND name=?',
                    (namespace, checkpoint.name),
                ).fetchone()
                cursor = row[0] if row else 0
                if cursor != checkpoint.cursor:
                    raise CheckpointConflictError('ingestion checkpoint changed')
                old = self._db.execute(
                    'SELECT fingerprint,record_id,outcome,revision FROM ingest_operations '
                    'WHERE namespace=? AND operation_id=?',
                    (namespace, operation_id),
                ).fetchone()
                if old is not None:
                    if old[0] != fingerprint:
                        raise OperationConflictError('operation_id already names different ingestion')
                    operation = IngestOperation(namespace, operation_id, *old, replayed=True)
                else:
                    current = self.get(record.id, namespace=namespace)
                    if current is None:
                        self._write(record, 1)
                        outcome, revision = 'inserted', 1
                    else:
                        identical = (replace(current, revision=0, created_at=0)
                                     == replace(record, revision=0, created_at=0))
                        outcome = 'duplicate' if identical else 'conflict'
                        revision = current.revision
                    operation = IngestOperation(
                        namespace, operation_id, fingerprint, record.id, outcome, revision,
                    )
                    self._db.execute(
                        'INSERT INTO ingest_operations VALUES (?,?,?,?,?,?)',
                        (namespace, operation_id, fingerprint, record.id, outcome, revision),
                    )
                self._db.execute(
                    'INSERT INTO ingest_checkpoints VALUES (?,?,?) '
                    'ON CONFLICT(namespace,name) DO UPDATE SET cursor=excluded.cursor',
                    (namespace, checkpoint.name, checkpoint.cursor + 1),
                )
                self._db.commit()
                return operation
            except BaseException:
                self._db.rollback()
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
        _integer_bound(expected, 'expected_revision', minimum=0)
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
        if revision is not None:
            _integer_bound(revision, 'revision', minimum=1)
        with self._lock:
            if revision is None:
                row = self._db.execute('SELECT payload FROM records WHERE namespace=? AND id=?', (namespace, record_id)).fetchone()
            else:
                row = self._db.execute('SELECT payload FROM history WHERE namespace=? AND id=? AND revision=?', (namespace, record_id, revision)).fetchone()
            return self._deserialize(record_id, row[0]) if row else None

    @staticmethod
    def _deserialize(record_id: str, payload: str) -> Record:
        try:
            return Record.from_dict(json.loads(payload))
        except (ValueError, TypeError, KeyError) as exc:
            # Never forward payload values, constructor keys or JSON excerpts.
            field = 'payload'
            if isinstance(exc, ValueError):
                message = str(exc)
                for name in ('confidence', 'created_at', 'occurred_at', 'expires_at',
                             'author', 'audience', 'sources'):
                    if message.startswith(name + ' '):
                        field = name
                        break
                if message in ('invalid trust', 'invalid status', 'invalid revision'):
                    field = message.removeprefix('invalid ')
            raise ValueError(f'record {record_id!r}: invalid field {field}') from None

    def snapshot(self, *, namespace: str = 'default') -> dict[str, Record]:
        with self._lock:
            rows = self._db.execute('SELECT id,payload FROM records WHERE namespace=? ORDER BY id', (namespace,)).fetchall()
        records = [self._deserialize(row[0], row[1]) for row in rows]
        return {r.id: r for r in records}

    def link(self, record: Record, subject: str, relation: str, target: str, *, weight: float = .5):
        _finite_number(weight, 'weight')
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
                terminal = outcome in {'incorrect', 'retract'}
                if not terminal:
                    count = self._db.execute("SELECT count(*) FROM feedback WHERE namespace=? AND record_id=? AND outcome IN ('helpful', 'irrelevant')", (namespace, record_id)).fetchone()[0]
                    if count >= 64:
                        raise ValueError('feedback receipt capacity reached')
                # Observations belong only in the ledger: evidence, history and
                # revision-bound descendants/edges must remain unchanged.
                saved = self._write(replace(record, status='retired'), record.revision + 1) if terminal else record
                self._db.execute('INSERT INTO feedback VALUES (?,?,?,?,?,?)', (namespace, operation_id, record_id, command, outcome, saved.revision))
                self._db.commit()
                return FeedbackReceipt(record_id, operation_id, outcome, saved.revision, False)
            except BaseException:
                self._db.rollback(); raise
