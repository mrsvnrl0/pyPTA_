"""Consistent, position-specific wording for deterministic holding alerts."""
from .core import DataError, finite, utc
from .ledger import queue_event
from .take_profit_alerts import fresh_quote
from .position_options import alerts_enabled, monitored_position


def exit_action(side):
    return "SELL TO EXIT LONG" if side == "long" else "BUY TO COVER SHORT"


def entry_action(side):
    return "BUY TO OPEN LONG" if side == "long" else "SELL TO OPEN SHORT"


def target_event_id(position, kind):
    revision = ":"+position["alert_revision"] if position.get("alert_revision") else ""
    return f"holding:{position['id']}{revision}:{kind}"


def price(value):
    if 0 < abs(value) < 1e-8:
        return f"${value:.8g}"
    text = f"{value:,.8f}".rstrip("0")
    whole, _, fraction = text.partition(".")
    return "$" + whole + "." + fraction.ljust(2, "0")


def identity(position):
    return (f"Position #{position['id'][:8]} · {position['symbol']} · {position['side'].upper()}\n"
            f"Quantity: {position['quantity']:.8g} {position['asset']}\n"
            f"Recorded entry price: {price(position['entry'])}")


def momentum_alert(document, position, reading, previous, quote, now, event_id, rules_key=None):
    if not alerts_enabled(position, "position_momentum") or position.get("target_mode") == "nn":
        return
    event = next((e for e in document["outbox"] if e["id"] == event_id), None)
    if event and (event["status"] not in {"queued", "cancelled"} or event.get("retired")):
        return
    against = reading["direction"] == ("bearish" if position["side"] == "long" else "bullish")
    action = exit_action(position["side"]) if against else f"HOLD {position['side'].upper()}"
    # Earlier transitions remain in the audit trail; never pair them with today's quote.
    current = reading["bar_ms"] == (now // reading["interval_ms"] - 1) * reading["interval_ms"]
    quote = fresh_quote(quote, now) if current else None
    quote_side = "bid" if position["side"] == "long" else "ask"
    mark = quote[quote_side] if quote else reading["close"]
    target = position.get("take_profit")
    if not against and quote and target and (
            mark >= target["price"] if position["side"] == "long" else mark <= target["price"]):
        if event:
            event.update(status="cancelled", retired=True,
                         error="Take-profit is reached; a supporting momentum reading cannot issue HOLD.")
        return
    title = f"{position['asset']} #{position['id'][:8]} · {'EXIT SIGNAL' if against else 'POSITION UPDATE'} · {action}"
    lines = [title, identity(position)]
    if against:
        lines.append(f"{'Current exit quote (' + quote_side + ')' if quote else 'Signal exit reference (completed candle)'}: {price(mark)}")
    else:
        lines.append(f"No exit signal for this position. {'Current ' + quote_side if quote else 'Completed candle close'}: {price(mark)}")
    if not quote:
        lines.append("Reference price only; a fresh executable quote is unavailable for this signal.")
    interval = reading["interval_ms"] // 60000
    lines.append(f"Reason: {interval}M momentum turned {reading['direction'].upper()} (from {previous.upper()})"
                 + (" against your position." if against else " in your position's direction."))
    if reading.get("basis") == "structure":
        lines.append(f"Confirmed structure break at {price(reading['break_level'])}")
    try:
        sign = 1 if position["side"] == "long" else -1
        pnl = finite(sign * (mark - position["entry"]) * position["quantity"])
        percent = finite(sign * (mark - position["entry"]) / position["entry"] * 100)
        lines.append(f"Estimated gross P/L at this price: ${pnl:+,.2f} ({percent:+.2f}%)")
    except DataError:
        lines.append("Gross P/L unavailable: recorded values exceed the calculation range")
    lines.append(f"Signal candle closed: {utc(reading['end_ms'])}")
    if quote:
        lines.append(f"Quote observed: {utc(quote['asof_ms'])}")
    lines.append("Holding remains open; record your actual exit in the dashboard. No exchange order is submitted.")
    payload = {**reading, "position_id": position["id"], "entry_price": position["entry"],
               "quantity": position["quantity"], "side": position["side"],
               "action": ("sell" if position["side"] == "long" else "buy") if against else "hold",
               "action_label": action, "exit_price": mark if against else None,
               "mark_price": mark, "price_kind": "live_" + quote_side if quote else "candle_reference",
               "price_ms": quote["asof_ms"] if quote else reading["end_ms"]}
    # A newer transition for this position supersedes any undelivered older one.
    for old in document["outbox"]:
        if (old["id"] != event_id and old.get("alert_type") == "position_momentum" and old["status"] == "queued"
                and position["id"] in old.get("position_ids", [])):
            old.update(status="cancelled", error="Superseded by a newer momentum signal for this position.")
    if event is None:
        queue_event(document, event_id, "telegram", "\n".join(lines), now, payload)
        event = document["outbox"][-1]
    candle_expiry = reading["end_ms"] + reading["interval_ms"]
    expires = min(int(quote["asof_ms"] + 30000), candle_expiry) if quote else candle_expiry
    event.update(status="queued", text="\n".join(lines), payload=payload, error=None,
                 observed_ms=now, rules_key=rules_key,
                 title=title, action_label=action, alert_type="position_momentum", scope="holding",
                 summary=f"{action} · {'Exit quote' if quote and against else 'Reference price' if against else 'Mark'} {price(mark)} · Entry {price(position['entry'])}",
                 position_ids=[position["id"]], asset=position["asset"], symbol=position["symbol"],
                 side=position["side"], direction=reading["direction"], previous_direction=previous,
                 candle_ms=reading["bar_ms"], expires_ms=expires)
    if not current:
        event.update(status="cancelled", error="Historical momentum transition; retained for reference, not sent as a current alert.")


def refresh_momentum_alerts(document, asset, symbol, watch, quote, now, market_mode="margin"):
    """Refresh an undelivered current signal without creating or replaying one."""
    reading = watch.get("reading") or {}
    positions = {p["id"]: p for p in document["positions"] if p["status"] == "open"
                 and monitored_position(p, market_mode) and alerts_enabled(p, "position_momentum") and p.get("target_mode") != "nn"}
    for event in document["outbox"]:
        if (event.get("alert_type") != "position_momentum" or event.get("asset") != asset
                or event.get("symbol") != symbol or event["status"] not in {"queued", "cancelled"}
                or event.get("retired")):
            continue
        position = positions.get(event["payload"].get("position_id"))
        valid = (not watch.get("error") and position is not None
                 and event.get("rules_key") == watch.get("rules_key")
                 and event.get("candle_ms") == reading.get("bar_ms")
                 and event.get("direction") == reading.get("direction")
                 and reading.get("direction") in {"bullish", "bearish"})
        if valid:
            momentum_alert(document, position, reading, event["previous_direction"], quote, now,
                           event["id"], watch["rules_key"])
        else:
            if event["status"] == "queued":
                event.update(status="cancelled", error=watch.get("error") or
                             "This momentum signal no longer matches the current candle and formula.")
            if not watch.get("error"):
                event["retired"] = True


def target_reached_alert(document, position, quote, now, newly_reached=False):
    """One exit alert per target; never replay targets already reached before this update."""
    if not alerts_enabled(position, "target_reached"):
        return
    event_id = target_event_id(position, "target-hit")
    event = next((e for e in document["outbox"] if e["id"] == event_id), None)
    if not newly_reached and event is None:
        return
    if event and (event["status"] not in {"queued", "cancelled"} or event.get("retired")):
        return
    quote = fresh_quote(quote, now)
    target = position["take_profit"]
    long = position["side"] == "long"
    quote_side = "bid" if long else "ask"
    mark = quote[quote_side] if quote else None
    at_target = quote and (mark >= target["price"] if long else mark <= target["price"])
    if not newly_reached and not at_target:
        if event and event["status"] == "queued":
            event.update(status="cancelled", error="Target exit alert paused: no fresh quote at or beyond the target.")
        return
    action = exit_action(position["side"])
    title = f"{position['asset']} #{position['id'][:8]} · {'TAKE-PROFIT HIT' if at_target else 'TARGET TOUCHED EARLIER'} · {action if at_target else 'REVIEW EXIT'}"
    lines = [title, identity(position),
             f"Exit action: {action}" if at_target else f"Exit side when you choose to close: {action}",
             f"Target exit price: {price(target['price'])}"]
    if quote:
        lines.extend([f"Current exit quote ({quote_side}): {price(mark)}", f"Quote observed: {utc(quote['asof_ms'])}"])
    else:
        lines.append("Fresh exit quote unavailable; the target is a reference price, not a current fill.")
    if not at_target:
        lines.append("The completed candle touched the target earlier. Verify the current price before exiting.")
    lines.extend([f"Target first observed reached: {utc(target['reached_ms'])}",
                  "Holding remains open; record your actual exit in the dashboard. No exchange order is submitted."])
    payload = {"position_id": position["id"], "asset": position["asset"], "side": position["side"],
               "quantity": position["quantity"], "entry_price": position["entry"],
               "action": ("sell" if long else "buy") if at_target else "review",
               "action_label": action if at_target else "REVIEW EXIT", "planned_exit_action": action,
               "target": target["price"], "exit_price": mark if quote else target["price"],
               "price_kind": "live_" + quote_side if quote else "target_reference",
               "quote_ms": quote["asof_ms"] if quote else None}
    if event is None:
        queue_event(document, event_id, "telegram", "\n".join(lines), now, payload)
        event = document["outbox"][-1]
    event.update(status="queued", observed_ms=now, title=title, action_label=action if at_target else "REVIEW EXIT", text="\n".join(lines), payload=payload,
                 summary=f"{action if at_target else 'REVIEW EXIT'} · {'Exit quote' if quote else 'Target reference'} {price(mark if quote else target['price'])} · Target {price(target['price'])} · Entry {price(position['entry'])}",
                 alert_type="target_reached", scope="holding", position_ids=[position["id"]],
                 asset=position["asset"], symbol=position["symbol"], side=position["side"], error=None,
                 expires_ms=int(quote["asof_ms"]+30000) if at_target else target["reached_ms"]+30000)
