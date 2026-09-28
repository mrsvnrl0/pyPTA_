"""Bearish price watches for future spot purchases, independent of owned holdings."""
from __future__ import annotations

import copy
import re
import uuid
from math import isclose

from .core import DataError, finite, normalise_pair, utc, validate_candles
from .ledger import queue_event
from .smc import structure_readings
from .gex_targets import select_target, validate_selection, validate_target_mode, rules_for_target
from .take_profit_alerts import fresh_quote


def validate_buy_watches(document):
    watches = document.get("buy_watches", [])
    if not isinstance(watches, list):
        raise DataError("Invalid buy watch section")
    ids, requests = set(), set()
    for watch in watches:
        if not isinstance(watch, dict):
            raise DataError("Invalid buy watch")
        if "target_mode" in watch:
            validate_target_mode(watch["target_mode"])
        for key, seen in (("id", ids), ("request_id", requests)):
            value = watch.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value) or value in seen:
                raise DataError("Invalid buy watch identity")
            seen.add(value)
        if (not isinstance(watch.get("asset"), str) or not watch["asset"].isalnum()
                or normalise_pair(watch["symbol"]) != watch["symbol"] or not watch["symbol"].endswith("/USD")
                or watch.get("status") not in {"watching", "reached", "missed", "cancelled"}):
            raise DataError("Invalid buy watch type")
        reference = finite(watch.get("reference_price"), "watch reference", 1e-15)
        finite(watch.get("quantity"), "planned quantity", 1e-15)
        finite(reference*watch["quantity"], "planned value", 1e-15)
        for key in ("buy_price", "requested_buy_price"):
            if watch.get(key) is not None and not 0 < finite(watch[key], key) < reference:
                raise DataError("Buy level must be below the watch reference price")
        if watch.get("target_source") not in {"manual", "sweep", None} or type(watch.get("armed")) is not bool:
            raise DataError("Invalid buy watch target state")
        if watch.get("target_source") == "manual" and watch["buy_price"] != watch["requested_buy_price"]:
            raise DataError("Manual buy level does not match the saved request")
        if watch.get("target_source") == "sweep":
            level = finite(watch.get("liquidity_price"), "buy liquidity low", 1e-15)
            buffer = finite(watch.get("sweep_buffer_bps"), "buy sweep buffer", 1e-15)
            validate_selection(watch.get("target_selection"), level, "short")
            if buffer > 100 or not isclose(watch["buy_price"], level*(1-buffer/10000), rel_tol=1e-12):
                raise DataError("Buy level does not match its saved liquidity sweep")
        if type(watch.get("created_ms")) is not int or watch["created_ms"] < 0:
            raise DataError("Invalid buy watch timestamp")
        if watch["status"] == "reached" and (watch.get("buy_price") is None or
                type(watch.get("reached_ms")) is not int or watch["reached_ms"] < watch["created_ms"]):
            raise DataError("Invalid buy watch reaching time")
        for key in ("last_bar_ms", "selected_ms"):
            if watch.get(key) is not None and (type(watch[key]) is not int or watch[key] < 0):
                raise DataError("Invalid buy watch progress")
        if watch.get("direction", "neutral") not in {"neutral", "bullish", "bearish"}:
            raise DataError("Invalid buy watch momentum")
    return ids


def create_buy_watch(document, assets, asset, reference, buy_price, quantity, request_id, now, target_mode="smc"):
    validate_target_mode(target_mode)
    if asset not in assets:
        raise DataError("Choose an enabled asset")
    reference = finite(reference, "reference price", 1e-15)
    quantity = finite(quantity, "planned quantity", 1e-15)
    finite(reference*quantity, "planned value", 1e-15)
    buy_price = None if buy_price in (None, "") else finite(buy_price, "buy level", 1e-15)
    if buy_price is not None and buy_price >= reference:
        raise DataError("Buy level must be below the watch reference price")
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise DataError("Invalid form identity; reload the dashboard")
    values = {"asset": asset, "symbol": assets[asset]["symbol"], "reference_price": reference,
              "requested_buy_price": buy_price, "quantity": quantity, "target_mode": target_mode}
    watches = document.setdefault("buy_watches", [])
    existing = next((w for w in watches if w["request_id"] == request_id), None)
    if existing:
        if any(existing.get(k, "smc" if k == "target_mode" else None) != v for k, v in values.items()):
            raise DataError("This watch form was already submitted with different values")
        return existing, False
    watch = {**values, "id": uuid.uuid4().hex, "request_id": request_id, "created_ms": now, "status": "watching",
             "buy_price": buy_price, "target_source": "manual" if buy_price is not None else None,
             "armed": False, "direction": "neutral", "last_bar_ms": None, "rules_key": None,
             "error": "Waiting for a fresh price above the buy level."}
    watches.append(watch)
    return watch, True


