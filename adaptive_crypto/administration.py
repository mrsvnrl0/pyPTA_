"""Coordinated settings reload and backed-up dashboard resets."""
from contextlib import contextmanager
from dataclasses import asdict
import copy
import json
import threading
import time
import uuid

from .core import DataError, Rules, load_settings
from .backups import backup_runtime
from .gex import NaiveGEX
from .ledger import new_state, validate_state
from .persistence import SQLiteDocument
from .positions import prepare_positions, validate_positions
from .smc_engine import SMCEngine
from .smc_ledger import SMCStore, prepare_smc, validate as validate_smc


class ActivityGate:
    """Scans, requests and deliveries may overlap; maintenance excludes them all."""
    def __init__(self):
        self.condition = threading.Condition()
        self.active = 0
        self.waiting = 0
        self.exclusive = False

    @contextmanager
    def activity(self):
        with self.condition:
            self.condition.wait_for(lambda: not self.exclusive and not self.waiting)
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    @contextmanager
    def maintenance(self):
        with self.condition:
            self.waiting += 1
            try:
                self.condition.wait_for(lambda: not self.exclusive and not self.active)
                self.exclusive = True
            finally:
                self.waiting -= 1
        try:
            yield
        finally:
            with self.condition:
                self.exclusive = False
                self.condition.notify_all()


def _check(runtime, generation):
    if generation != runtime.generation:
        raise DataError("The dashboard changed since this page loaded. Reload before trying again.")
    if runtime.settings_path is None:
        raise DataError("Strategy controls require a configured settings file.")
    if runtime.stop.is_set():
        raise DataError("The dashboard is stopping; restart before changing its database.")


def _documents(runtime):
    backend = runtime.store._backend
    if isinstance(backend, SQLiteDocument):
        db = backend.database
        return db.documents(), db.information()["contexts"]
    namespace = runtime.rules.paper_namespace
    return {namespace: runtime.store.snapshot(), "positions": runtime.positions.snapshot()}, {
        namespace: {"assets": runtime.assets, "rules": asdict(runtime.rules)},
        "positions": {"market_mode": runtime.rules.market_mode}}


def _backup(runtime, action, documents, contexts):
    return str(backup_runtime(runtime, action=action, documents=documents, contexts=contexts))


def _save(runtime, documents, contexts, backup):
    stores = {runtime.rules.paper_namespace: runtime.store, "positions": runtime.positions}
    if runtime.store.database_path:
        runtime.store._backend.database.replace_documents(documents, contexts)
        for name, store in stores.items():
            store._backend.context = copy.deepcopy(contexts[name])
        return
    previous = {name: store.snapshot() for name, store in stores.items()}
    saved = []
    try:
        for name, store in stores.items():
            # Include the attempted write: a filesystem failure may follow replacement.
            saved.append(name)
            store._backend.save(documents[name])
    except Exception:
        try:
            for name in reversed(saved):
                stores[name]._backend.save(previous[name])
        except Exception as exc:
            runtime.paused = True
            runtime.stop.set()
            raise DataError(f"Database update and rollback failed. Monitoring stopped. Restore backup: {backup}") from exc
        raise


def _finish(runtime, message, backup):
    runtime.generation = uuid.uuid4().hex
    runtime.data = {}
    runtime.market = {}
    runtime.feed_observations = {}
    runtime.chart_fallback = {}
    runtime.position_errors = {}
    runtime.updated_ms = None
    runtime.error = None
    runtime.last_administration = {"message": message, "backup": backup, "at_ms": int(time.time()*1000)}
    runtime.wake.set()
    return copy.deepcopy(runtime.last_administration)


