"""Previewed whole-study SQLite restore with a durable pre-change rollback journal.

SQLite's backup API replaces the database in one transaction. Settings live in a
separate file, so a flushed journal survives until both publications complete.
Startup must call recover_pending_restore before reading settings or opening stores.
"""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
from zipfile import BadZipFile, ZipFile

from .administration import _check, _finish
from .backups import backup_path, backup_runtime, state_base
from .core import DataError, H4, M5, M15, Rules, load_settings, safe_error
from .ledger import atomic_json
from .persistence import SCHEMA, SQLiteDatabase, SQLiteDocument, connect
from .state_codec import loads
from .state_paths import StatePaths, state_owner, validate_documents

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
PREVIEW_LIFETIME = 600


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _base(runtime):
    return Path(getattr(runtime, "state_base", state_base(runtime.store.path))).resolve()


def _journal_path(base):
    return Path(base).resolve().with_suffix(".restore-pending.json")


def _write_bytes(path, raw):
    """Durably stage bytes beside their destination before atomic replacement."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+".", suffix=".tmp", delete=False) as out:
            temporary = Path(out.name)
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _settings(raw):
    # Use the exact startup parser without touching the saved configuration.
    loads(raw.decode("utf-8-sig"))
    with tempfile.TemporaryDirectory(prefix="restore-settings-") as folder:
        path = Path(folder)/"settings.json"
        path.write_bytes(raw)
        return load_settings(path)


def _read_archive(runtime):
    path = backup_path(_base(runtime))
    if not path.is_file():
        raise DataError("No backup is available in backup/latest.zip.")
    if path.stat().st_size > MAX_ARCHIVE:
        raise DataError("Backup exceeds the guided restore size limit (256 MiB).")
    raw = path.read_bytes()
    with ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 64:
            raise DataError("Backup contains duplicate or excessive entries.")
        if sum(item.file_size for item in archive.infolist()) > MAX_EXPANDED:
            raise DataError("Expanded backup exceeds the guided restore size limit (512 MiB).")
        manifest = loads(archive.read("manifest.json").decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise DataError("Unsupported backup manifest version.")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(names) != set(files) | {"manifest.json"}:
            raise DataError("Backup manifest does not describe every archived file.")
        entries = {}
        for name, record in files.items():
            if not isinstance(record, dict):
                raise DataError("Malformed backup file record.")
            contents = archive.read(name)
            if len(contents) != record.get("size") or _sha(contents) != record.get("sha256"):
                raise DataError(f"Backup file verification failed: {name}")
            entries[name] = contents
    if manifest.get("backend") != "sqlite":
        raise DataError("Guided restore requires a complete SQLite backup. JSON archives must be migrated and verified separately.")
    saved = entries.get("settings/settings.json")
    if saved is None or manifest.get("settings_present") is not True:
        raise DataError("Backup has no saved settings; a partial restore is not allowed.")
    saved_assets, saved_rules, refresh = _settings(saved)
    if type(manifest.get("created_ms")) is not int or manifest["created_ms"] < 0:
        raise DataError("Invalid backup creation date.")
    basename = manifest.get("state_base")
    if not isinstance(basename, str) or Path(basename).name != basename or "/" in basename or "\\" in basename:
        raise DataError("Invalid backup state filename.")
    database_name = "state/"+Path(basename).with_suffix(".sqlite3").name
    if set(name for name in entries if name.startswith("state/")) != {database_name}:
        raise DataError("Backup has mixed or incomplete state files; a partial restore is not allowed.")
    database_bytes = entries[database_name]
    with tempfile.TemporaryDirectory(prefix="restore-inspect-") as folder:
        database_path = Path(folder)/"study.sqlite3"
        database_path.write_bytes(database_bytes)
        with SQLiteDatabase(database_path, owned=True) as database:
            schema = database._connection.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
            expected = {statement.split()[2]: " ".join(statement.lower().split()) for statement in SCHEMA}
            if ({name: " ".join(statement.lower().split()) for kind, name, statement in schema if kind == "table"} != expected
                    or any(kind != "table" for kind, name, statement in schema)):
                raise DataError("Backup database has an unsupported schema, index, view or trigger.")
            information = database.information()
            documents = database.documents()
            contexts = information["contexts"]
            _validate(documents, contexts)
    if "metadata/documents.json" in entries:
        metadata = loads(entries["metadata/documents.json"].decode("utf-8"))
        if metadata != {"documents": documents, "contexts": contexts}:
            raise DataError("Backup database differs from its recorded documents or settings.")
    active = runtime.rules.paper_namespace
    if active not in documents or "positions" not in documents:
        raise DataError(f"Backup must contain {active} paper history and positions for this dashboard.")
    context = contexts[active]
    rules = Rules(**context["rules"]).validate()
    if rules.strategy_model != runtime.rules.strategy_model:
        raise DataError("Backup uses a different active paper strategy; this dashboard cannot activate it.")
    if contexts["positions"]["market_mode"] != rules.market_mode:
        raise DataError("Backup paper and position market settings disagree.")
    return {"sha256": _sha(raw), "manifest": manifest, "settings": saved,
            "saved_settings": loads(saved.decode("utf-8-sig")), "database": database_bytes,
            "documents": documents, "contexts": contexts, "assets": context["assets"],
            "rules": rules, "refresh": refresh,
            "saved_differs": saved_assets != context["assets"] or saved_rules != rules}


def _validate(documents, contexts):
    if not isinstance(documents, dict) or not documents or not isinstance(contexts, dict) or set(contexts) != set(documents):
        raise DataError("Backup contains missing or mismatched namespace settings.")
    for name, context in contexts.items():
        if name == "positions":
            if not isinstance(context, dict) or context.get("market_mode") not in {"spot", "margin"}:
                raise DataError("Invalid positions settings in backup.")
        else:
            assets, rules, _ = _settings(json.dumps({"assets": [dict(name=key, **value) for key, value in context["assets"].items()],
                "strategy": context["rules"]}).encode())
            if assets != context["assets"] or rules.paper_namespace != name:
                raise DataError("Backup namespace settings do not match their history.")
            document = documents[name]
            if name in {"neural", "smc"} and Rules(**document["settings"]).validate() != rules:
                raise DataError("Backup paper settings disagree with the recorded namespace settings.")
            for asset, configuration in assets.items():
                if document["assets"].get(asset, {}).get("symbol") != configuration["symbol"]:
                    raise DataError("Backup currency pairs disagree with the recorded paper history.")
    validate_documents(documents, contexts)


def _require_sqlite(runtime):
    if not isinstance(runtime.store._backend, SQLiteDocument):
        raise DataError("Guided restore is available for SQLite studies only; no files were changed.")
    if runtime.positions._backend.database is not runtime.store._backend.database:
        raise DataError("Paper and positions must share the same database.")


def _counts(documents):
    result = {}
    for name, document in documents.items():
        trades = [trade for record in document.get("assets", {}).values() for trade in record.get("trades", [])]
        result[name] = {"paper_trades": len(trades), "open_paper_trades": sum(t.get("status") in {"active", "open"} for t in trades),
            "positions": len(document.get("positions", [])), "buy_watches": len(document.get("buy_watches", [])),
            "queued_alerts": sum(event.get("status") == "queued" for event in document.get("outbox", []))}
    return result


def preview_restore(runtime, generation):
    with runtime.lock, runtime.store.lock, runtime.positions.lock:
        _check(runtime, generation)
        _require_sqlite(runtime)
        if _journal_path(_base(runtime)).exists():
            raise DataError("A previous restore needs startup recovery. Restart the dashboard before restoring again.")
        captured = _read_archive(runtime)
        token = secrets.token_urlsafe(32)
        runtime._restore_preview = {"token": token, "generation": generation, "sha256": captured["sha256"],
                                    "settings_sha256": _sha(runtime.settings_path.read_bytes()), "expires": time.monotonic()+PREVIEW_LIFETIME}
        return {"token": token, "created_ms": captured["manifest"]["created_ms"], "action": captured["manifest"].get("action", "backup"),
                "archive": str(backup_path(_base(runtime))), "settings": captured["saved_settings"],
                "active_strategy": captured["rules"].strategy_model, "saved_settings_differ": captured["saved_differs"],
                "records": _counts(captured["documents"]), "expires_seconds": PREVIEW_LIFETIME}


def _copy_database(database, snapshot):
    """Replace the owned connection atomically, retaining its workers and locks."""
    source = connect(snapshot, readonly=True)
    try:
        source.backup(database._connection)
    except BaseException:
        database._poisoned = True
        raise
    finally:
        source.close()
        database._cache.clear()
    database._poisoned = False
    database._check_identity()
    database.information()


def _snapshot_bytes(database, folder, filename):
    path = Path(folder)/filename
    database.backup(path)
    return path.read_bytes()


def _journal(base, settings_path, database_bytes, settings_bytes):
    return {"version": 1, "state_base": str(Path(base).resolve()), "settings_path": str(settings_path),
            "database_sha256": _sha(database_bytes), "settings_sha256": _sha(settings_bytes),
            "database": base64.b64encode(database_bytes).decode("ascii"),
            "settings": base64.b64encode(settings_bytes).decode("ascii")}


def _rollback(database, settings_path, journal, folder):
    database_raw = base64.b64decode(journal["database"], validate=True)
    settings_raw = base64.b64decode(journal["settings"], validate=True)
    if _sha(database_raw) != journal["database_sha256"] or _sha(settings_raw) != journal["settings_sha256"]:
        raise DataError("Restore recovery journal failed verification.")
    snapshot = Path(folder)/"rollback.sqlite3"
    snapshot.write_bytes(database_raw)
    with SQLiteDatabase(snapshot, owned=True) as original:
        _validate(original.documents(), original.information()["contexts"])
    _copy_database(database, snapshot)
    _write_bytes(settings_path, settings_raw)


def recover_pending_restore(base, settings_path):
    """Roll back interrupted restore before startup can normalize or use its state."""
    marker = _journal_path(base)
    if not marker.exists():
        return False
    paths = StatePaths(base)
    settings_path = Path(settings_path).resolve()
    with state_owner(paths):
        if not marker.exists():
            return False
        journal = loads(marker.read_text(encoding="utf-8"))
        if (journal.get("version") != 1 or journal.get("state_base") != str(paths.base)
                or journal.get("settings_path") != str(settings_path)):
            raise DataError("Restore recovery journal belongs to a different study or settings file.")
        with SQLiteDatabase(paths.database, owned=True) as database, tempfile.TemporaryDirectory(prefix="restore-recover-") as folder:
            _rollback(database, settings_path, journal, folder)
            marker.unlink()
    return True


def _engine(runtime, captured):
    assets, rules = captured["assets"], captured["rules"]
    if runtime.is_neural:
        from .neural_engine import NeuralEngine
        path = Path(rules.nn_model_path) if rules.nn_model_path else None
        if path is not None and not path.is_absolute():
            path = runtime.settings_path.parent/path
        engine = NeuralEngine(assets, rules, runtime.store, model_path=path,
                              settings_path=runtime.settings_path)
        if engine.model_error:
            raise DataError(engine.model_error)
        return engine
    from .smc_engine import SMCEngine
    from .engine import Engine
    return (SMCEngine if runtime.is_smc else Engine)(assets, rules, runtime.store)


def confirm_restore(runtime, generation, token, confirmation):
    if confirmation != "restore":
        raise DataError("Confirm restoration of all settings and trading history.")
    with runtime.activity_gate.maintenance(), runtime.lock, runtime.store.lock, runtime.positions.lock:
        _check(runtime, generation)
        _require_sqlite(runtime)
        preview = getattr(runtime, "_restore_preview", None)
        if (not isinstance(token, str) or not preview or not secrets.compare_digest(preview["token"], token)
                or preview["generation"] != generation or time.monotonic() > preview["expires"]):
            raise DataError("Restore preview expired. Preview the backup again.")
        captured = _read_archive(runtime)
        if captured["sha256"] != preview["sha256"] or _sha(runtime.settings_path.read_bytes()) != preview["settings_sha256"]:
            raise DataError("Backup or saved settings changed after preview. Preview the backup again.")
        marker = _journal_path(_base(runtime))
        if marker.exists():
            raise DataError("An interrupted restore requires startup recovery first.")
        engine = _engine(runtime, captured)
        from .gex import NaiveGEX
        from .position_neural import PositionNeuralReader
        gex = NaiveGEX(captured["assets"], provider=runtime.gex.provider)
        database = runtime.store._backend.database
        with tempfile.TemporaryDirectory(prefix="restore-apply-") as folder:
            candidate = Path(folder)/"candidate.sqlite3"
            candidate.write_bytes(captured["database"])
            with SQLiteDatabase(candidate, owned=True) as incoming:
                documents = incoming.documents()
                for document in documents.values():
                    for event in document.get("outbox", []):
                        # Ordinary cancellations can recover after an outage.
                        # Restored identities must remain retired permanently,
                        # including cancellations already saved in the archive.
                        if event.get("status") in {"queued", "cancelled", "running", "failed", "uncertain"}:
                            event.update(retired=True, restore_suppressed=True)
                        if event.get("status") in {"queued", "cancelled"}:
                            event.update(status="cancelled", error="Historical undelivered alert retired during backup restore.")
                        elif event.get("status") == "running":
                            event.update(status="uncertain", error="Delivery was in progress in the restored backup; it will not be resent.")
                incoming.replace_documents(documents, captured["contexts"])
                incoming.checkpoint()
            previous_settings = runtime.settings_path.read_bytes()
            previous_database = _snapshot_bytes(database, folder, "previous.sqlite3")
            backup = str(backup_runtime(runtime, action="before-restore"))
            journal = _journal(_base(runtime), runtime.settings_path, previous_database, previous_settings)
            atomic_json(marker, journal)
            try:
                _write_bytes(runtime.settings_path, captured["settings"])
                _copy_database(database, candidate)
                marker.unlink()
            except BaseException as exc:
                try:
                    _rollback(database, runtime.settings_path, journal, folder)
                    marker.unlink()
                except BaseException as rollback_error:
                    runtime.paused = True
                    runtime.stop.set()
                    runtime.wake.set()
                    raise DataError("Restore could not finish or roll back. Monitoring stopped. Restart to recover the pre-restore settings and database.") from rollback_error
                raise DataError("Restore failed; the previous settings and trading data were recovered.") from exc
        runtime._restore_preview = None
        runtime.assets, runtime.rules, runtime.refresh = captured["assets"], captured["rules"], captured["refresh"]
        runtime.engine, runtime.gex = engine, gex
        runtime.is_combined = runtime.rules.combined
        runtime.high_interval = runtime.rules.smc_setup_minutes*60000 if runtime.is_smc else H4
        runtime.low_interval = runtime.rules.smc_entry_minutes*60000 if runtime.is_smc else M5 if runtime.is_neural else M15
        runtime.positions.market_mode = runtime.rules.market_mode
        for name, store in ((runtime.rules.paper_namespace, runtime.store), ("positions", runtime.positions)):
            store._backend.context = copy.deepcopy(captured["contexts"][name])
        if runtime.rules.paper_namespace == "legacy":
            from .ledger import validate_state
            assets, rules = runtime.assets, runtime.rules
            runtime.store.assets, runtime.store.rules = assets, rules
            runtime.store._validator = lambda document: validate_state(document, assets, rules)
        runtime.position_neural, runtime.paper_neural = PositionNeuralReader(), PositionNeuralReader()
        runtime.structure_baselined = set()
        runtime.paused = bool(runtime.store.snapshot().get("monitoring_paused"))
        message = "Backup restored: saved settings and all trading history. Historical queued alerts were cancelled."
        if captured["saved_differs"]:
            message += " Archived active settings are running; use Apply saved settings to activate the saved edits from this backup."
        return _finish(runtime, message, backup)


def register_restore_routes(app, runtime, position_payload):
    """The confirm endpoint must be excluded from the ordinary request activity gate."""
    from flask import jsonify, session

    @app.post("/api/database/restore/preview")
    def preview_backup_restore():
        try:
            position_payload(set())
            return jsonify(preview_restore(runtime, session["generation"]))
        except (DataError, ValueError, TypeError, KeyError, OSError, BadZipFile, sqlite3.Error) as exc:
            return jsonify({"error": safe_error(exc)}), 400

    @app.post("/api/database/restore")
    def confirm_backup_restore():
        try:
            payload = position_payload({"token", "confirm"})
            return jsonify(confirm_restore(runtime, session["generation"], payload["token"], payload["confirm"]))
        except (DataError, ValueError, TypeError, KeyError, OSError, BadZipFile, sqlite3.Error) as exc:
            return jsonify({"error": safe_error(exc)}), 400