def cancel_buy_watch(document, watch_id):
    watch = next((w for w in document.get("buy_watches", []) if w["id"] == watch_id), None)
    if watch is None:
        raise KeyError("Buy watch not found")
    watch.update(status="cancelled", error="Buy watch cancelled.")
    for event in document["outbox"]:
        if watch_id in event.get("buy_watch_ids", []) and event["status"] == "queued":
            event.update(status="cancelled", retired=True, expires_ms=0, error=watch["error"])
    return watch


def cancel_alert(document, event_id, reason):
    for event in document["outbox"]:
        if event["id"] == event_id and event["status"] == "queued":
            event.update(status="cancelled", error=reason)


def watch_alert(document, watch, kind, quote, now, reading=None):
    identity = f"buy-watch:{watch['id']}:"+("buy" if kind == "buy" else f"bearish:{reading['bar_ms']}")
    existing = next((e for e in document["outbox"] if e["id"] == identity), None)
    if existing and (existing["status"] not in {"queued", "cancelled"} or existing.get("retired")):
        return
    buying = kind == "buy"
    action = "BUY SPOT · BUY LEVEL REACHED" if buying else "BEARISH MOMENTUM · WAIT TO BUY"
    title = f"{watch['asset']} #{watch['id'][:8]} · {action}"
    target = f"${watch['buy_price']:,.8f}" if watch["buy_price"] is not None else "Waiting for an untaken liquidity low"
    lines = [title, f"SHORT buy watch · {watch['symbol']} · no asset purchase recorded",
             f"Watch reference: ${watch['reference_price']:,.8f}", f"Spot buy level: {target}",
             f"Planned quantity: {watch['quantity']:.8g} {watch['asset']}"]
    if quote:
        lines.extend([f"Current buy quote (ask): ${quote['ask']:,.8f}", f"Quote observed: {utc(quote['asof_ms'])}"])
    if buying:
        lines.append("The observed ask is at or below your buy level. Record a Spot buy holding only after your actual purchase. No exchange order is submitted.")
    else:
        lines.extend([f"Completed {reading['interval_ms']//60000}M structure is bearish; wait for the lower buy level.",
                      f"Signal candle closed: {utc(reading['end_ms'])}"])
    expires = int(quote["asof_ms"]+30000) if quote else reading["end_ms"]+reading["interval_ms"]
    if not buying:
        expires = min(expires, reading["end_ms"]+reading["interval_ms"])
    payload = {"buy_watch_id": watch["id"], "action": "buy" if buying else "wait",
               "side": "long", "source_side": "short", "reference_price": watch["reference_price"],
               "buy_price": watch["buy_price"], "quantity": watch["quantity"],
               "price_kind": "live_ask" if quote else "candle_reference",
               "buy_quote": quote["ask"] if quote else None}
    if existing is None:
        queue_event(document, identity, "telegram", "\n".join(lines), now, payload)
        existing = document["outbox"][-1]
    existing.update(status="queued", text="\n".join(lines), payload=payload, error=None,
                    title=title, action_label=action, alert_type="buy_watch_hit" if buying else "buy_watch_momentum",
                    scope="buy_watch", asset=watch["asset"], side="long", direction=watch["direction"],
                    position_ids=[], buy_watch_ids=[watch["id"]], observed_ms=now, expires_ms=expires,
                    candle_ms=reading["bar_ms"] if reading else None,
                    summary=f"{action} · Buy level {target} · Reference ${watch['reference_price']:,.8f}")


