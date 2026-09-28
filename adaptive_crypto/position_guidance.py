"""Three independent exit guides for manually recorded positions."""
import copy
from dataclasses import replace

from .core import DataError, H4, atr_series, finite, validate_candles
from .gex_targets import select_target, rules_for_target
from .holding_targets import monitor_holding_targets
from .ledger import queue_event
from .position_alerts import exit_action, identity, price
from .position_options import monitored_position, alerts_enabled
from .smc import swings, higher_setups
from .take_profit_alerts import fresh_quote


def _current_nn_signal(reading, now):
    reading = reading or {}
    signal = reading.get("signal")
    return bool(signal and not reading.get("error") and signal.get("signal_end") == now//H4*H4-1
                and now < reading.get("expires_ms", 0))


def nn_advice(position, reading, quote, now):
    reading = reading or {}
    signal = reading.get("signal")
    valid = _current_nn_signal(reading, now)
    quote = fresh_quote(quote, now)
    long = position["side"] == "long"
    mark = quote["bid" if long else "ask"] if quote else None
    against = valid and signal["label"] == ("SELL" if long else "BUY")
    light = "WAIT"
    reason = reading.get("error") or "Waiting for the latest completed NN candle and a fresh exit quote."
    if quote and valid:
        profitable = (mark-position["entry"])*(1 if long else -1) > 0
        light = ("TAKE PROFIT" if profitable else "STOP LOSS") if against else "HOLD"
        reason = ("Adverse NN prediction: exit while above your entry." if profitable else
                  "Adverse NN prediction: protective exit at or below your entry.") if against and long else (
                  "Adverse NN prediction: cover this short." if against else "NN does not oppose this position.")
    action = exit_action(position["side"]) if light in {"STOP LOSS", "TAKE PROFIT"} else light
    return {"light": light, "action_label": light+" · "+action if action != light else light,
            "signal": signal["label"] if signal else None, "signal_end": signal.get("signal_end") if signal else None,
            "probabilities": copy.deepcopy(signal.get("probabilities", {})) if signal else {},
            "reason": reason, "mark": mark, "observed_ms": now,
            "expires_ms": min(quote["asof_ms"]+30000, reading.get("expires_ms", now)) if quote and valid else now}


def target_advice(position, target, quote, now):
    quote = fresh_quote(quote, now)
    long = position["side"] == "long"
    mark = quote["bid" if long else "ask"] if quote else None
    stop = position.get("stop_price")
    stopped = mark is not None and stop is not None and (mark <= stop if long else mark >= stop)
    hit = mark is not None and target is not None and (mark >= target["price"] if long else mark <= target["price"])
    light = "STOP LOSS" if stopped else "TAKE PROFIT" if hit else "HOLD" if quote and target else "WAIT"
    return {"light": light, "action_label": light+" · "+exit_action(position["side"]) if stopped or hit else light,
            "mark": mark, "observed_ms": now, "expires_ms": quote["asof_ms"]+30000 if quote else now}


def suggested_stop(position, high, low, rules):
    long = position["side"] == "long"
    if rules.smc_stop_basis == "atr":
        values = atr_series(low, rules.atr_period)
        distance = values[-1]*rules.stop_buffer_atr if values and values[-1] else None
        level = position["entry"] + (-distance if long else distance) if distance else None
    else:
        if rules.smc_stop_basis == "order_block":
            levels = [s["zone_low" if long else "zone_high"] for s in higher_setups(high, rules, position["side"])]
        else:
            levels = [p["price"] for p in swings(low, rules.smc_pivot_strength) if p["high"] != long]
        levels = [v for v in levels if v < position["entry"]] if long else [v for v in levels if v > position["entry"]]
        basis = levels[-1] if levels else None
        level = basis*(1+(-1 if long else 1)*rules.smc_stop_buffer_bps/10000) if basis else None
    return level if level and level > 0 else None


