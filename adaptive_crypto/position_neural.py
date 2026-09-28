"""Combine a frozen NN classification with independently calculated holding TP."""
from pathlib import Path
from .core import H4, DataError, safe_error, validate_candles
from .take_profit_alerts import fresh_quote
from .position_alerts import exit_action, identity, price
from .position_options import alerts_enabled, monitored_position, target_basis
from .ledger import queue_event


class PositionNeuralReader:
    def __init__(self):
        self.model = None
        self.key = None
        self.cache = {}

    def read(self, candles, asset, rules, settings_path, now):
        try:
            from .neural import NeuralModel, DEFAULT_MODEL
            path = Path(rules.nn_model_path) if rules.nn_model_path else DEFAULT_MODEL
            if not path.is_absolute() and settings_path:
                path = Path(settings_path).parent/path
            key = (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
            if self.key != key:
                model = NeuralModel(path)
                self.model, self.key, self.cache = model, key, {}
            validate_candles(candles, H4, now, minimum=100)
            if candles[-1].t != (now//H4-1)*H4:
                raise DataError("Waiting for the latest completed 4H NN candle.")
            if candles[-1].t <= self.model.trained_through_ms:
                raise DataError("NN observations must follow the model's training period.")
            cache_key = (asset, candles[-1].end)
            if cache_key not in self.cache:
                signal = self.model.predict(candles, asset)
                self.cache = {k: v for k, v in self.cache.items() if k[0] != asset}
                self.cache[cache_key] = signal
            return {"signal": self.cache[cache_key], "error": None, "expires_ms": candles[-1].end+H4}
        except Exception as exc:
            return {"signal": None, "error": safe_error(exc), "expires_ms": now}


def combined_advice(position, reading, quote, now):
    quote = fresh_quote(quote, now)
    signal = reading.get("signal")
    valid = signal and not reading.get("error") and now < reading.get("expires_ms", 0)
    long = position["side"] == "long"
    mark = quote["bid" if long else "ask"] if quote else None
    target = position.get("take_profit")
    reached = target and mark is not None and (mark >= target["price"] if long else mark <= target["price"])
    against = valid and signal["label"] == ("SELL" if long else "BUY")
    action = ("SELL" if long else "BUY") if quote and (reached or against) else "HOLD" if quote and valid else "WAIT"
    reason = ("Numerical take-profit reached." if reached else "Stop-loss exit: NN classification opposes this position." if against and quote
              else "NN classification supports retaining this position." if valid and quote
              else reading.get("error") or "Waiting for a current NN classification and fresh exit quote.")
    action_label = (("TAKE PROFIT" if reached else "STOP LOSS") + " · " + exit_action(position["side"])
                    if action in {"BUY", "SELL"} else action)
    return {"signal": signal["label"] if signal else None,
            "probabilities": signal.get("probabilities", {}) if signal else {},
            "signal_end": signal.get("signal_end") if signal else None,
            "model_id": signal.get("model_id") if signal else None,
            "action": action, "action_label": action_label,
            "reason": reason, "target": target["price"] if target else None,
            "target_method": target_basis(position), "target_hit": bool(reached), "mark": mark,
            "observed_ms": now, "expires_ms": min(quote["asof_ms"]+30000, reading.get("expires_ms", now)) if quote and valid else
                quote["asof_ms"]+30000 if quote and reached else now,
            "error": reading.get("error")}


def monitor_neural_positions(document, asset, symbol, reading, quote, rules, now):
    for position in document["positions"]:
        if (position["status"] != "open" or position["asset"] != asset or position["symbol"] != symbol
                or position.get("target_mode") != "nn" or not monitored_position(position, rules.market_mode)):
            continue
        previous = position.get("neural_advice")
        advice = combined_advice(position, reading, quote, now)
        position["neural_advice"] = advice
        # One notification on a new completed classification, never on each quote refresh.
        changed = previous and previous.get("signal") and previous["signal"] != advice["signal"]
        eligible = (changed and advice["action"] != "WAIT" and not advice["target_hit"] and advice["signal_end"] is not None
                    and advice["signal_end"] > max(position["opened_ms"], position.get("momentum_enabled_ms", 0)))
        for event in document["outbox"]:
            if event.get("alert_type") == "position_neural" and position["id"] in event.get("position_ids", []) and event["status"] == "queued":
                if (event.get("signal_end") != advice["signal_end"] or advice["action"] == "WAIT" or advice["target_hit"]
                        or event.get("payload", {}).get("action") != advice["action"] or not alerts_enabled(position, "position_neural")):
                    event.update(status="cancelled", retired=True, error="Superseded or disabled NN alert.")
        if not eligible or not alerts_enabled(position, "position_neural"):
            continue
        event_id = f"holding:{position['id']}:nn:{advice['model_id']}:{advice['signal_end']}"
        if any(e["id"] == event_id for e in document["outbox"]):
            continue
        title = f"{asset} #{position['id'][:8]} · NN {advice['signal']} · {advice['action_label']}"
        target_text = price(advice["target"]) if advice["target"] else "waiting for an eligible liquidity level"
        text = (f"{title}\n{identity(position)}\n{advice['reason']}\nExit quote: {price(advice['mark'])}\n"
                f"SMC{' + GEX' if advice['target_method'] == 'gex_smc' else ''} TP: {target_text}\n"
                "NN signal and numerical TP are combined by the position rules. Record your actual exit; no exchange order is submitted.")
        queue_event(document, event_id, "telegram", text, now, advice)
        document["outbox"][-1].update(alert_type="position_neural", position_ids=[position["id"]], asset=asset, symbol=symbol,
            title=title, action_label=advice["action_label"], signal_end=advice["signal_end"], observed_ms=now,
            expires_ms=advice["expires_ms"], direction="bullish" if advice["signal"] == "BUY" else "bearish" if advice["signal"] == "SELL" else "neutral",
            summary=f"{advice['action_label']} · NN {advice['signal']} · TP {target_text}")
