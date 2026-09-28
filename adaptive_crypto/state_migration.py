"""Offline, explicit JSON import and coherent SQLite verification/export/backup."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from .core import DataError, load_settings
from .backups import backup_path, create_backup
from .ledger import atomic_json, prepare_state
from .smc_ledger import prepare_smc
from .neural_ledger import prepare_neural
from .positions import prepare_positions
from .persistence import SQLiteDatabase, check_sqlite_runtime
from .state_codec import decode, encode
from .state_paths import StatePaths, state_owner, validate_documents

BASE = Path(__file__).resolve().parent.parent


def counts(document):
    result = {name: len(document[name]) for name in ("outbox", "positions", "watches", "buy_watches") if name in document}
    if "assets" in document:
        result.update(assets=len(document["assets"]), trades=sum(len(a["trades"]) for a in document["assets"].values()),
                      consumed=sum(len(a["consumed"]) for a in document["assets"].values()))
    return result


def _prepare(paths, settings):
    assets, rules, _ = load_settings(settings)
    settings_bytes = Path(settings).read_bytes() if Path(settings).exists() else None
    settings_hash = hashlib.sha256(settings_bytes).hexdigest() if settings_bytes is not None else None
    active = rules.paper_namespace
    sources, documents, provenance = {}, {}, {}
    for name in (active, "positions"):
        path = paths.sources[name]
        source = path.read_bytes() if path.exists() else None
        sources[name] = source
        if name == "legacy":
            document, backups = prepare_state(source, assets, rules)
        elif name == "smc":
            document, backups = prepare_smc(source, assets, rules)
        elif name == "neural":
            document, backups = prepare_neural(source, assets, rules)
        else:
            document, backups = prepare_positions(source, rules.market_mode)
        context = {"market_mode": rules.market_mode} if name == "positions" else {"assets": assets, "rules": asdict(rules)}
        validate_documents({name: document}, {name: context})
        root, rows = encode(document)
        if decode(root, rows) != document:
            raise DataError("State codec failed import roundtrip")
        documents[name] = document
        provenance[name] = {"source_path": str(path) if source is not None else None,
                            "source_sha256": hashlib.sha256(source).hexdigest() if source is not None else None,
                            "settings_sha256": settings_hash, "imported_ms": int(time.time()*1000),
                            "context": context, "domain_version": document["version"], "counts": counts(document),
                            "backups_required": backups, "startup_normalization": True}
    return sources, documents, provenance, settings_bytes


def _matching(existing, proposed, namespace):
    keys = ("source_path", "source_sha256") if namespace == "positions" else ("source_path", "source_sha256", "settings_sha256")
    return all(existing.get(key) == proposed.get(key) for key in keys)


def _publish_database(staging, target):
    if target.exists():
        raise DataError("Database appeared before publication; refusing overwrite")
    # Same-directory rename after a complete checkpoint and close; owner lock held.
    staging.rename(target)


def import_state(base, settings, *, dry_run=False):
    paths = StatePaths(base)
    if dry_run:
        _, documents, provenance, _ = _prepare(paths, settings)
        return {"dry_run": True, "advisory": "Run with the dashboard stopped; real import repeats validation under ownership locks.",
                "database_exists": paths.database.exists(), "counts": {k: counts(v) for k, v in documents.items()},
                "proposed_imports": provenance}
    check_sqlite_runtime()
    with state_owner(paths), ExitStack() as stack:
        sources, documents, provenance, _ = _prepare(paths, settings)
        db = None
        if paths.database.exists():
            db = stack.enter_context(SQLiteDatabase(paths.database, owned=True))
            info = db.information()
            saved = db.documents()
            validate_documents(saved, info["contexts"])
            for name in list(documents):
                if name in saved:
                    if not _matching(info["imports"].get(name, {}), provenance[name], name):
                        raise DataError(f"Source or settings conflict for {name}; existing database state was not replaced")
                    del documents[name]
            if not documents:
                return {"status": "already_imported", "database": str(paths.database), "revisions": info["revisions"]}
        inactive = [str(path) for name, path in paths.sources.items() if name not in sources and path.exists()]
        original_sources = {path: path.read_bytes() for path in paths.sources.values() if path.is_file()}
        original_sources.update({paths.sources[name]: raw for name, raw in sources.items() if raw is not None})
        backup = create_backup(base, settings=settings, database=db, sources=original_sources,
                               documents=documents, contexts={name: info["context"] for name, info in provenance.items()},
                               action="sqlite-import", import_provenance=provenance)
        for info in provenance.values():
            info["backup"] = str(backup)
        if db is None:
            staging = paths.database.with_name(paths.database.name+".staging-"+uuid.uuid4().hex)
            with SQLiteDatabase(staging, create=True, owned=True) as staged:
                staged.import_documents(documents, provenance)
                info = staged.information()
                if staged.documents() != documents:
                    raise DataError("Imported database differs from normalized sources")
                staged.checkpoint()
            _publish_database(staging, paths.database)
        else:
            db.import_documents(documents, provenance)
            info = db.information()
        return {"status": "imported", "database": str(paths.database), "backup": str(backup),
                "counts": {k: counts(v) for k, v in documents.items()}, "revisions": info["revisions"],
                "inactive_sources_untouched": inactive}


def _write_bytes(path, value):
    with Path(path).open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def verify_state(base):
    paths = StatePaths(base)
    with state_owner(paths), SQLiteDatabase(paths.database, owned=True) as db:
        info = db.information()
        documents = db.documents()
        validate_documents(documents, info["contexts"])
        info["counts"] = {name: counts(doc) for name, doc in documents.items()}
        return info


def export_state(base, output_dir, settings=None):
    paths = StatePaths(base)
    output = Path(output_dir).resolve()
    if output.exists():
        raise DataError("Export directory already exists; use a new directory")
    with state_owner(paths), SQLiteDatabase(paths.database, owned=True) as db:
        info = db.information()
        documents = db.documents()
        validate_documents(documents, info["contexts"])
        if settings is not None:
            assets, rules, _ = load_settings(settings)
            namespace = rules.paper_namespace
            context = info["contexts"].get(namespace)
            if context != {"assets": assets, "rules": asdict(rules)}:
                raise DataError("Export settings do not match the saved active paper context")
            settings_bytes = Path(settings).read_bytes()
        else:
            settings_bytes = None
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=output.name+".staging-", dir=output.parent))
        for name, document in documents.items():
            atomic_json(staging/paths.sources[name].name, document)
        if settings_bytes is not None:
            _write_bytes(staging/"settings.json", settings_bytes)
        # Also provide exact per-strategy settings for both stored study namespaces.
        for name in ("legacy", "smc", "neural"):
            if name in info["contexts"]:
                context = info["contexts"][name]
                atomic_json(staging/(name+".settings.json"), {"assets": [{"name": asset, **cfg} for asset, cfg in context["assets"].items()], "strategy": context["rules"]})
        atomic_json(staging/"export-manifest.json", {"base_name": paths.base.name, "database": str(paths.database), **info,
                                                   "missing_namespaces": sorted(set(paths.sources)-set(documents))})
        if output.exists():
            raise DataError("Export directory appeared before publication")
        staging.rename(output)
        return {"output_dir": str(output), "state_base": str(output/paths.base.name), "namespaces": sorted(documents)}


def backup_state(base, output=None, *, settings=None):
    paths = StatePaths(base)
    target = backup_path(base)
    if output is not None and Path(output).resolve() != target:
        raise DataError(f"Backups use a single archive: {target}. Omit --output to replace it.")
    with state_owner(paths), ExitStack() as stack:
        db = stack.enter_context(SQLiteDatabase(paths.database, owned=True)) if paths.database.exists() else None
        if db is not None:
            validate_documents(db.documents(), db.information()["contexts"])
        elif not any(path.is_file() for path in paths.sources.values()):
            raise DataError("No trading data found at this state path")
        create_backup(base, settings=settings, database=db, action="manual")
    return {"backup": str(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("import", "verify", "export", "backup"):
        p = sub.add_parser(command)
        p.add_argument("--state", required=True, type=Path)
        if command == "import":
            p.add_argument("--settings", type=Path, default=BASE/"adaptive_crypto_settings.json")
            p.add_argument("--dry-run", action="store_true")
        if command == "export":
            p.add_argument("--output-dir", required=True, type=Path)
            p.add_argument("--settings", type=Path)
        if command == "backup":
            p.add_argument("--output", type=Path, help="Optional; must be backup/latest.zip beside the state base")
            p.add_argument("--settings", type=Path, help="Settings file to include (defaults beside state)")
    args = parser.parse_args()
    try:
        if args.command == "import":
            result = import_state(args.state, args.settings, dry_run=args.dry_run)
        elif args.command == "verify":
            result = verify_state(args.state)
        elif args.command == "export":
            result = export_state(args.state, args.output_dir, args.settings)
        else:
            result = backup_state(args.state, args.output, settings=args.settings)
    except (DataError, OSError, ValueError) as exc:
        parser.exit(1, str(exc)+"\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
