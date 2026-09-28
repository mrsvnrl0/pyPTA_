"""Completed-candle neural decisions with durable, idempotent paper execution."""
from __future__ import annotations

import copy

from .core import DataError, H4, M5, finite, safe_error, validate_candles
from .neural import NeuralModel
from .neural_ledger import close_trade, open_trade
from .take_profit_alerts import fresh_quote


class NeuralEngine:
    def __init__(self, assets, rules, store, model=None, model_path=None, settings_path=None):
        self.assets, self.rules, self.store = assets, rules, store
        self.model, self.model_error, self.cache = model, None, {}
        if model is None:
            try:
                if rules.nn_model_id == "parente_mlp_v1":
                    # Retain the original loader and its strict NPZ validation.
                    self.model = NeuralModel(model_path or rules.nn_model_path or None)
                else:
                    from .neural_models import load_selected_model
                    self.model = load_selected_model(rules, settings_path)
            except Exception as exc:
                # Existing stops remain monitored even if the model is unavailable.
                self.model_error = safe_error(exc)

    def read(self, name, high, now, errors=None):
        """One loaded model and cached classification for Home, paper and holdings.

        Reading never changes the paper ledger. Model changes take effect only
        through an explicit settings apply or restart, for every consumer together.
        """
        errors = errors or {}
        signal, problem, latest = None, self.model_error, None
        if "clock" in errors:
            problem = "Exchange clock unavailable"
        else:
            try:
                validate_candles(high, H4, now, minimum=getattr(self.model, "required_history_bars", 100))
                if high[-1].t != (now//H4-1)*H4:
                    raise DataError("Latest completed 4H candle is missing")
                latest = high[-1].t
                if self.model is not None:
                    asset = self.assets[name]["symbol"].split("/")[0]
                    cutoff = (self.model.trained_through_ms_for(asset)
                              if hasattr(self.model, "trained_through_ms_for") else self.model.trained_through_ms)
                    if high[-1].t <= cutoff:
                        raise DataError("Live signal must follow the model training/calibration period")
                    key = (name, high[-1].end)
                    if key not in self.cache:
                        predicted = self.model.predict(high, asset)
                        self.cache = {k: v for k, v in self.cache.items() if k[0] != name}
                        self.cache[key] = predicted
                    signal = self.cache[key]
            except Exception as exc:
                problem = safe_error(exc)
        return {"signal": copy.deepcopy(signal), "error": problem,
                "expires_ms": signal["signal_end"]+H4+1 if signal else now,
                "model_id": self.model.identity if self.model else None, "latest_4h_ms": latest}

    def evaluate(self, name, quote, high, low, now, errors=None):
        errors = dict(errors or {})
        observed_quote = quote
        quote = fresh_quote(quote, now) if "clock" not in errors else None
        display_quote = dict(quote) if quote else None
        if display_quote is not None:
            try:
                # Keep the last trade for charts; execution still uses only the
                # separately validated bid/ask quote below.
                display_quote["last"] = finite(observed_quote["last"], "last trade", 1e-15)
            except (DataError, KeyError, TypeError):
                pass
        row = {"name": name, "symbol": self.assets[name]["symbol"], "asof_ms": now,
               "quote": display_quote, "errors": errors, "neural": {"status": "WAITING", "signal": None}}
        valid_low = []
        existing = self.store.snapshot()["assets"][name]["trades"]
        needs_stop_history = self.rules.nn_limitations or any(t["status"] == "active" and t["stop"] is not None for t in existing)
        if "clock" not in errors and needs_stop_history:
            try:
                valid_low = validate_candles(low, M5, now, fresh=True)
                if valid_low[-1].t != (now//M5-1)*M5:
                    raise DataError("Latest completed stop-monitoring candle is missing")
            except (DataError, TypeError, AttributeError) as exc:
                valid_low = []
                errors["stop_history"] = safe_error(exc)
        reading = self.read(name, high, now, errors)
        signal, problem = reading["signal"], reading["error"]
        if reading["latest_4h_ms"] is not None:
            row["latest_4h_ms"] = reading["latest_4h_ms"]
        row["neural"].update(reading)

        def update(document):
            saved = document["assets"][name]
            trades = [t for t in saved["trades"] if t["status"] == "active"]
            stopped = False
            for trade in trades:
                if "clock" in errors or trade["stop"] is None:
                    continue
                # Only full bars that opened after entry can prove a historical stop.
                # The entry-containing bar cannot safely establish intrabar ordering.
                if valid_low:
                    new_bars = [b for b in valid_low if b.end > trade["last_bar_end"]]
                    if new_bars and new_bars[0].t > trade["last_bar_end"]+1:
                        trade["tracking_gap"] = True
                    for bar in new_bars:
                        if bar.t >= trade["opened_ms"] and bar.l <= trade["stop"]:
                            close_trade(document, trade, min(bar.o, trade["stop"]), bar.end+1, "stop")
                            stopped = True
                            break
                        trade["last_bar_end"] = max(trade["last_bar_end"], bar.end)
                else:
                    trade["tracking_gap"] = True
                if trade["status"] == "active" and quote and quote["bid"] <= trade["stop"]:
                    close_trade(document, trade, quote["bid"], now, "stop")
                    stopped = True
            if signal:
                # Candle identity (not model identity) prevents re-entry after model changes.
                end = signal["signal_end"]
                if end > saved["last_signal_end"]:
                    age = now-end
                    fresh = 0 < age <= self.rules.nn_signal_max_age_seconds*1000
                    active = [t for t in trades if t["status"] == "active"]
                    if not fresh:
                        saved.update(last_signal_end=end, last_result="Signal window elapsed; historical entries/exits are not backfilled")
                    elif stopped and signal["label"] == "BUY":
                        saved.update(last_signal_end=end, last_result="Stop exited this observation; no same-candle re-entry")
                    elif signal["label"] == "HOLD" or (signal["label"] == "SELL" and not active) or (signal["label"] == "BUY" and active and self.rules.nn_limitations):
                        saved.update(last_signal_end=end, last_result="HOLD existing position" if active else "WAIT for BUY")
                    elif not quote or quote["asof_ms"] <= end:
                        saved["last_result"] = "Waiting for a fresh executable quote after the signal close"
                    elif signal["label"] == "SELL" and active:
                        for trade in active:
                            close_trade(document, trade, quote["bid"], now, "sell")
                        saved.update(last_signal_end=end, last_result="SELL classification closed the neural paper position" if len(active) == 1 else f"SELL classification closed {len(active)} neural paper positions")
                    elif signal["label"] == "BUY":
                        if self.rules.nn_limitations and not valid_low:
                            saved["last_result"] = "Waiting for current stop-monitoring candles"
                        elif self.rules.nn_limitations and (quote["ask"]/quote["bid"]-1)*10000 > self.rules.max_spread_bps:
                            saved["last_result"] = "Spread exceeds the configured maximum"
                        else:
                            try:
                                open_trade(document, name, signal, quote, self.rules, now)
                                saved.update(last_signal_end=end, last_result="BUY opened a neural paper spot position")
                            except DataError as exc:
                                saved["last_result"] = str(exc)
                saved["model_id"] = signal["model_id"]
            if stopped and not any(t["status"] == "sold" for t in trades):
                saved["last_result"] = "Stop closed the neural paper position"
            active = [t for t in saved["trades"] if t["status"] == "active"]
            return active, saved["last_result"]
        active, result = self.store.transaction(update)
        row["neural"].update(status=("ACTIVE PAPER TRADE" if len(active) == 1 else f"{len(active)} ACTIVE PAPER TRADES") if active else "MODEL UNAVAILABLE" if problem else "WAITING",
                              trade=active[0] if active else None, trades=active, active_count=len(active), result=result)
        return row
