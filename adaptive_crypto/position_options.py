"""Per-holding choices, independent of the automatic paper strategy."""
from .core import DataError
import hashlib

POSITION_TYPES = {"spot": ("long", "Spot buy"), "margin_long": ("long", "Margin long"),
                  "margin_short": ("short", "Margin short")}


def validate_position_target(mode):
    if mode not in {"smc", "gex_smc", "nn"}:
        raise DataError("Choose SMC, SMC + GEX or NN for this position.")
    return mode


def target_basis(position):
    return position.get("nn_target_mode", "gex_smc") if position.get("target_mode") == "nn" else position.get("target_mode", "smc")


def alerts_enabled(position, kind):
    return position.get("momentum_alerts" if kind in {"position_momentum", "position_neural", "position_structure"} else "tp_alerts", True)


def monitored_position(position, market_mode):
    return position["side"] == "long" or position.get("position_type") == "margin_short" or market_mode == "margin"


def position_type(position, market_mode):
    return position.get("position_type", "spot" if market_mode == "spot" and position["side"] == "long" else "margin_" + position["side"])


def paper_key(namespace, trade_id):
    return hashlib.sha256(f"{namespace}:{trade_id}".encode()).hexdigest()


def paper_alert_allowed(document, event, preferences, namespace):
    if event.get("kind") != "telegram":
        return True
    trade = next((t for row in document.get("assets", {}).values() for t in row.get("trades", [])
                  if event["id"].startswith(t["id"]+":")), None)
    if trade is None:
        return True
    options = preferences.get(paper_key(namespace, trade["id"]), {})
    is_tp = event.get("alert_type") in {"near_take_profit", "target_reached"} or event["id"].endswith((":near-tp", ":TP1", ":TP2")) or (event.get("payload") or {}).get("status") == "target"
    # Stop-loss delivery is not a momentum notification.
    is_stop = event["id"].endswith(":STOP") or (event.get("payload") or {}).get("status") == "stopped"
    return True if is_stop else options.get("tp_alerts" if is_tp else "momentum_alerts", True)


def cancel_disabled_paper_alerts(document, preferences, namespace):
    for event in document["outbox"]:
        if event["status"] == "queued" and not paper_alert_allowed(document, event, preferences, namespace):
            event.update(status="cancelled", error="Alerts disabled for this paper position.")
