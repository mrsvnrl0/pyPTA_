"""NN confirmation for strategy-priced paper trades; never touches manual holdings."""
import copy

from .core import DataError, H4
from .take_profit_alerts import fresh_quote


def current_signal(reading, now):
    reading = reading or {}
    signal = reading.get("signal")
    expected_end = (now//H4)*H4-1
    if (reading.get("error") or not signal or signal.get("label") not in {"BUY", "HOLD", "SELL"}
            or signal.get("signal_end") != expected_end or now >= reading.get("expires_ms", 0)):
        return None
    return signal


def entry_confirmation(side, reading, quote, now):
    signal = current_signal(reading, now)
    desired = "BUY" if side == "long" else "SELL"
    if signal is None:
        raise DataError("Strategy levels ready; waiting for the latest completed 4H NN reading")
    if signal["label"] != desired:
        raise DataError(f"Strategy levels ready; NN {signal['label']} does not confirm {desired} for this entry")
    quote = fresh_quote(quote, now)
    if not quote or quote["asof_ms"] <= signal["signal_end"]:
        raise DataError("Waiting for a fresh entry quote after the NN candle close")
    return {**copy.deepcopy(signal), "observed_ms": now, "quote_ms": quote["asof_ms"]}


def monitor_exits(document, name, reading, quote, now, base):
    """Price stops/targets run first. An adverse current NN may close the remainder."""
    signal = current_signal(reading, now)
    quote = fresh_quote(quote, now)
    if not signal or not quote or quote["asof_ms"] <= signal["signal_end"]:
        return
    for trade in document["assets"][name]["trades"]:
        side = trade.get("side", "long")
        if (trade["status"] != "active" or signal["label"] != ("SELL" if side == "long" else "BUY")
                or now <= trade["opened_ms"]):
            continue
        trade["neural_exit"] = {**copy.deepcopy(signal), "observed_ms": now, "quote_ms": quote["asof_ms"]}
        if base == "smc_video":
            from .smc_ledger import close_trade
            close_trade(document, name, trade, quote["bid" if side == "long" else "ask"], "nn_exit", now)
        else:
            from .ledger import close_quantity
            close_quantity(document, trade, trade["remaining"], quote["bid"], "NN EXIT", now)


def trade_progress(record):
    return {t["id"]: (t["status"], t.get("remaining")) for t in record["trades"]}


def decision(record, before, reading, quote, now):
    """Describe actual committed paper actions separately from the raw NN class."""
    signal = current_signal(reading, now)
    quote = fresh_quote(quote, now)
    active = next((t for t in reversed(record["trades"]) if t["status"] == "active"), None)
    order = record.get("pending")
    action, label = ("HOLD", "HOLD PAPER POSITION") if active else ("WAIT", "WAIT FOR STRATEGY ENTRY + NN")
    if not signal or not quote:
        action, label = "WAIT", "WAIT FOR CURRENT NN / QUOTE"
    events = []
    for trade in record["trades"]:
        previous = before.get(trade["id"])
        side = trade.get("side", "long")
        if previous is None:
            events.append(("BUY" if side == "long" else "SELL", "PAPER ENTRY RECORDED", trade))
        if ((previous is None or previous[0] == "active") and trade["status"] != "active"
                or previous and previous[0] == "active" and trade.get("remaining") != previous[1]):
            reason = "NN adverse signal" if trade["status"] == "nn_exit" else trade["status"] if trade["status"] != "active" else "TP1 partial exit"
            events.append(("SELL" if side == "long" else "BUY", "PAPER EXIT RECORDED · "+reason, trade))
    levels = active or order
    if events:
        action, label, levels = events[-1]
    reason = ((reading or {}).get("error") or "Waiting for the latest completed 4H NN reading") if not signal else (
        "Strategy stops and targets remain active; an adverse NN signal can exit early." if active else
        "The strategy entry level must be reached with NN confirmation and all risk checks passing.")
    return {"signal": copy.deepcopy(signal), "last_signal": copy.deepcopy((reading or {}).get("signal")),
            "action": action, "action_label": label, "reason": reason,
            "observed_ms": now, "expires_ms": min(quote["asof_ms"]+30000, now+30000) if quote else now,
            "signal_expires_ms": (reading or {}).get("expires_ms", now),
            "levels": copy.deepcopy(levels), "events": [{"action": a, "label": l, "trade_id": t["id"]} for a,l,t in events]}
