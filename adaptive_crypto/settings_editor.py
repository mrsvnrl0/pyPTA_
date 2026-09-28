"""Validated, versioned editing of the dashboard's complete settings document."""
from dataclasses import asdict, replace
import copy
import hashlib
import json
from pathlib import Path
import tempfile

from .core import DEFAULT_ASSETS, DataError, Rules, load_settings, load_application_settings
from .ledger import atomic_json
from .backups import backup_runtime


CHOICES = {
    "market_mode": [("spot", "Spot"), ("margin", "Margin")],
    "smc_entry_method": [("both", "Both"), ("conservative", "Conservative"), ("aggressive", "Aggressive")],
    "smc_stop_basis": [("swing", "Swing"), ("order_block", "Order block"), ("atr", "ATR")],
    "smc_setup_minutes": [(30, "30 minutes"), (15, "15 minutes")],
    "smc_entry_minutes": [(5, "5 minutes"), (1, "1 minute")],
}


def read_editor(runtime):
    path = runtime.settings_path
    if path is None:
        raise DataError("This dashboard has no settings file configured.")
    raw = path.read_bytes() if path.exists() else b"{}"
    saved = json.loads(raw.decode("utf-8-sig"))
    load_settings(path)
    document = {"assets": [dict(enabled=True, price_decimals=3) | entry
                           for entry in saved.get("assets", DEFAULT_ASSETS)],
                "refresh_seconds": saved.get("refresh_seconds", 15),
                "strategy": asdict(replace(Rules(**saved.get("strategy", {})), strategy_model="neural_network"))}
    return document, hashlib.sha256(raw).hexdigest()


def field_groups(document):
    groups = {name: [] for name in ("Paper account and risk", "NN paper simulation", "SMC market analysis")}
    defaults = asdict(Rules())
    general = {"strategy_model", "market_mode", "fee_rate", "slippage_rate", "paper_equity", "paper_floor",
               "risk_per_trade", "max_total_risk", "max_allocation", "minimum_notional", "max_spread_bps"}
    for key, value in document["strategy"].items():
        if key in {"strategy_model", "market_mode", "smc_gex_targets"}:
            continue
        if key not in general and not key.startswith(("nn_", "smc_")) and key not in {"atr_period", "stop_buffer_atr"}:
            continue
        group = "Paper account and risk" if key in general else "NN paper simulation" if key.startswith("nn_") else "SMC market analysis"
        label = key.replace("_", " ").capitalize()
        for acronym in ("nn", "smc", "atr", "rsi", "rvol", "gex", "roc", "mss", "ltf", "ob", "tp"):
            label = " ".join(word.upper() if word.lower() == acronym else word for word in label.split())
        kind = type(defaults[key])
        groups[group].append({"key": key, "label": label,
                              "value": value, "kind": "bool" if kind is bool else "int" if kind is int else "number" if kind is float else "text",
                              "choices": CHOICES.get(key)})
    return groups


def validate_document(document, directory):
    if not isinstance(document, dict) or set(document) != {"assets", "strategy", "refresh_seconds"}:
        raise DataError("Expected assets, strategy and refresh_seconds.")
    if not isinstance(document["assets"], list) or not 1 <= len(document["assets"]) <= 100:
        raise DataError("Provide between 1 and 100 currency pairs.")
    names, pairs = set(), set()
    from .core import normalise_pair
    for entry in document["assets"]:
        if not isinstance(entry, dict) or set(entry) != {"name", "symbol", "enabled", "price_decimals"}:
            raise DataError("Each currency pair needs name, symbol, enabled and price_decimals.")
        if not isinstance(entry["name"], str) or not isinstance(entry["symbol"], str):
            raise DataError("Asset names and currency pairs must be text.")
        name, pair = entry["name"].strip().upper(), normalise_pair(entry["symbol"])
        if not name.isalnum() or len(name) > 30 or name in names or pair in pairs:
            raise DataError("Asset names and currency pairs must be distinct, including disabled rows.")
        if not pair.endswith("/USD"):
            raise DataError(f"{pair}: USD quote required for the USD paper portfolio.")
        if type(entry["enabled"]) is not bool or type(entry["price_decimals"]) is not int or not 0 <= entry["price_decimals"] <= 10:
            raise DataError("Choose Enabled or Disabled and a whole number of price decimals from 0 to 10.")
        names.add(name)
        pairs.add(pair)
    # Use the same validation as startup without touching the saved file.
    with tempfile.TemporaryDirectory(prefix="settings-check-", dir=directory) as folder:
        path = Path(folder)/"settings.json"
        path.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")
        return load_settings(path)


def save_editor(runtime, document, revision):
    with runtime.lock:
        if runtime.stop.is_set():
            raise DataError("The dashboard is restarting. Wait for it to return before saving.")
        _, current_revision = read_editor(runtime)
        if revision != current_revision:
            raise DataError("Settings changed in another tab or on disk. Reload before saving.")
        if document.get("strategy", {}).get("strategy_model") != "neural_network":
            raise DataError("Paper trading uses NN only. SMC settings control market analysis.")
        _, rules, _ = validate_document(document, runtime.settings_path.parent)
        if rules.nn_model_id != "parente_mlp_v1":
            from .neural_models import load_selected_model
            load_selected_model(rules, runtime.settings_path)
        path = runtime.settings_path
        backup = backup_runtime(runtime, action="settings-edit")
        atomic_json(path, copy.deepcopy(document))
        _, revision = read_editor(runtime)
        return {"message": "Settings saved. Apply saved settings below to update the NN simulation and SMC analysis.",
                "revision": revision, "backup": str(backup)}


def check_restart(runtime):
    """Check the next model and ledger before stopping the working dashboard."""
    document, _ = read_editor(runtime)
    assets, rules, _ = validate_document(document, runtime.settings_path.parent)
    rules = replace(rules, strategy_model="neural_network")
    from .ledger import prepare_state
    from .smc_ledger import prepare_smc
    from .neural_ledger import prepare_neural
    from .positions import prepare_positions
    namespace = rules.paper_namespace
    prepare = {"legacy": prepare_state, "smc": prepare_smc, "neural": prepare_neural}[namespace]
    if runtime.store.database_path:
        documents = runtime.store._backend.database.documents()
        raw = json.dumps(documents[namespace]).encode() if namespace in documents else None
    else:
        # JSON namespaces are siblings of the active store (same naming as startup).
        from .state_paths import StatePaths
        base = getattr(runtime, "state_base", runtime.store.path)
        path = StatePaths(base).sources[namespace]
        raw = path.read_bytes() if path.exists() else None
    prepared, _ = prepare(raw, assets, rules)
    prepare_positions(json.dumps(runtime.positions.snapshot()).encode(), rules.market_mode)
    if rules.strategy_model == "neural_network" or rules.combined:
        from .neural_models import load_selected_model
        model = load_selected_model(rules, runtime.settings_path)
        if namespace == "neural":
            for name, record in prepared["assets"].items():
                if any(trade["status"] == "active" for trade in record["trades"]):
                    if name not in assets:
                        raise DataError(f"Keep {name} enabled while its neural paper trade is active")
                    asset = assets[name]["symbol"].split("/")[0]
                    if hasattr(model, "trained_through_ms_for"):
                        model.trained_through_ms_for(asset)
                    elif asset not in model.volume_stats:
                        raise DataError(f"Selected NN model has no trained artifact for {asset}")
