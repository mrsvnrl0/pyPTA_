"""Canonical state paths, backend selection, and application store lifetimes."""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from pathlib import Path

from .core import DataError
from .state_lock import SingleInstance


@dataclass(frozen=True)
class StatePaths:
    base: Path

    def __post_init__(self):
        object.__setattr__(self, "base", Path(self.base).resolve())
        if self.base.suffix.lower() == ".sqlite3":
            raise DataError("--state is the logical base path (for example study.json), not the SQLite filename")

    @property
    def database(self):
        return self.base.with_suffix(".sqlite3")

    @property
    def sources(self):
        return {"legacy": self.base, "smc": self.base.with_suffix(".smc.json"),
                "neural": self.base.with_suffix(".neural.json"),
                "positions": self.base.with_suffix(".positions.json")}

    def select(self, requested):
        if requested not in {"auto", "json", "sqlite"}:
            raise DataError("Unknown state backend")
        if self.database.exists():
            if requested == "json":
                raise DataError("SQLite state exists; export it to a new base before selecting JSON")
            return "sqlite"
        has_json = any(path.exists() for path in self.sources.values())
        if requested == "sqlite" and has_json:
            raise DataError("JSON state exists; first run python -m adaptive_crypto.state_migration import")
        return ("json" if has_json else "sqlite") if requested == "auto" else requested


@contextmanager
def state_owner(paths):
    paths.base.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(SingleInstance(str(paths.base)+".lock"))
        stack.enter_context(SingleInstance(str(paths.database)+".lock"))
        yield


def validate_documents(documents, contexts):
    """Validate saved state against its saved configuration, without recovery."""
    from .core import Rules
    from .ledger import validate_state, upgrade_state
    from .smc_ledger import validate as validate_smc
    from .positions import validate_positions
    try:
        for name, document in documents.items():
            if name == "legacy":
                context = contexts[name]
                rules = Rules(**context["rules"]).validate()
                validate_state(upgrade_state(document, context["assets"], rules), context["assets"], rules)
            elif name == "smc":
                Rules(**contexts[name]["rules"]).validate()
                validate_smc(document)
            elif name == "positions":
                validate_positions(document)
            elif name == "neural":
                from .neural_ledger import validate as validate_neural
                Rules(**contexts[name]["rules"]).validate()
                validate_neural(document)
            else:
                raise DataError(f"Unknown state namespace: {name}")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError(f"Invalid saved database state: {exc}") from exc


@contextmanager
def open_stores(base, assets, rules, backend="auto", *, settings_path=None):
    from .ledger import StateStore, prepare_state
    from .smc_ledger import SMCStore, prepare_smc
    from .positions import PositionStore, prepare_positions
    from dataclasses import asdict
    from .persistence import SQLiteDatabase
    paths = StatePaths(base)
    from .neural_ledger import NeuralStore, prepare_neural
    name = rules.paper_namespace
    cls = {"smc": SMCStore, "neural": NeuralStore, "legacy": StateStore}[name]
    prepare = {"smc": prepare_smc, "neural": prepare_neural, "legacy": prepare_state}[name]
    from .backups import backup_scope
    with state_owner(paths), backup_scope(base, settings_path), ExitStack() as stack:
        selected = paths.select(backend)
        db = None
        if selected == "sqlite":
            if not paths.database.exists():
                prepared, _ = prepare(None, assets, rules)
                holdings, _ = prepare_positions(None, rules.market_mode)
                _initialize_database(paths, {name: prepared, "positions": holdings},
                                     {name: {"assets": assets, "rules": asdict(rules)}, "positions": {"market_mode": rules.market_mode}})
            db = stack.enter_context(SQLiteDatabase(paths.database, owned=True))
            existing = db.documents()
            validate_documents(existing, db.information()["contexts"])
            for namespace in (name, "positions"):
                if namespace not in existing and paths.sources[namespace].exists():
                    raise DataError(f"Import the missing {namespace} namespace explicitly with matching settings")
        paper = cls(paths.sources[name], assets, rules, backend=db.namespace(name) if db else None)
        positions = PositionStore(paths.sources["positions"], market_mode=rules.market_mode,
                                  backend=db.namespace("positions") if db else None)
        yield paper, positions


@contextmanager
def open_positions(base, backend="auto", market_mode="spot", *, settings_path=None):
    from .positions import PositionStore, prepare_positions
    from .persistence import SQLiteDatabase
    paths = StatePaths(base)
    from .backups import backup_scope
    with state_owner(paths), backup_scope(base, settings_path), ExitStack() as stack:
        db = None
        if paths.select(backend) == "sqlite":
            if not paths.database.exists():
                prepared, _ = prepare_positions(None, market_mode)
                _initialize_database(paths, {"positions": prepared}, {"positions": {"market_mode": market_mode}})
            db = stack.enter_context(SQLiteDatabase(paths.database, owned=True))
            if "positions" not in db.documents() and paths.sources["positions"].exists():
                raise DataError("Import the holdings namespace before correcting SQLite state")
        yield PositionStore(paths.sources["positions"], market_mode=market_mode,
                            backend=db.namespace("positions") if db else None)


def _initialize_database(paths, documents, contexts):
    """Publish a complete fresh database, never partially initialized namespaces."""
    import time
    import uuid
    from .persistence import SQLiteDatabase
    staging = paths.database.with_name(paths.database.name+".staging-"+uuid.uuid4().hex)
    provenance = {name: {"context": contexts[name], "imported_ms": int(time.time()*1000),
                         "source_path": None, "source_sha256": None, "origin": "fresh"} for name in documents}
    with SQLiteDatabase(staging, create=True, owned=True) as db:
        db.import_documents(documents, provenance)
        db.information()
        db.checkpoint()
    if paths.database.exists():
        raise DataError("Database appeared during initialization")
    staging.rename(paths.database)