def apply_settings(runtime, generation):
    with runtime.activity_gate.maintenance(), runtime.lock, runtime.store.lock, runtime.positions.lock:
        _check(runtime, generation)
        if not runtime.settings_path.is_file():
            raise DataError(f"Settings file does not exist: {runtime.settings_path}")
        assets, rules, refresh = load_settings(runtime.settings_path)
        if rules.strategy_model != runtime.rules.strategy_model:
            raise DataError("Changing strategy_model requires a dashboard restart. Other settings can be applied here.")
        if runtime.is_neural:
            return _apply_neural(runtime, assets, rules, refresh)
        if rules.base_strategy == "legacy":
            return _apply_legacy(runtime, assets, rules, refresh)
        documents, contexts = _documents(runtime)
        previous_contexts = copy.deepcopy(contexts)
        proposed = copy.deepcopy(documents)
        paper = proposed["smc"]
        for name, cfg in assets.items():
            record = paper["assets"].get(name)
            if record and record["symbol"] != cfg["symbol"]:
                if record["trades"] or record["pending"]:
                    raise DataError(f"Purge the database before changing {name}'s pair.")
                paper["assets"][name] = SMCStore._record(cfg["symbol"])
        proposed["smc"], _ = prepare_smc(json.dumps(paper).encode(), assets, rules)
        proposed["positions"], _ = prepare_positions(json.dumps(proposed["positions"]).encode(), rules.market_mode)
        # Old pending messages must not be delivered using newly applied settings.
        for document in (proposed["smc"], proposed["positions"]):
            for event in document["outbox"]:
                if event["status"] == "queued":
                    event.update(status="cancelled", error="Settings applied; waiting for fresh evidence.")
        # A purged ledger starts with the equity from the newly edited file.
        if runtime.paused:
            proposed["smc"], _ = prepare_smc(None, assets, rules)
        contexts["smc"] = {"assets": assets, "rules": asdict(rules)}
        contexts["positions"] = {"market_mode": rules.market_mode}
        engine = SMCEngine(assets, rules, runtime.store)
        gex = NaiveGEX(assets, provider=runtime.gex.provider)
        backup = _backup(runtime, "settings", documents, previous_contexts)
        _save(runtime, proposed, contexts, backup)
        runtime.assets, runtime.rules, runtime.refresh = assets, rules, refresh
        runtime.high_interval, runtime.low_interval = rules.smc_setup_minutes*60000, rules.smc_entry_minutes*60000
        runtime.engine, runtime.gex = engine, gex
        runtime.positions.market_mode = rules.market_mode
        runtime.paused = False
        return _finish(runtime, "Settings applied. Monitoring is running with the saved JSON settings.", backup)


def purge_database(runtime, generation):
    with runtime.activity_gate.maintenance(), runtime.lock, runtime.store.lock, runtime.positions.lock:
        _check(runtime, generation)
        documents, contexts = _documents(runtime)
        proposed = {}
        for name in documents:
            context = contexts[name]
            if name == "positions":
                proposed[name], _ = prepare_positions(None, runtime.rules.market_mode)
            elif name == "smc":
                proposed[name], _ = prepare_smc(None, context["assets"], Rules(**context["rules"]))
            elif name == "legacy":
                rules = Rules(**context["rules"])
                proposed[name] = new_state(context["assets"], rules)
                validate_state(proposed[name], context["assets"], rules)
            elif name == "neural":
                from .neural_ledger import prepare_neural
                proposed[name], _ = prepare_neural(None, context["assets"], Rules(**context["rules"]))
            else:
                raise DataError(f"Cannot purge unknown database section: {name}")
        namespace = runtime.rules.paper_namespace
        if runtime.is_neural:
            from .neural_ledger import validate as validate_neural
            validate_neural(proposed[namespace])
        elif namespace == "smc":
            validate_smc(proposed[namespace])
        else:
            validate_state(proposed[namespace], runtime.assets, runtime.rules)
        validate_positions(proposed["positions"])
        proposed[namespace]["monitoring_paused"] = True
        backup = _backup(runtime, "purge", documents, contexts)
        _save(runtime, proposed, contexts, backup)
        runtime.paused = True
        return _finish(runtime, "Database cleared. Monitoring is paused. Apply settings to resume.", backup)


