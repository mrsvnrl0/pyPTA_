"""Timely, durable Telegram warnings shared by paper trades and recorded holdings."""
from __future__ import annotations

from .core import DataError, finite, utc
from .ledger import queue_event


def fresh_quote(quote, now):
    try:
        bid, ask = finite(quote["bid"], "bid", 1e-15), finite(quote["ask"], "ask", 1e-15)
        stamp = finite(quote["asof_ms"], "quote time", 0)
        if bid > ask or not -2000 <= now-stamp <= 30000:
            return None
        return {"bid": bid, "ask": ask, "asof_ms": stamp}
    except (DataError, KeyError, TypeError):
        return None


def cancel_near_alert(document, event_id, reason):
    for event in document["outbox"]:
        if event["id"] == event_id and event["status"] == "queued":
            event.update(status="cancelled", error=reason)


def near_take_profit(document, event_id, asset, side, target, quote, now,
                     threshold_bps, scope, reference=None, position_ids=None, position=None):
    """One delivery per target; an undelivered expired warning can be refreshed."""
    quote = fresh_quote(quote, now)
    mark = quote["bid" if side == "long" else "ask"] if quote else None
    distance = ((target-mark) if side == "long" else (mark-target)) if quote else None
    trigger = target*(1+(-1 if side == "long" else 1)*threshold_bps/10000)
    inside = quote and (mark >= trigger if side == "long" else mark <= trigger)
    if threshold_bps <= 0 or distance is None or distance <= 0 or not inside:
        cancel_near_alert(document, event_id, "Take-profit warning cancelled: no fresh price inside the alert range.")
        return
    event = next((e for e in document["outbox"] if e["id"] == event_id), None)
    if event and (event["status"] not in {"queued", "cancelled"} or event.get("retired")):
        return
    distance_percent = distance/target*100
    quote_side = "bid" if side == "long" else "ask"
    from .position_alerts import exit_action, identity, price
    action = exit_action(side)
    title = f"{asset}{' #' + position['id'][:8] if position else ''} · TAKE-PROFIT APPROACHING · PREPARE TO {action}"
    lines = [title,
             f"Planned exit action: {action}", f"Target exit price: {price(target)}",
             "Target not reached yet; this is an advance warning.",
             f"{'Recorded holding' if scope == 'holding' else 'Paper trade'} · live {quote_side} ${mark:,.8f}",
             f"Take-profit ${target:,.8f}; ${distance:,.8f} remaining ({distance_percent:.4f}%)",
             f"Alert range: within {threshold_bps/100:g}% of take-profit"]
    if position:
        lines.insert(1, identity(position))
    if reference is not None:
        lines.append(f"Key liquidity {'high' if side == 'long' else 'low'} ${reference:,.8f}")
    lines.extend([f"Quote observed: {utc(quote['asof_ms'])}",
                  "Holding remains open; record your actual exit in the dashboard." if scope == "holding"
                  else "Paper tracker only; no exchange order."])
    payload = {"asset": asset, "side": side, "target": target, "mark": mark,
               "distance_percent": distance_percent, "threshold_bps": threshold_bps,
               "quote_ms": quote["asof_ms"], "liquidity_price": reference, "trigger_price": trigger}
    payload.update(action="prepare_sell" if side == "long" else "prepare_buy", action_label=action,
                   exit_price=target, price_kind="planned_target", current_exit_quote=mark,
                   position_id=position["id"] if position else None,
                   entry_price=position["entry"] if position else None,
                   quantity=position["quantity"] if position else None)
    if event is None:
        queue_event(document, event_id, "telegram", "\n".join(lines), now, payload)
        event = document["outbox"][-1]
    elif event["status"] == "cancelled":
        # Retain an acknowledged Telegram rate-limit deadline across excursions.
        event.update(status="queued", created_ms=now)
    event.update(text="\n".join(lines), payload=payload, error=None,
                 observed_ms=now,
                 title=title, action_label="PREPARE TO " + action,
                 summary=f"PREPARE TO {action} · Target exit {price(target)} · Current {quote_side} {price(mark)} · Target not reached",
                 expires_ms=int(quote["asof_ms"]+30000), alert_type="near_take_profit",
                 asset=asset, side=side, scope=scope)
    if position_ids is not None:
        event["position_ids"] = list(position_ids)
