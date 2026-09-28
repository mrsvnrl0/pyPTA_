"""Read-only presentation of all saved paper model namespaces."""
import copy
import json
from pathlib import Path
from .state_paths import StatePaths
from .position_options import paper_key


def paper_records(runtime):
    active = runtime.rules.paper_namespace
    errors = []
    preferences = runtime.positions.snapshot().get("paper_alert_preferences", {})
    if runtime.store.database_path:
        documents = runtime.store._backend.database.documents()
    else:
        base = getattr(runtime, "state_base", None)
        if base is None:
            text = str(runtime.store.path)
            base = text[:-len('.'+active+'.json')]+'.json' if active != 'legacy' and text.endswith('.'+active+'.json') else text
        documents = {}
        for name, path in StatePaths(base).sources.items():
            if name == 'positions' or not path.exists():
                continue
            try:
                documents[name] = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                errors.append(f"Could not read {name} paper history.")
    documents[active] = runtime.store.snapshot()
    trades = []
    labels = {"legacy": "Legacy", "smc": "SMC", "neural": "NN"}
    for name, document in documents.items():
        if name not in labels:
            continue
        for asset, record in document.get("assets", {}).items():
            for saved in record.get("trades", []):
                trade = copy.deepcopy(saved)
                trade.update(asset=asset, model=labels[name]+(" + NN" if saved.get("neural_entry") else ""), model_active=name == active,
                             symbol=saved.get("symbol") or record.get("symbol") or runtime.assets.get(asset, {}).get("symbol", asset+'/USD'),
                             side=saved.get("side", "long"), paper_key=paper_key(name,saved['id']))
                trade.update(preferences.get(trade["paper_key"], {}))
                trades.append(trade)
    trades.sort(key=lambda t: t.get("closed_ms", t.get("opened_ms", 0)), reverse=True)
    return {"active": [t for t in trades if t["status"] == "active"],
            "closed": [t for t in trades if t["status"] != "active"], "errors": errors}
