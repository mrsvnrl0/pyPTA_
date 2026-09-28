"""One complete, atomically replaced backup for a local study directory."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from zipfile import ZIP_DEFLATED, ZipFile

from .core import DataError
from .state_lock import SingleInstance

_scope = ContextVar("backup_scope", default=None)
_lock = threading.RLock()


@contextmanager
def backup_scope(base, settings=None):
    token = _scope.set((Path(base).resolve(), Path(settings).resolve() if settings else None))
    try:
        yield
    finally:
        _scope.reset(token)


def state_base(path):
    path = Path(path).resolve()
    for suffix in (".smc.json", ".neural.json", ".positions.json"):
        if path.name.endswith(suffix):
            return path.with_name(path.name[:-len(suffix)]+".json")
    return path


def backup_path(base):
    return Path(base).resolve().parent/"backup"/"latest.zip"


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def create_backup(base, *, settings=None, database=None, sources=None, documents=None,
                  contexts=None, action="backup", import_provenance=None):
    """Capture saved settings and every study namespace before an operation.

    Callers hold state ownership and any live store locks. SQLite is copied with
    its online backup API under the coordinator lock, including committed WAL.
    Explicit sources preserve original import/upgrade bytes, even when damaged.
    Only latest.zip is replaced; unrelated or historical files are never deleted.
    """
    from .state_paths import StatePaths
    base = Path(base).resolve()
    paths = StatePaths(base)
    destination = backup_path(base)
    directory = destination.parent
    settings = Path(settings).resolve() if settings else base.parent/"adaptive_crypto_settings.json"
    # Lock order matches stores: database/store lock before the backup lock.
    with database.lock if database is not None else nullcontext():
        with _lock:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or directory.resolve().parent != base.parent:
                raise DataError("Backup folder must be directly inside the study root")
            try:
                owner = SingleInstance(base.parent/".pypta-backup.lock")
            except SystemExit as exc:
                raise DataError("Another process is creating the backup. Try again when it finishes.") from exc
            with owner:
                # A separate owned staging directory keeps a failed write away
                # from the previous backup and cleans only our temporary files.
                with tempfile.TemporaryDirectory(prefix=".pending-", dir=directory) as temporary:
                    staging = Path(temporary).resolve()
                    if staging.parent != directory.resolve():
                        raise DataError("Invalid backup staging directory")
                    entries = {}
                    if settings.is_file():
                        entries["settings/settings.json"] = settings.read_bytes()
                    if database is not None:
                        copied = staging/paths.database.name
                        database.backup(copied)
                        entries["state/"+paths.database.name] = copied.read_bytes()
                        documents = database.documents()
                        contexts = database.information()["contexts"]
                    else:
                        for path in paths.sources.values():
                            if path.is_file():
                                entries["state/"+path.name] = path.read_bytes()
                    for path, raw in (sources or {}).items():
                        entries["state/"+Path(path).name] = raw
                    if documents is not None:
                        entries["metadata/documents.json"] = _json({"documents": documents, "contexts": contexts or {}})
                    for name, context in (contexts or {}).items():
                        if "rules" in context and "assets" in context:
                            entries["settings/"+name+".settings.json"] = _json({
                                "assets": [{"name": asset, **cfg} for asset, cfg in context["assets"].items()],
                                "strategy": context["rules"]})
                    manifest = {"version": 1, "created_ms": int(time.time()*1000), "action": action,
                                "state_base": base.name, "settings_source": str(settings),
                                "settings_present": "settings/settings.json" in entries,
                                "backend": "sqlite" if database is not None else "json",
                                "files": {name: {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                                          for name, raw in entries.items()}}
                    if import_provenance is not None:
                        manifest["proposed_imports"] = import_provenance
                    entries["manifest.json"] = _json(manifest)
                    archive = staging/"latest.zip"
                    with archive.open("xb") as stream:
                        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as zipped:
                            for name, raw in entries.items():
                                zipped.writestr(name, raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    with ZipFile(archive) as zipped:
                        if zipped.testzip() is not None or set(zipped.namelist()) != set(entries):
                            raise DataError("Backup archive verification failed")
                        for name, raw in entries.items():
                            if zipped.read(name) != raw:
                                raise DataError("Backup contents differ from the captured state")
                    os.replace(archive, destination)
    return destination


def backup_store(store, *, source=None, action="startup"):
    scope = _scope.get()
    base, settings = scope if scope else (state_base(store.path), None)
    database = getattr(store._backend, "database", None)
    sources = {store.path: source} if source is not None else None
    return create_backup(base, settings=settings, database=database, sources=sources, action=action)


def backup_runtime(runtime, *, action="backup", documents=None, contexts=None):
    """Capture both JSON stores together; include inactive sibling ledgers too."""
    with runtime.store.lock, runtime.positions.lock:
        base = Path(getattr(runtime, "state_base", state_base(runtime.store.path))).resolve()
        if documents is None:
            name = runtime.rules.paper_namespace
            documents = {name: runtime.store.snapshot(), "positions": runtime.positions.snapshot()}
            contexts = {name: {"assets": runtime.assets, "rules": asdict(runtime.rules)},
                        "positions": {"market_mode": runtime.rules.market_mode}}
        return create_backup(base, settings=runtime.settings_path,
                             database=getattr(runtime.store._backend, "database", None),
                             documents=documents, contexts=contexts, action=action)
