"""Read-only, repeatable four-namespace backup and record fingerprint audit."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from zipfile import ZipFile

from adaptive_crypto.core import load_settings
from adaptive_crypto.persistence import SQLiteDatabase


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def checked_archive(path):
    with ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Backup archive has a corrupt member")
        manifest = json.loads(archive.read("manifest.json"))
        files = manifest.get("files")
        if not isinstance(files, dict) or set(archive.namelist()) != set(files) | {"manifest.json"}:
            raise ValueError("Backup member inventory does not match its manifest")
        for name, expected in files.items():
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != expected["sha256"] or len(raw) != expected["size"]:
                raise ValueError(f"Backup member differs from its manifest: {name}")
        metadata = json.loads(archive.read("metadata/documents.json"))
        if set(metadata["documents"]) != {"legacy", "smc", "neural", "positions"}:
            raise ValueError("Backup is missing one or more study namespaces")
        database_members = [name for name in files if name.startswith("state/") and name.endswith(".sqlite3")]
        if manifest["backend"] != "sqlite" or len(database_members) != 1:
            raise ValueError("Backup is missing the SQLite database")
        with tempfile.TemporaryDirectory(prefix="nn-candidate-archive-check-") as folder:
            copied = Path(folder) / Path(database_members[0]).name
            copied.write_bytes(archive.read(database_members[0]))
            with SQLiteDatabase(copied, owned=True) as store:
                if store._connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup SQLite database failed integrity check")
                if store.documents() != metadata["documents"]:
                    raise ValueError("Backup metadata differs from its SQLite database")
        return {"sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                "settings_sha256": hashlib.sha256(archive.read("settings/settings.json")).hexdigest(),
                "namespace_document_hashes": {name: digest(document)
                                              for name, document in metadata["documents"].items()},
                "manifest_created_ms": manifest["created_ms"]}


def snapshot_documents(database):
    # SQLite's online backup API captures committed WAL content atomically
    # while the dashboard may be running. The live DB is never opened writable.
    database = Path(database).resolve()
    with tempfile.TemporaryDirectory(prefix="nn-candidate-audit-") as folder:
        copied = Path(folder) / "snapshot.sqlite3"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(copied)) as target:
                source.backup(target)
        with SQLiteDatabase(copied, owned=True) as store:
            if store._connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite snapshot integrity check failed")
            return store.documents()


def immutable_facts(documents):
    facts = {}
    for namespace in ("legacy", "smc", "neural"):
        trades = []
        for asset, row in documents[namespace]["assets"].items():
            for trade in row.get("trades", []):
                trades.append({"asset": asset, "id": trade.get("id"), "opened_ms": trade.get("opened_ms"),
                               "entry": trade.get("entry"), "quantity": trade.get("quantity"),
                               "entry_fee": trade.get("entry_fee"), "stop": trade.get("stop")})
        facts[namespace] = sorted(trades, key=lambda item: (item["asset"], str(item["id"])))
    facts["positions"] = sorted(({"id": row.get("id"), "asset": row.get("asset"),
                                  "opened_ms": row.get("opened_ms"), "entry": row.get("entry"),
                                  "quantity": row.get("quantity")}
                                 for row in documents["positions"].get("positions", [])),
                                key=lambda item: str(item["id"]))
    return facts


def audit(database, settings, backup):
    archive = checked_archive(backup)
    documents = snapshot_documents(database)
    if set(documents) != {"legacy", "smc", "neural", "positions"}:
        raise ValueError("Current SQLite state is missing a study namespace")
    _, rules, _ = load_settings(settings)
    facts = immutable_facts(documents)
    settings_hash = hashlib.sha256(Path(settings).read_bytes()).hexdigest()
    namespace_hashes = {name: digest(document) for name, document in documents.items()}
    return {"settings_sha256": settings_hash,
            "saved_nn_model_id": rules.nn_model_id, "saved_nn_limitations": rules.nn_limitations,
            "backup": archive,
            "matches_backup": (settings_hash == archive["settings_sha256"] and
                               namespace_hashes == archive["namespace_document_hashes"]),
            "current_namespace_hashes": namespace_hashes,
            "immutable_record_hashes": {name: digest(rows) for name, rows in facts.items()},
            "immutable_record_counts": {name: len(rows) for name, rows in facts.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-backup-match", action="store_true",
                        help="fail unless current settings and all four namespaces equal the archive")
    args = parser.parse_args()
    result = audit(args.database, args.settings, args.backup)
    if args.require_backup_match and not result["matches_backup"]:
        raise ValueError("Current settings or state differs from the complete backup")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "namespaces": list(result["current_namespace_hashes"]),
                      "records": result["immutable_record_counts"],
                      "nn_model_id": result["saved_nn_model_id"],
                      "nn_limitations": result["saved_nn_limitations"],
                      "matches_backup": result["matches_backup"]}))


if __name__ == "__main__":
    main()