def monitor_buy_watches(document, asset, symbol, high, low, quote, rules, now, clock_error=None, gex_context=None):
    watches = [w for w in document.get("buy_watches", []) if w["asset"] == asset and w["symbol"] == symbol
               and w["status"] in {"watching", "reached"}]
    if not watches:
        return
    quote = fresh_quote(quote, now) if not clock_error else None
    validated = []
    for bars, interval in ((high, rules.smc_setup_minutes*60000), (low, rules.smc_entry_minutes*60000)):
        try:
            bars = validate_candles(bars, interval, now, 1, fresh=False) if not clock_error else []
        except (ValueError, TypeError):
            bars = []
        validated.append(bars if bars and bars[-1].t == (now//interval-1)*interval else [])
    high, low = validated
    readings = structure_readings(low, rules.smc_pivot_strength) if low and rules.strategy_model == "smc_video" else []
    rules_key = f"buy-watch-structure:{rules.strategy_model}:{rules.smc_entry_minutes}:{rules.smc_pivot_strength}"
    for watch in watches:
        event_id = f"buy-watch:{watch['id']}:buy"
        if rules.market_mode != "spot" or rules.strategy_model != "smc_video":
            watch["error"] = "Buy watch paused: requires the SMC strategy in spot mode."
            for event in document["outbox"]:
                if watch["id"] in event.get("buy_watch_ids", []):
                    cancel_alert(document, event["id"], watch["error"])
            continue
        current_quote = quote if quote and quote["asof_ms"] >= watch["created_ms"] else None
        if watch["buy_price"] is None and current_quote and high and low:
            buy_rules = rules_for_target(rules, watch.get("target_mode", "smc"))
            point = select_target(high, low, buy_rules, "short",
                                  min(watch["reference_price"], current_quote["ask"]), now, gex_context)
            if point:
                watch.update(buy_price=point["price"]*(1-rules.smc_tp_sweep_buffer_bps/10000),
                             target_source="sweep", liquidity_price=point["price"], pivot_ms=point["bar_ms"],
                             sweep_buffer_bps=rules.smc_tp_sweep_buffer_bps, selected_ms=now,
                             target_selection=point["target_selection"])
        watch["error"] = ("Waiting for a fresh ask quote." if not current_quote else
                          "Waiting for current candles and an untaken liquidity low below the reference and price." if watch["buy_price"] is None else None)
        # Momentum is informational: a later BUY needs the price touch, not a bullish reversal.
        reading = copy.deepcopy(readings[-1]) if readings else None
        if reading:
            previous = watch["direction"]
            changed_rules = watch["rules_key"] not in {None, rules_key}
            gap = watch["last_bar_ms"] is not None and readings[0]["bar_ms"] > watch["last_bar_ms"]+reading["interval_ms"]
            if not changed_rules and not gap and watch["last_bar_ms"] is not None:
                newer = [r for r in readings if r["bar_ms"] > watch["last_bar_ms"] and r.get("event")]
                if newer:
                    reading["direction"] = newer[-1]["direction"]
                else:
                    reading["direction"] = previous
            first = watch["last_bar_ms"] is None
            watch.update(direction=reading["direction"], last_bar_ms=reading["bar_ms"], rules_key=rules_key,
                         momentum_end_ms=reading["end_ms"])
            if watch["status"] == "watching" and reading["direction"] == "bearish" and not changed_rules and not gap and (first or previous != "bearish"):
                watch_alert(document, watch, "bearish", current_quote, now, reading)
        for event in document["outbox"]:
            if (watch["id"] in event.get("buy_watch_ids", []) and event["alert_type"] == "buy_watch_momentum"
                    and event["status"] in {"queued", "cancelled"} and not event.get("retired")):
                if watch["status"] == "watching" and reading and reading["direction"] == "bearish" and event["candle_ms"] == reading["bar_ms"] and not changed_rules and not gap:
                    watch_alert(document, watch, "bearish", current_quote, now, reading)
                else:
                    cancel_alert(document, event["id"], "Bearish notice no longer matches the current watch and candle.")
                    # A temporary candle outage pauses delivery, without reviving past notices.
                    if reading or watch["status"] != "watching":
                        event["retired"] = True
        if not current_quote or watch["buy_price"] is None:
            cancel_alert(document, event_id, watch["error"])
            continue
        watch.update(last_ask=current_quote["ask"], quote_ms=int(current_quote["asof_ms"]))
        if not watch["armed"]:
            if current_quote["ask"] <= watch["buy_price"]:
                watch.update(status="missed", error="Price was already at or below the buy level when first observed. Create a new watch for a future touch.")
                for event in document["outbox"]:
                    if watch["id"] in event.get("buy_watch_ids", []):
                        cancel_alert(document, event["id"], watch["error"])
                continue
            watch["armed"] = True
        if current_quote["ask"] <= watch["buy_price"]:
            if watch["status"] == "watching":
                watch.update(status="reached", reached_ms=now)
            for event in document["outbox"]:
                if watch["id"] in event.get("buy_watch_ids", []) and event["alert_type"] == "buy_watch_momentum":
                    cancel_alert(document, event["id"], "Buy level reached; the wait notice is superseded.")
                    event["retired"] = True
            watch_alert(document, watch, "buy", current_quote, now)
        else:
            cancel_alert(document, event_id, "Ask has moved above the buy level; no current BUY instruction.")