def _apply_neural(runtime, assets, rules, refresh):
    from pathlib import Path
    from .neural_engine import NeuralEngine
    from .neural_ledger import prepare_neural
    documents, contexts = _documents(runtime)
    previous_contexts = copy.deepcopy(contexts)
    proposed = copy.deepcopy(documents)
    proposed["neural"], _ = prepare_neural(None if runtime.paused else json.dumps(proposed["neural"]).encode(), assets, rules)
    proposed["positions"], _ = prepare_positions(json.dumps(proposed["positions"]).encode(), rules.market_mode)
    proposed["neural"].pop("monitoring_paused", None)
    for document in (proposed["neural"], proposed["positions"]):
        for event in document["outbox"]:
            if event["status"] == "queued":
                event.update(status="cancelled", error="Settings applied; waiting for fresh evidence.")
    path = Path(rules.nn_model_path) if rules.nn_model_path else None
    if path is not None and not path.is_absolute():
        path = runtime.settings_path.parent/path
    engine = NeuralEngine(assets, rules, runtime.store, model_path=path,
                          settings_path=runtime.settings_path)
    if engine.model_error:
        raise DataError(engine.model_error)
    for name, record in proposed["neural"]["assets"].items():
        if any(trade["status"] == "active" for trade in record["trades"]):
            if name not in assets:
                raise DataError(f"Keep {name} enabled while its neural paper trade is active")
            asset = assets[name]["symbol"].split("/")[0]
            if hasattr(engine.model, "trained_through_ms_for"):
                engine.model.trained_through_ms_for(asset)
            elif asset not in engine.model.volume_stats:
                raise DataError(f"Selected NN model has no trained artifact for {asset}")
    contexts["neural"] = {"assets": assets, "rules": asdict(rules)}
    contexts["positions"] = {"market_mode": rules.market_mode}
    backup = _backup(runtime, "settings", documents, previous_contexts)
    _save(runtime, proposed, contexts, backup)
    runtime.assets, runtime.rules, runtime.refresh = assets, rules, refresh
    runtime.low_interval = 300000  # NN stop monitoring uses fixed 5M candles, independently of SMC.
    runtime.engine, runtime.gex = engine, NaiveGEX(assets, provider=runtime.gex.provider)
    runtime.positions.market_mode = rules.market_mode
    runtime.paused = False
    return _finish(runtime, "Neural settings applied. Existing trades retain their recorded stops and costs.", backup)


def save_model_selection(runtime, selection):
    """Save the next-start selection, never switch a running ledger underneath workers."""
    from .ledger import atomic_json
    if selection != "neural_network":
        raise DataError("Paper trading uses NN only; SMC is independent market analysis.")
    if runtime.settings_path is None or not runtime.settings_path.is_file():
        raise DataError("Model selection needs an existing settings file")
    with runtime.lock:
        original = runtime.settings_path.read_bytes()
        data = json.loads(original.decode("utf-8-sig"))
        load_settings(runtime.settings_path)
        data.setdefault("strategy", {})["strategy_model"] = selection
        Rules(**data["strategy"]).validate()
        backup = backup_runtime(runtime, action="model-selection")
        atomic_json(runtime.settings_path, data)
        return {"message": "Saved NN-only paper trading. Apply saved settings or restart the dashboard.", "backup": str(backup)}


def _apply_legacy(runtime, assets, rules, refresh):
    from .engine import Engine
    from .ledger import fingerprint
    if assets != runtime.assets:
        raise DataError("Legacy pair changes require a separate study; existing paper history is preserved.")
    documents, contexts = _documents(runtime)
    proposed = copy.deepcopy(documents)
    paper = proposed["legacy"]
    paper["fingerprint"] = fingerprint(assets, rules)
    for record in paper["assets"].values():
        record["pending"] = None
        record["momentum_watch"] = None
    for document in (paper, proposed["positions"]):
        for event in document["outbox"]:
            if event["status"] == "queued":
                event.update(status="cancelled", error="Settings applied; waiting for fresh evidence.")
    paper.pop("monitoring_paused", None)
    if runtime.paused:
        proposed["legacy"] = new_state(assets, rules)
    validate_state(proposed["legacy"], assets, rules)
    previous_contexts = copy.deepcopy(contexts)
    contexts["legacy"] = {"assets": assets, "rules": asdict(rules)}
    backup = _backup(runtime, "settings", documents, previous_contexts)
    _save(runtime, proposed, contexts, backup)
    runtime.rules, runtime.refresh = rules, refresh
    runtime.engine = Engine(assets, rules, runtime.store)
    runtime.positions.market_mode = rules.market_mode
    runtime.paused = False
    return _finish(runtime, "Settings applied. Existing paper trades retain their recorded levels and costs.", backup)
