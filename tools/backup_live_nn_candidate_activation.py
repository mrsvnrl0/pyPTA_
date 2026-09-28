"""Create a coherent four-namespace backup while the dashboard owns its state lock.

The live SQLite database is copied with its online backup API. Existing backup
code then archives only that stable copy; settings are checked for changes
before publishing the verified ZIP beside the live database.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from adaptive_crypto.backups import create_backup
from adaptive_crypto.core import load_settings
from adaptive_crypto.persistence import SQLiteDatabase
from adaptive_crypto.state_lock import SingleInstance
from adaptive_crypto.state_paths import StatePaths, validate_documents
from tools.audit_nn_candidate_activation import checked_archive, digest


def create_live_backup(state: Path, settings: Path) -> dict:
    state, settings = Path(state).resolve(), Path(settings).resolve()
    paths = StatePaths(state)
    if not paths.database.is_file() or not settings.is_file():
        raise ValueError("The live SQLite database and settings file are required")
    initial_settings = settings.read_bytes()
    _, rules, _ = load_settings(settings)
    if settings.read_bytes() != initial_settings:
        raise ValueError("Settings changed during backup preparation")
    with tempfile.TemporaryDirectory(prefix="nn-candidate-live-backup-") as folder:
        temporary = Path(folder)
        snapshot = temporary / paths.database.name
        with closing(sqlite3.connect(paths.database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
        with SQLiteDatabase(snapshot, owned=True) as db:
            if db._connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Live SQLite snapshot failed integrity check")
            documents = db.documents()
            contexts = db.information()["contexts"]
            if set(documents) != {"legacy", "smc", "neural", "positions"}:
                raise ValueError("Live snapshot is missing a study namespace")
            validate_documents(documents, contexts)
            staged = create_backup(temporary / state.name, settings=settings,
                                   database=db, action="nn-candidate-live-activation")
        checked = checked_archive(staged)
        if (checked["namespace_document_hashes"] !=
                {name: digest(document) for name, document in documents.items()}):
            raise ValueError("Backup metadata differs from its stable SQLite snapshot")
        settings_hash = hashlib.sha256(initial_settings).hexdigest()
        if checked["settings_sha256"] != settings_hash or settings.read_bytes() != initial_settings:
            raise ValueError("Settings changed while creating the backup")

        destination_dir = state.parent / "backup"
        destination_dir.mkdir(parents=True, exist_ok=True)
        if destination_dir.is_symlink() or destination_dir.resolve().parent != state.parent:
            raise ValueError("Backup destination must be directly inside the study root")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        retained = destination_dir / f"nn-candidates-live-{stamp}.zip"
        previous = destination_dir / f"pre-nn-candidates-live-{stamp}.zip"
        latest = destination_dir / "latest.zip"
        if retained.exists() or previous.exists():
            raise FileExistsError("A backup with this timestamp already exists")
        with SingleInstance(state.parent / ".pypta-backup.lock"):
            if latest.is_file():
                shutil.copyfile(latest, previous)
            shutil.copyfile(staged, retained)
            if hashlib.sha256(retained.read_bytes()).hexdigest() != checked["sha256"]:
                raise ValueError("Published retained backup differs from the verified archive")
            pending = destination_dir / f".pending-nn-candidate-{os.getpid()}.zip"
            if pending.exists():
                raise FileExistsError("Pending backup path already exists")
            shutil.copyfile(retained, pending)
            os.replace(pending, latest)
        if settings.read_bytes() != initial_settings:
            raise ValueError("Settings changed while publishing the backup; retry with current settings")
        return {"backup": str(retained), "latest": str(latest), "sha256": checked["sha256"],
                "settings_sha256": settings_hash, "nn_model_id": rules.nn_model_id,
                "nn_limitations": rules.nn_limitations,
                "namespace_document_hashes": checked["namespace_document_hashes"]}


def main() -> None:
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    result = create_live_backup(args.state, args.settings)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