def _target(point, rules, side, now):
    return {"liquidity_price": point["price"], "pivot_ms": point["bar_ms"],
            "price": point["price"]*(1+(1 if side == "long" else -1)*rules.smc_tp_sweep_buffer_bps/10000),
            "sweep_buffer_bps": rules.smc_tp_sweep_buffer_bps, "selected_ms": now,
            "setup_minutes": rules.smc_setup_minutes, "state": "watching", "reached_ms": None,
            "selection": point["target_selection"]}


def _alert(document, position, kind, key, advice, now):
    if not alerts_enabled(position, kind):
        return
    event_id = f"holding:{position['id']}:{key}"
    event = next((e for e in document["outbox"] if e["id"] == event_id), None)
    if event and (event["status"] not in {"queued", "cancelled"} or event.get("retired")):
        return
    title = f"{position['asset']} #{position['id'][:8]} · {advice['action_label']}"
    text = f"{title}\n{identity(position)}\nExit quote: {price(advice['mark'])}\nRecord your actual exit; the holding remains open."
    if event is None:
        queue_event(document, event_id, "telegram", text, now, advice)
        event = document["outbox"][-1]
    event.update(status="queued", text=text, payload=copy.deepcopy(advice), alert_type=kind,
                 position_ids=[position["id"]], asset=position["asset"], symbol=position["symbol"],
                 title=title, summary=title, action_label=advice["action_label"],
                 observed_ms=now, expires_ms=advice["expires_ms"], error=None)


