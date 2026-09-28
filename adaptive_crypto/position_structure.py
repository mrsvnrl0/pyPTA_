"""Position-only alerts for adverse breaks of independent 4H protected levels."""
import copy

from .core import H4, utc
from .ledger import queue_event
from .position_alerts import identity, price
from .position_options import alerts_enabled, monitored_position
from .structure_context import advance_structure


def monitor_structure(document, asset, symbol, candles, now, market_mode="spot", error=None, allow_alerts=True):
    contexts = document.setdefault("market_structure", {})
    previous = contexts.get(asset, {})
    if previous.get("symbol") != symbol:
        previous = {}
    state, reset = advance_structure(candles, now, previous, error)
    state["symbol"] = symbol
    contexts[asset] = state
    # Retire the old generic momentum notifications, preserving their history.
    for event in document["outbox"]:
        if event.get("asset") != asset or event.get("symbol") != symbol:
            continue
        if event.get("alert_type") == "position_momentum":
            event.update(retired=True, expires_ms=0)
            if event["status"] == "queued":
                event.update(status="cancelled", error="Replaced by position-specific 4H protected-level alerts.")
        if (event.get("alert_type") == "position_structure" and event["status"] == "queued"
                and (state.get("error") or event["expires_ms"] <= now)):
            event.update(status="cancelled", error=state.get("error") or "Structural alert delivery window expired.")
    event = state.get("last_breach")
    fresh_event = (not state.get("error") and not reset and allow_alerts and event
                   and event["bar_ms"] == state["bar_ms"] and previous.get("bar_ms") != state["bar_ms"])
    if fresh_event:
        for position in document["positions"]:
            started = max(position["opened_ms"], position.get("momentum_enabled_ms", 0),
                          position.get("spot_correction", {}).get("corrected_ms", 0))
            adverse = "protected_low" if position["side"] == "long" else "protected_high"
            if (position["asset"] == asset and position["symbol"] == symbol and position["status"] == "open"
                    and monitored_position(position, market_mode) and alerts_enabled(position, "position_structure")
                    and started < event["end_ms"] and event["kind"] == adverse):
                queue_structure_alert(document, position, event, now)
    return copy.deepcopy(state)


def queue_structure_alert(document, position, breach, now):
    event_id = f"holding:{position['id']}:4h-structure:{breach['protected_ms']}:{breach['bar_ms']}:{breach['kind']}"
    if any(event["id"] == event_id for event in document["outbox"]):
        return
    level = breach["kind"].replace("_", " ")
    action = "REVIEW LONG EXIT" if position["side"] == "long" else "REVIEW SHORT EXIT"
    title = f"{position['asset']} #{position['id'][:8]} · 4H {level.upper()} BROKEN"
    text = "\n".join([
        title, identity(position), f"{action}: confirmed structure break against your position.",
        f"Protected level: {price(breach['level'])} · established {utc(breach['protected_ms'])}",
        f"Completed 4H close: {price(breach['close'])} · {utc(breach['end_ms'])}",
        "The candle closed " + ("below" if position["side"] == "long" else "above") + " the protected level. A wick alone does not trigger this alert.",
        "This is a completed-candle reference, not a live exit quote. Check the current price before acting.",
        "Your recorded position remains open until you record its actual exit. No exchange order is submitted."])
    payload = {**breach, "position_id": position["id"], "entry_price": position["entry"],
               "quantity": position["quantity"], "side": position["side"], "action": "review",
               "action_label": action, "price_kind": "candle_reference", "interval_ms": H4}
    queue_event(document, event_id, "telegram", text, now, payload)
    document["outbox"][-1].update(
        alert_type="position_structure", scope="holding", title=title, action_label=action,
        summary=f"4H {level} {price(breach['level'])} broken · Close {price(breach['close'])}",
        position_ids=[position["id"]], asset=position["asset"], symbol=position["symbol"],
        candle_ms=breach["bar_ms"], observed_ms=now,
        expires_ms=min(now+300000, breach["end_ms"]+H4+1))
