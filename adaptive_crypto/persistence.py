"""Synchronous JSON compatibility and incremental SQLite WAL persistence."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading

from .core import DataError
from .state_codec import decode, dumps, encode
from .state_lock import SingleInstance

APPLICATION_ID = 0x50594143  # PYAC
SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE namespaces (namespace TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>=0), root_json TEXT NOT NULL, context_json TEXT NOT NULL)",
    "CREATE TABLE records (namespace TEXT NOT NULL REFERENCES namespaces(namespace) ON DELETE CASCADE, collection TEXT NOT NULL, owner TEXT NOT NULL, record_key TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0), payload_json TEXT NOT NULL, PRIMARY KEY(namespace,collection,owner,record_key))",
    "CREATE TABLE imports (namespace TEXT PRIMARY KEY REFERENCES namespaces(namespace), source_path TEXT, source_sha256 TEXT, imported_ms INTEGER NOT NULL, manifest_json TEXT NOT NULL)",
)


def wal_version_supported(version):
    return version >= (3, 51, 3) or (version[:2] == (3, 50) and version[2] >= 7) or (version[:2] == (3, 44) and version[2] >= 6)


def check_sqlite_runtime():
    if not wal_version_supported(sqlite3.sqlite_version_info):
        raise DataError(f"SQLite {sqlite3.sqlite_version} lacks the WAL-reset fix. Use Python linked to SQLite 3.51.3+ (or 3.50.7/3.44.6 backport). JSON mode remains available for unmigrated state.")
    if sqlite3.threadsafety == 0:
        raise DataError("SQLite was built without thread safety")


def connect(path, *, readonly=False):
    options = {"isolation_level": None, "check_same_thread": False, "timeout": 5.0}
    if hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"):
        options["autocommit"] = sqlite3.LEGACY_TRANSACTION_CONTROL
    if readonly:
        return sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, **options)
    return sqlite3.connect(path, **options)


class SQLiteDatabase:
    """One owned connection. All operations, including close, share this lock."""
    def __init__(self, path, *, create=False, owned=False):
        check_sqlite_runtime()
        self.path = Path(path).resolve()
        if str(self.path).startswith(("\\\\", "//")):
            raise DataError("SQLite WAL requires a local disk, not a UNC path")
        self.lock = threading.RLock()
        self._ownership = None
        self._connection = None
        self._cache = {}
        self._bound = set()
        self._mutating = False
        self._poisoned = False
        self._closed = False
        self.last_changed_rows = 0
        try:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            if not owned:
                self._ownership = SingleInstance(str(self.path)+".lock")
            exists = self.path.exists()
            if create == exists:
                raise DataError("Database already exists" if exists else "Database does not exist; import or initialize it explicitly")
            self._connection = connect(self.path)
            if exists:
                self._check_identity()
            if self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise DataError("Could not enable SQLite WAL")
            for pragma in ("synchronous=FULL", "foreign_keys=ON", "busy_timeout=5000", "wal_autocheckpoint=1000"):
                self._connection.execute("PRAGMA "+pragma)
            if self._connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise DataError("SQLite FULL durability unavailable")
            if not exists:
                with self._sql_transaction():
                    for statement in SCHEMA:
                        self._connection.execute(statement)
                    self._connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            self.close()
            raise

    def _check_identity(self):
        if (self._connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or self._connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
            raise DataError("Unknown database application or schema version; state was not reset")
        # Fail before runtime normalization if the storage schema was damaged.
        for table, columns in (("namespaces", ["namespace", "revision", "root_json", "context_json"]),
                               ("records", ["namespace", "collection", "owner", "record_key", "ordinal", "payload_json"]),
                               ("imports", ["namespace", "source_path", "source_sha256", "imported_ms", "manifest_json"])):
            if [row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")] != columns:
                raise DataError("Malformed database schema")

    def _ready(self):
        if self._closed or self._poisoned:
            raise DataError("Persistence is closed or its commit outcome is unknown; reopen and reconcile state")

    def _commit(self):
        self._connection.execute("COMMIT")

    @contextmanager
    def _sql_transaction(self):
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._commit()
        except BaseException:
            if self._connection.in_transaction:
                try:
                    self._connection.execute("ROLLBACK")
                except BaseException:
                    self._poisoned = True
            else:
                # COMMIT might have succeeded before an interrupt/error reached Python.
                self._poisoned = True
            raise

    def _read(self, namespace):
        if namespace not in self._cache:
            row = self._connection.execute("SELECT revision,root_json,context_json FROM namespaces WHERE namespace=?", (namespace,)).fetchone()
            if row is None:
                return None
            records = {(c, o, k): (i, p) for c, o, k, i, p in self._connection.execute(
                "SELECT collection,owner,record_key,ordinal,payload_json FROM records WHERE namespace=?", (namespace,))}
            self._cache[namespace] = (decode(row[1], records), row[0], row[1], records, row[2])
        return self._cache[namespace]

    def namespace(self, namespace):
        with self.lock:
            self._ready()
            if namespace not in {"legacy", "smc", "neural", "positions"} or namespace in self._bound:
                raise DataError("Unknown or already bound state namespace")
            self._bound.add(namespace)
            return SQLiteDocument(self, namespace)

    def _write_rows(self, namespace, old, new):
        changed = 0
        for key in old.keys()-new.keys():
            self._connection.execute("DELETE FROM records WHERE namespace=? AND collection=? AND owner=? AND record_key=?", (namespace, *key))
            changed += 1
        for key, value in new.items():
            if old.get(key) != value:
                self._connection.execute("INSERT INTO records VALUES (?,?,?,?,?,?) ON CONFLICT(namespace,collection,owner,record_key) DO UPDATE SET ordinal=excluded.ordinal,payload_json=excluded.payload_json", (namespace, *key, *value))
                changed += 1
        return changed

    def save(self, namespace, document, context=None):
        with self.lock:
            self._ready()
            old = self._read(namespace)
            encoded_context = dumps(context) if context is not None else (old[4] if old else "{}")
            if old is not None and old[0] == document and old[4] == encoded_context:
                self.last_changed_rows = 0
                return
            next_data = copy.deepcopy(document)
            root, rows = encode(next_data, previous=old[0] if old else None, previous_rows=old[3] if old else None)
            revision = old[1]+1 if old else 0
            with self._sql_transaction():
                actual = self._connection.execute("SELECT revision FROM namespaces WHERE namespace=?", (namespace,)).fetchone()
                if actual != ((old[1],) if old else None):
                    self._poisoned = True
                    raise DataError("State revision changed outside the owner; refusing a stale overwrite")
                if old:
                    if old[2] == root and old[4] == encoded_context:
                        self._connection.execute("UPDATE namespaces SET revision=? WHERE namespace=?", (revision, namespace))
                    else:
                        self._connection.execute("UPDATE namespaces SET revision=?,root_json=?,context_json=? WHERE namespace=?", (revision, root, encoded_context, namespace))
                else:
                    self._connection.execute("INSERT INTO namespaces VALUES (?,?,?,?)", (namespace, revision, root, encoded_context))
                changed = self._write_rows(namespace, old[3] if old else {}, rows)
            self._cache[namespace] = (next_data, revision, root, rows, encoded_context)
            self.last_changed_rows = changed+1

    def replace_documents(self, documents, contexts):
        """Commit coordinated maintenance across namespaces as one transaction."""
        with self.lock:
            self._ready()
            if self._mutating:
                raise DataError("Maintenance cannot run inside a state transaction")
            from .state_paths import validate_documents
            validate_documents(documents, contexts)
            prepared = {}
            for name, document in documents.items():
                old = self._read(name)
                if old is None:
                    raise DataError(f"Missing database section: {name}")
                current = copy.deepcopy(document)
                root, rows = encode(current, previous=old[0], previous_rows=old[3])
                prepared[name] = (current, old[1]+1, root, rows, dumps(contexts[name]))
            changed = 0
            with self._sql_transaction():
                for name, new in prepared.items():
                    old = self._read(name)
                    actual = self._connection.execute("SELECT revision FROM namespaces WHERE namespace=?", (name,)).fetchone()
                    if actual != (old[1],):
                        self._poisoned = True
                        raise DataError("State revision changed outside the owner; refusing a stale overwrite")
                    self._connection.execute("UPDATE namespaces SET revision=?,root_json=?,context_json=? WHERE namespace=?",
                                             (new[1], new[2], new[4], name))
                    changed += self._write_rows(name, old[3], new[3])+1
            self._cache.update(prepared)
            self.last_changed_rows = changed

    def import_documents(self, documents, provenance):
        """Insert only absent namespaces, all in one transaction, with provenance."""
        with self.lock:
            self._ready()
            prepared = {name: (copy.deepcopy(doc), *encode(doc)) for name, doc in documents.items()}
            with self._sql_transaction():
                for name, (doc, root, rows) in prepared.items():
                    if self._connection.execute("SELECT 1 FROM namespaces WHERE namespace=?", (name,)).fetchone():
                        raise DataError(f"Namespace already exists: {name}")
                    self._connection.execute("INSERT INTO namespaces VALUES (?,0,?,?)", (name, root, dumps(provenance[name]["context"])))
                    self._write_rows(name, {}, rows)
                    info = provenance[name]
                    self._connection.execute("INSERT INTO imports VALUES (?,?,?,?,?)", (name, info.get("source_path"), info.get("source_sha256"), info["imported_ms"], dumps(info)))
            for name, (doc, root, rows) in prepared.items():
                self._cache[name] = (doc, 0, root, rows, dumps(provenance[name]["context"]))

    def documents(self):
        with self.lock:
            self._ready()
            return {name: copy.deepcopy(self._read(name)[0]) for name, in self._connection.execute("SELECT namespace FROM namespaces ORDER BY namespace").fetchall()}

    def information(self):
        with self.lock:
            self._ready()
            integrity = [r[0] for r in self._connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"] or self._connection.execute("PRAGMA foreign_key_check").fetchall():
                raise DataError("Database integrity check failed")
            from .state_codec import loads
            return {"application_id": APPLICATION_ID, "schema_version": SCHEMA_VERSION, "integrity": "ok",
                    "revisions": dict(self._connection.execute("SELECT namespace,revision FROM namespaces")),
                    "contexts": {name: loads(value) for name, value in self._connection.execute("SELECT namespace,context_json FROM namespaces")},
                    "imports": {name: loads(value) for name, value in self._connection.execute("SELECT namespace,manifest_json FROM imports")}}

    def checkpoint(self):
        with self.lock:
            self._ready()
            result = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result[0] != 0 or result[1] != result[2]:
                raise DataError("Database checkpoint is busy")

    def backup(self, destination):
        with self.lock:
            self._ready()
            destination = Path(destination).resolve()
            # Exclusive creation prevents overwriting an unrelated file.
            with destination.open("xb"):
                pass
            target = connect(destination)
            try:
                self._connection.backup(target)
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise DataError("Backup integrity check failed")
            finally:
                target.close()

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._connection is not None:
                    self._connection.close()
            finally:
                if self._ownership is not None:
                    self._ownership.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SQLiteDocument:
    def __init__(self, database, namespace):
        self.database, self.namespace = database, namespace
        self.lock = database.lock
        self.database_path = database.path
        self.context = None

    def source(self):
        with self.lock:
            self.database._ready()
            value = self.database._read(self.namespace)
            if value:
                from .state_paths import validate_documents
                from .state_codec import loads
                validate_documents({self.namespace: value[0]}, {self.namespace: loads(value[4])})
            return dumps(value[0]).encode() if value else None

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self._current())

    def _current(self):
        self.database._ready()
        value = self.database._read(self.namespace)
        if value is None:
            raise DataError("State namespace is not initialized")
        return value[0]

    def save(self, document):
        self.database.save(self.namespace, document, self.context)


class JsonDocument:
    database_path = None

    def __init__(self, path, writer):
        self.path, self.writer = path, writer
        self.lock = threading.RLock()
        self.data = None
        self._mutating = False

    def source(self):
        return self.path.read_bytes() if self.path.exists() else None

    def snapshot(self):
        return copy.deepcopy(self.data)

    def _current(self):
        return self.data

    def save(self, document):
        next_data = copy.deepcopy(document)
        self.writer(self.path, next_data)
        self.data = next_data


class DocumentStore:
    """Domain facade with common copy/validate/commit semantics."""
    def _bind(self, path, backend, validator, writer):
        self.path = Path(path)
        self._backend = backend if backend is not None else JsonDocument(self.path, writer)
        self.lock = self._backend.lock
        self.database_path = self._backend.database_path
        self._validator = validator

    def _initialize(self, prepare, *, eager=False):
        with self.lock:
            source = self._backend.source()
            document, backups = prepare(source)
            self._validator(document)
            if backups:
                from .backups import backup_store
                backup_store(self, source=source, action="startup-normalization")
            if isinstance(self._backend, JsonDocument):
                from .state_codec import loads
                if eager or backups or (source is not None and loads(source.decode("utf-8-sig")) != document):
                    self._backend.save(document)
                else:
                    self._backend.data = copy.deepcopy(document)
            else:
                self._backend.save(document)

    @property
    def data(self):
        # Historical JSON test/caller compatibility; SQLite never exposes its cache.
        return self._backend.data if isinstance(self._backend, JsonDocument) else self.snapshot()

    @data.setter
    def data(self, value):
        if not isinstance(self._backend, JsonDocument):
            raise DataError("Use a transaction to modify SQLite state")
        self._backend.data = value

    def snapshot(self):
        with self.lock:
            return self._backend.snapshot()

    def transaction(self, operation):
        with self.lock:
            owner = self._backend.database if isinstance(self._backend, SQLiteDocument) else self._backend
            if owner._mutating:
                raise DataError("Recursive state transactions are not supported")
            owner._mutating = True
            try:
                current = self._backend._current()
                proposed = copy.deepcopy(current)
                result = operation(proposed)
                self._validator(proposed)
                result = copy.deepcopy(result)
                if proposed != current:
                    self._backend.save(proposed)
                elif isinstance(self._backend, SQLiteDocument):
                    owner.last_changed_rows = 0
                return result
            finally:
                owner._mutating = False
