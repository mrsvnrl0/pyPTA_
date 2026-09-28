"""Fixed liquidity-sweep targets for holdings recorded outside the paper engine."""
from __future__ import annotations

from math import isclose

from .core import DataError, finite, validate_candles
from .gex_targets import select_target, validate_selection, rules_for_target
from .take_profit_alerts import cancel_near_alert, fresh_quote, near_take_profit
from .position_alerts import target_event_id, target_reached_alert
from .position_options import alerts_enabled, monitored_position, target_basis


def validate_holding_target(position):
    target = position.get("take_profit")
    if target is None:
        return
    if not isinstance(target, dict) or target.get("state") not in {"watching", "reached"}:
        raise DataError("Invalid holding take-profit state")
    level = finite(target.get("liquidity_price"), "holding liquidity", 1e-15)
    price = finite(target.get("price"), "holding take-profit", 1e-15)
    buffer = finite(target.get("sweep_buffer_bps"), "holding sweep buffer", 1e-15)
    validate_selection(target.get("selection"), level, position["side"])
    sign = 1 if position["side"] == "long" else -1
    if (buffer > 100 or sign*(level-position["entry"]) <= 0 or sign*(price-level) <= 0
            or not isclose(price, level*(1+sign*buffer/10000), rel_tol=1e-12)):
        raise DataError("Holding take-profit does not match its recorded liquidity sweep")
    if (type(target.get("selected_ms")) is not int or target["selected_ms"] < position["opened_ms"]
            or type(target.get("pivot_ms")) is not int or not 0 <= target["pivot_ms"] < target["selected_ms"]
            or target.get("setup_minutes") not in {15, 30}):
        raise DataError("Invalid holding take-profit chronology")
    if target["state"] == "reached":
        if type(target.get("reached_ms")) is not int or target["reached_ms"] < target["selected_ms"]:
            raise DataError("Holding target reached before it was selected")
    elif target.get("reached_ms") is not None:
        raise DataError("Unreached holding target has a reaching timestamp")


def monitor_holding_targets(document, asset, symbol, high, low, quote, rules, now, clock_error=None, gex_context=None):
    positions = [p for p in document["positions"] if p["asset"] == asset and p["symbol"] == symbol and p["status"] == "open"]
    if not positions:
        return
    quote = fresh_quote(quote, now) if not clock_error else None
    validated = []
    for candles, interval in ((high, rules.smc_setup_minutes*60000), (low, rules.smc_entry_minutes*60000)):
        try:
            bars = validate_candles(candles, interval, now, 1, fresh=False) if not clock_error else []
        except (ValueError, TypeError):
            bars = []
        current = bool(bars) and bars[-1].t == (now//interval-1)*interval
        validated.append((bars, current))
    (high, current_high), (low, current_low) = validated
    for position in positions:
        event_id = target_event_id(position, "near-tp")
        if not monitored_position(position, rules.market_mode):
            position["take_profit_error"] = "Short holding requires correction to a spot buy or explicit margin mode."
            cancel_near_alert(document, event_id, position["take_profit_error"])
            cancel_near_alert(document, target_event_id(position, "target-hit"), position["take_profit_error"])
            continue
        if rules.strategy_model != "smc_video":
            position["take_profit_error"] = "Sweep take-profit alerts are disabled."
            cancel_near_alert(document, event_id, position["take_profit_error"])
            cancel_near_alert(document, target_event_id(position, "target-hit"), position["take_profit_error"])
            continue
        long = position["side"] == "long"
        mark = quote["bid" if long else "ask"] if quote else None
        target = position.get("take_profit")
        if target is None:
            if not quote or not current_high or not current_low:
                position["take_profit_error"] = "Waiting for current setup/entry candles and a fresh quote to select take-profit."
                continue
            reference = max(position["entry"], mark) if long else min(position["entry"], mark)
            target_rules = rules_for_target(rules, target_basis(position)) if "target_mode" in position else rules
            point = select_target(high, low, target_rules, position["side"], reference, now, gex_context)
            if point is None:
                position["take_profit_error"] = "Waiting for an untaken opposing liquidity swing beyond entry and the current price."
                continue
            target = {"liquidity_price": point["price"], "pivot_ms": point["bar_ms"],
                      "price": point["price"]*(1+(1 if long else -1)*rules.smc_tp_sweep_buffer_bps/10000),
                      "sweep_buffer_bps": rules.smc_tp_sweep_buffer_bps,
                      "selected_ms": now, "setup_minutes": rules.smc_setup_minutes,
                      "state": "watching", "reached_ms": None, "selection": point["target_selection"]}
            position["take_profit"] = target
        position["take_profit_error"] = None if quote else "Waiting for a fresh quote for take-profit alerts."
        newly_reached = False
        if target["state"] == "watching":
            reached = []
            for bar in high+low:
                if bar.end <= target["selected_ms"]:
                    continue
                # A candle already forming when the target was selected may
                # establish a later close, but not the time of its earlier wick.
                price = bar.c if bar.t <= target["selected_ms"] else (bar.h if long else bar.l)
                if price >= target["price"] if long else price <= target["price"]:
                    reached.append(bar.end)
            if quote and (mark >= target["price"] if long else mark <= target["price"]):
                reached.append(now)
            if reached:
                target.update(state="reached", reached_ms=min(reached))
                newly_reached = True
        if target["state"] == "reached":
            cancel_near_alert(document, event_id, "The recorded holding's take-profit was already reached.")
            target_reached_alert(document, position, quote, now, newly_reached)
            continue
        if not alerts_enabled(position, "near_take_profit") or rules.smc_tp_alert_bps == 0:
            cancel_near_alert(document, event_id, "Take-profit proximity alerts are disabled for this position.")
            continue
        near_take_profit(document, event_id, asset, position["side"], target["price"], quote, now,
                         rules.smc_tp_alert_bps, "holding", target["liquidity_price"], [position["id"]], position)