def monitor_position_guidance(document, asset, symbol, high, low, quote, reading, rules, confluence, now):
    rules = replace(rules, strategy_model="smc_video")
    quote = fresh_quote(quote, now)
    valid_history = True
    for bars, minutes in ((high, rules.smc_setup_minutes), (low, rules.smc_entry_minutes)):
        try:
            validate_candles(bars, minutes*60000, now, 1)
            if bars[-1].t != (now//(minutes*60000)-1)*minutes*60000:
                valid_history = False
        except (DataError, TypeError, AttributeError):
            valid_history = False
    for position in document["positions"]:
        if (position["status"] != "open" or position["asset"] != asset or position["symbol"] != symbol
                or not monitored_position(position, rules.market_mode)):
            continue
        long = position["side"] == "long"
        mark = quote["bid" if long else "ask"] if quote else None
        if "position_targets" not in position:
            old = position.get("take_profit")
            old_method = (old.get("selection") or {}).get("method", position.get("target_mode", "smc")) if old else None
            position["position_targets"] = {"smc": copy.deepcopy(old) if old_method == "smc" else None,
                                             "gex_smc": copy.deepcopy(old) if old_method == "gex_smc" else None}
            for event in document["outbox"]:
                if position["id"] in event.get("position_ids", []) and event["status"] == "queued" and event.get("alert_type") in {"position_neural", "near_take_profit", "target_reached"}:
                    event.update(status="cancelled", retired=True, error="Replaced by independent NN / TP1 / TP2 guidance.")
        targets = position["position_targets"]
        problems = {}
        if valid_history:
            position["suggested_stop"] = suggested_stop(position, high, low, rules)
        for method in ("smc", "gex_smc"):
            if targets.get(method) is None and quote and valid_history:
                reference = max(position["entry"], mark) if long else min(position["entry"], mark)
                if method == "gex_smc" and targets.get("smc"):
                    reference = max(reference, targets["smc"]["liquidity_price"]) if long else min(reference, targets["smc"]["liquidity_price"])
                point = select_target(high, low, rules_for_target(rules, method), position["side"], reference, now, confluence)
                if method == "smc" and point and targets.get("gex_smc"):
                    farther = (point["price"] >= targets["gex_smc"]["liquidity_price"] if long else point["price"] <= targets["gex_smc"]["liquidity_price"])
                    if farther:
                        point = None
                if point and (method == "smc" or point["target_selection"]["method"] == "gex_smc"):
                    targets[method] = _target(point, rules, position["side"], now)
                else:
                    problems[method] = ("No farther GEX-supported TP2 available. "+(confluence or {}).get("reason", "Waiting for options data.").replace("; nearest SMC liquidity used", "")
                                        if method == "gex_smc" else "Waiting for untaken SMC liquidity beyond entry and the current price.")
            elif targets.get(method) is None:
                problems[method] = "Waiting for current SMC candles and a fresh quote."
            if targets.get(method):
                shadow = {**position, "target_mode": method, "take_profit": targets[method],
                          "alert_revision": (position.get("alert_revision") or "")+":"+method}
                # Existing target chronology, proximity and hit alerts run separately for each method.
                virtual = {"positions": [shadow], "outbox": document["outbox"]}
                stopped = target_advice(position, targets[method], quote, now)["light"] == "STOP LOSS"
                if not stopped:
                    monitor_holding_targets(virtual, asset, symbol, high if valid_history else [], low if valid_history else [],
                                            quote, rules, now, gex_context=confluence)
                for event in document["outbox"]:
                    if event["id"].startswith(f"holding:{position['id']}") and ":"+method+":" in event["id"]:
                        event["target_method"] = method
                        if stopped and event["status"] == "queued":
                            event.update(status="cancelled", error="Position stop currently takes precedence.")
                        if event["status"] == "queued":
                            label = "SMC TP1" if method == "smc" else "SMC + GEX TP2"
                            if not event["text"].startswith(label+"\n"):
                                event["text"] = label+"\n"+event["text"]
        position["target_errors"] = problems
        previous = position.get("nn_guidance")
        advice = nn_advice(position, reading, quote, now)
        position["nn_guidance"] = advice
        # Keep the notification baseline separate from the visible WAIT state.
        # An unavailable quote must not consume a newly adverse classification.
        previous_signal = position.get("nn_alert_signal", (previous or {}).get("signal"))
        if previous_signal is not None:
            position["nn_alert_signal"] = previous_signal
        current_signal = _current_nn_signal(reading, now)
        adverse_prediction = advice["signal"] == ("SELL" if long else "BUY")
        if current_signal and (previous_signal is None or not adverse_prediction or advice["light"] != "WAIT"):
            if (previous_signal is not None and previous_signal != advice["signal"]
                    and advice["light"] in {"STOP LOSS", "TAKE PROFIT"}
                    and advice["signal_end"] > max(position["opened_ms"], position.get("momentum_enabled_ms", 0))):
                _alert(document, position, "position_neural", f"independent-nn:{advice['signal_end']}", advice, now)
            position["nn_alert_signal"] = advice["signal"]
        stop_advice = target_advice(position, None, quote, now)
        if stop_advice["light"] == "STOP LOSS":
            _alert(document, position, "position_stop", f"stop:{position.get('stop_revision',0)}", stop_advice, now)
        for event in document["outbox"]:
            neural_event = event.get("alert_type") == "position_neural"
            recoverable = neural_event and event["status"] == "cancelled" and not event.get("retired")
            if position["id"] not in event.get("position_ids", []) or (event["status"] != "queued" and not recoverable):
                continue
            if event.get("alert_type") in {"position_neural", "position_stop"}:
                current = advice if neural_event else stop_advice
                same_signal = event.get("payload", {}).get("signal_end") == advice.get("signal_end") if neural_event else True
                enabled = alerts_enabled(position, event["alert_type"])
                if current["light"] not in {"STOP LOSS", "TAKE PROFIT"} or not same_signal or not enabled:
                    adverse = advice["signal"] == ("SELL" if long else "BUY")
                    retired = (not neural_event or not enabled or (current_signal and (not same_signal or not adverse)))
                    event.update(status="cancelled", retired=retired, error="This exit signal is no longer current.")
                else:
                    key = event["id"].split(f"holding:{position['id']}:", 1)[-1]
                    _alert(document, position, event["alert_type"], key, current, now)
