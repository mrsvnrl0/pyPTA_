"""Manually recorded holdings and durable completed-candle direction alerts."""
from __future__ import annotations

import copy
import json
import re
import uuid

from .core import M15, DataError, finite, normalise_pair, rvol, validate_candles
from .ledger import atomic_json
from .persistence import DocumentStore
from .strategy import momentum_indicators
from .smc import structure_readings
from .holding_targets import monitor_holding_targets, validate_holding_target
from .position_alerts import momentum_alert, refresh_momentum_alerts
from .buy_watches import validate_buy_watches, create_buy_watch, cancel_buy_watch, monitor_buy_watches
from .gex_targets import validate_target_mode
from .position_options import POSITION_TYPES, validate_position_target, alerts_enabled, monitored_position


def momentum_readings(candles, rules):
    """Directional regime; entry-only gates do not suppress swings in a holding."""
    if rules.strategy_model == "smc_video":
        return structure_readings(candles, rules.smc_pivot_strength)
    minimum = max(rules.momentum_roc_period+5*rules.momentum_signal_period,
                  rules.momentum_rsi_period+2, rules.volume_period+1, rules.atr_period)
    values = momentum_indicators(candles, rules)
    readings = []
    for i in range(minimum-1, len(candles)):
        roc = values["roc"][i-rules.momentum_roc_period]
        signal = values["signal"][i-rules.momentum_roc_period]
        direction = "bullish" if roc > max(0, signal) else "bearish" if roc < min(0, signal) else "neutral"
        bar = candles[i]
        readings.append({"direction": direction, "bar_ms": bar.t, "end_ms": bar.end,
                         "close": bar.c, "roc_percent": roc, "signal_percent": signal,
                         "rsi": values["rsi"][i], "rvol": rvol(candles, i, rules.volume_period),
                         "atr": values["atr"][i], "interval_ms": M15,
                         "roc_period": rules.momentum_roc_period, "signal_period": rules.momentum_signal_period,
                         "rsi_period": rules.momentum_rsi_period, "volume_period": rules.volume_period,
                         "atr_period": rules.atr_period})
    return readings


def position_pnl(position, price):
    sign = 1 if position["side"] == "long" else -1
    change = sign*(price-position["entry"])
    return {"pnl_usd": finite(change*position["quantity"], "position P/L"),
            "pnl_percent": finite(change/position["entry"]*100, "position return")}


def validate_positions(document):
    if not isinstance(document, dict) or document.get("version") != 1:
        raise DataError("Unsupported holdings file")
    json.dumps(document, allow_nan=False)
    if not isinstance(document.get("positions"), list) or not isinstance(document.get("watches"), dict) or not isinstance(document.get("outbox"), list):
        raise DataError("Malformed holdings sections")
    buy_watch_ids = validate_buy_watches(document)
    preferences = document.get("paper_alert_preferences", {})
    if not isinstance(preferences, dict):
        raise DataError("Invalid paper alert preferences")
    for key, options in preferences.items():
        if not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(options, dict) or set(options) != {"momentum_alerts", "tp_alerts"} or any(type(v) is not bool for v in options.values()):
            raise DataError("Invalid paper alert preferences")
    ids, requests = set(), set()
    for p in document["positions"]:
        if not isinstance(p, dict) or not re.fullmatch(r"[0-9a-f]{32}", p.get("id", "")) or p["id"] in ids:
            raise DataError("Invalid position identity")
        if "target_mode" in p:
            validate_position_target(p["target_mode"])
        if "nn_target_mode" in p:
            validate_target_mode(p["nn_target_mode"])
        if "position_type" in p and (p["position_type"] not in POSITION_TYPES or POSITION_TYPES[p["position_type"]][0] != p["side"]):
            raise DataError("Position type and direction disagree")
        for key in ("momentum_alerts", "tp_alerts"):
            if key in p and type(p[key]) is not bool:
                raise DataError("Position alert preferences must be true or false")
        if not re.fullmatch(r"[0-9a-f]{32}", p.get("request_id", "")) or p["request_id"] in requests:
            raise DataError("Invalid position request identity")
        ids.add(p["id"])
        requests.add(p["request_id"])
        if not isinstance(p.get("asset"), str) or not p["asset"].isalnum() or normalise_pair(p["symbol"]) != p["symbol"]:
            raise DataError("Invalid position asset")
        if not p["symbol"].endswith("/USD") or p["side"] not in {"long", "short"} or p["status"] not in {"open", "closed"}:
            raise DataError("Invalid position type")
        finite(p["entry"], "entry price", 1e-15)
        finite(p["quantity"], "quantity", 1e-15)
        finite(p["entry"]*p["quantity"], "position value", 1e-15)
        if type(p["opened_ms"]) is not int or p["opened_ms"] < 0:
            raise DataError("Invalid position tracking timestamp")
        if p["status"] == "closed":
            finite(p["close"], "close price", 1e-15)
            position_pnl(p, p["close"])
            if type(p["closed_ms"]) is not int or p["closed_ms"] < p["opened_ms"]:
                raise DataError("Invalid position close timestamp")
        elif p.get("close") is not None or p.get("closed_ms") is not None:
            raise DataError("Open position contains a closing record")
        validate_holding_target(p)
        if p.get("stop_price") is not None:
            finite(p["stop_price"], "position stop", 1e-15)
        targets = p.get("position_targets", {})
        if not isinstance(targets, dict) or set(targets)-{"smc", "gex_smc"}:
            raise DataError("Invalid independent position targets")
        for target in targets.values():
            validate_holding_target({**p, "take_profit": target})
    for asset, watch in document["watches"].items():
        if not isinstance(watch, dict) or not any(p["asset"] == asset and p["symbol"] == watch.get("symbol") and p["status"] == "open" for p in document["positions"]):
            raise DataError("Momentum watch has no open holding")
        if not re.fullmatch(r"[0-9a-f]{32}", watch.get("id", "")):
            raise DataError("Invalid position watch identity")
        for key in ("started_ms", "last_bar_ms"):
            stamp = watch.get(key)
            if not (key == "last_bar_ms" and stamp is None) and (type(stamp) is not int or stamp < 0):
                raise DataError("Invalid position watch timestamp")
        reading = watch.get("reading")
        if reading is not None:
            if reading["direction"] not in {"bullish", "bearish", "neutral"} or reading["bar_ms"] != watch["last_bar_ms"]:
                raise DataError("Invalid momentum reading")
            keys = ("close",) if reading.get("basis") == "structure" else ("close", "roc_percent", "signal_percent", "rsi", "atr")
            for key in keys:
                finite(reading[key], key)
    event_ids = set()
    for event in document["outbox"]:
        if not isinstance(event, dict) or event["id"] in event_ids or event["kind"] != "telegram":
            raise DataError("Invalid holding alert identity")
        if event["status"] not in {"queued", "running", "sent", "failed", "uncertain", "cancelled"}:
            raise DataError("Invalid holding alert status")
        if not isinstance(event["position_ids"], list) or not set(event["position_ids"]).issubset(ids):
            raise DataError("Invalid holding alert positions")
        if not isinstance(event.get("buy_watch_ids", []), list) or not set(event.get("buy_watch_ids", [])).issubset(buy_watch_ids):
            raise DataError("Invalid buy watch alert identity")
        event_ids.add(event["id"])


def prepare_positions(source, market_mode="spot"):
    """Pure holding startup recovery, shared by JSON and SQLite."""
    if market_mode not in {"spot", "margin"}:
        raise DataError("Invalid holdings market mode")
    data = {"version": 1, "positions": [], "watches": {}, "buy_watches": [], "outbox": []}
    if source is not None:
        document = json.loads(source.decode("utf-8-sig"))
        validate_positions(document)  # Fail without overwriting damaged holdings.
        data = document
        short_ids = {p["id"] for p in data["positions"] if not monitored_position(p, market_mode)}
        for event in data["outbox"]:
            if market_mode == "spot" and short_ids.intersection(event.get("position_ids", [])):
                event.update(retired=True, expires_ms=0)
                if event["status"] == "queued":
                    event.update(status="cancelled", error="Short alerts disabled in spot mode; correct the recorded purchase first.")
            if event.get("alert_type") in {"near_take_profit", "target_reached", "position_momentum", "position_structure", "position_neural", "position_stop", "buy_watch_hit", "buy_watch_momentum"} and event["status"] == "queued":
                event.update(status="cancelled", error="Restarted: waiting for fresh position alert evidence.")
            if event["status"] == "queued" and event.get("alert_type") is None:
                event.update(status="cancelled", error="Legacy grouped alert retired; waiting for a new position-specific signal.")
            if event["status"] == "running":
                event.update(status="uncertain", error="Process restarted during Telegram delivery; delivery is unknown.")
    validate_positions(data)
    return data, []


class PositionStore(DocumentStore):
    """Independent of paper balances and strategy-setting fingerprints."""
    def __init__(self, path, market_mode="spot", *, backend=None):
        self.market_mode = market_mode
        self._bind(path, backend, validate_positions, lambda path, doc: atomic_json(path, doc))
        if backend is not None:
            backend.context = {"market_mode": market_mode}
        self._initialize(lambda source: prepare_positions(source, market_mode))

    def correct_spot_buys(self, now):
        """Explicit correction of misrecorded purchases; preserve original evidence."""
        if self.market_mode != "spot":
            raise DataError("Purchase correction requires spot mode")
        with self.lock:
            if not any(p["side"] == "short" for p in self.data["positions"]):
                return []
            from .backups import backup_store
            backup = backup_store(self, source=json.dumps(self.data, allow_nan=False).encode("utf-8"),
                                  action="spot-correction")
            def correct(document):
                corrected = []
                for position in document["positions"]:
                    if position["side"] != "short":
                        continue
                    position["spot_correction"] = {"previous_side": "short", "corrected_ms": now,
                        "previous_take_profit": position.pop("take_profit", None), "backup": str(backup)}
                    position.update(side="long", position_type="spot", alert_revision="spot-v1")
                    position.pop("take_profit_error", None)
                    corrected.append(position["id"])
                for event in document["outbox"]:
                    if set(corrected).intersection(event.get("position_ids", [])):
                        event.update(retired=True, expires_ms=0,
                                     error="Recorded purchase corrected to a spot buy; previous short alert retired.")
                        if event["status"] == "queued":
                            event["status"] = "cancelled"
                        elif event["status"] == "running":
                            event["status"] = "uncertain"
                return corrected
            return self.transaction(correct)

    def open_position(self, assets, asset, side, entry, quantity, request_id, now, target_mode="smc",
                      position_type=None, nn_target_mode="gex_smc", momentum_alerts=True, tp_alerts=True, stop_price=None):
        validate_position_target(target_mode)
        validate_target_mode(nn_target_mode)
        if type(momentum_alerts) is not bool or type(tp_alerts) is not bool:
            raise DataError("Choose On or Off for position alerts")
        explicit_type = position_type is not None
        if explicit_type:
            if position_type not in POSITION_TYPES:
                raise DataError("Choose Spot, Margin long or Margin short")
            expected_side = POSITION_TYPES[position_type][0]
            if side is not None and side != expected_side:
                raise DataError("Position type and direction disagree")
            side = expected_side
        else:
            position_type = "spot" if self.market_mode == "spot" and side == "long" else "margin_" + str(side)
        if asset not in assets:
            raise DataError("Choose an enabled asset")
        if side not in {"long", "short"}:
            raise DataError("Choose long or short")
        if side == "short" and self.market_mode == "spot" and not explicit_type:
            raise DataError("Spot purchases must be recorded as buys (long). Short selling requires margin mode.")
        stop_price = finite(stop_price, "stop price", 1e-15) if stop_price is not None else None
        entry, quantity = finite(entry, "entry price", 1e-15), finite(quantity, "quantity", 1e-15)
        finite(entry*quantity, "position value", 1e-15)
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise DataError("Invalid form identity; reload the dashboard")
        symbol = assets[asset]["symbol"]
        def save(document):
            existing = next((p for p in document["positions"] if p["request_id"] == request_id), None)
            if existing:
                if any(existing.get(k, "smc" if k == "target_mode" else None) != v for k, v in {"asset": asset, "symbol": symbol, "side": side, "entry": entry, "quantity": quantity, "target_mode": target_mode}.items()):
                    raise DataError("This form was already submitted with different values; reload before adding another holding")
                for key, value in {"position_type": position_type, "nn_target_mode": nn_target_mode,
                                   "momentum_alerts": momentum_alerts, "tp_alerts": tp_alerts, "stop_price": stop_price}.items():
                    if key in existing and existing[key] != value:
                        raise DataError("This form was already submitted with different options; reload before adding another position")
                return existing, False
            if asset in document["watches"] and document["watches"][asset]["symbol"] != symbol:
                raise DataError("Close the existing holding for this asset before changing its pair")
            position = {"id": uuid.uuid4().hex, "request_id": request_id, "asset": asset, "symbol": symbol,
                        "side": side, "entry": entry, "quantity": quantity, "status": "open", "target_mode": target_mode,
                        "opened_ms": now, "close": None, "closed_ms": None,
                        "position_type": position_type, "nn_target_mode": nn_target_mode,
                        "momentum_alerts": momentum_alerts, "tp_alerts": tp_alerts, "stop_price": stop_price}
            document["positions"].append(position)
            document["watches"].setdefault(asset, {"id": uuid.uuid4().hex, "symbol": symbol, "started_ms": now,
                                                  "last_bar_ms": None, "reading": None, "rules_key": None,
                                                  "error": "Waiting for the first completed momentum reading"})
            return position, True
        return self.transaction(save)

    def set_stop(self, position_id, stop_price, now):
        value = finite(stop_price, "stop price", 1e-15) if stop_price is not None else None
        def update(document):
            position = next((p for p in document["positions"] if p["id"] == position_id), None)
            if position is None:
                raise KeyError(position_id)
            if position["status"] != "open":
                raise DataError("Only open positions can change their stop")
            if position.get("stop_price") != value:
                position.update(stop_price=value, stop_revision=uuid.uuid4().hex, stop_updated_ms=now)
                for event in document["outbox"]:
                    if position_id in event.get("position_ids", []) and event.get("alert_type") == "position_stop" and event["status"] == "queued":
                        event.update(status="cancelled", retired=True, error="Position stop changed")
            return position
        return self.transaction(update)

    def set_alerts(self, position_id, momentum_alerts, tp_alerts, now):
        if type(momentum_alerts) is not bool or type(tp_alerts) is not bool:
            raise DataError("Choose On or Off for both alert types")
        def update(document):
            position = next((p for p in document["positions"] if p["id"] == position_id), None)
            if position is None:
                raise KeyError(position_id)
            if position["status"] != "open":
                raise DataError("Only live positions have alert controls")
            if momentum_alerts and not position.get("momentum_alerts", True):
                position["momentum_enabled_ms"] = now
            position.update(momentum_alerts=momentum_alerts, tp_alerts=tp_alerts)
            for event in document["outbox"]:
                if position_id in event.get("position_ids", []) and not alerts_enabled(position, event.get("alert_type")):
                    event.update(retired=True, expires_ms=0)
                    if event["status"] == "queued":
                        event.update(status="cancelled", error="Alerts disabled for this position.")
            return position
        return self.transaction(update)

    def set_paper_alerts(self, key, momentum_alerts, tp_alerts):
        def update(document):
            options = {"momentum_alerts": momentum_alerts, "tp_alerts": tp_alerts}
            document.setdefault("paper_alert_preferences", {})[key] = options
            return options
        return self.transaction(update)

    def open_buy_watch(self, assets, asset, reference, buy_price, quantity, request_id, now, target_mode="smc"):
        if self.market_mode != "spot":
            raise DataError("SHORT buy watches require spot mode")
        return self.transaction(lambda document: create_buy_watch(
            document, assets, asset, reference, buy_price, quantity, request_id, now, target_mode))

    def cancel_buy_watch(self, watch_id):
        return self.transaction(lambda document: cancel_buy_watch(document, watch_id))

    def monitor_buy_watches(self, asset, symbol, high, low, quote, rules, now, clock_error=None, gex_context=None):
        if not any(w["asset"] == asset and w["status"] in {"watching", "reached"}
                   for w in self.snapshot().get("buy_watches", [])):
            return
        self.transaction(lambda document: monitor_buy_watches(
            document, asset, symbol, high, low, quote, rules, now, clock_error, gex_context))

    def close_position(self, position_id, price, now):
        price = finite(price, "close price", 1e-15)
        def close(document):
            position = next((p for p in document["positions"] if p["id"] == position_id), None)
            if position is None:
                raise KeyError("Position not found")
            if position["status"] == "closed":
                if position["close"] != price:
                    raise DataError("Position is already closed at a different price")
                return position
            position_pnl(position, price)
            position.update(status="closed", close=price, closed_ms=now)
            open_ids = {p["id"] for p in document["positions"] if p["status"] == "open"}
            if not any(p["asset"] == position["asset"] and p["status"] == "open" for p in document["positions"]):
                document["watches"].pop(position["asset"], None)
            for event in document["outbox"]:
                if event["status"] == "queued" and not open_ids.intersection(event["position_ids"]):
                    event.update(status="cancelled", error="The associated holdings were closed before delivery.")
            return position
        return self.transaction(close)

    def monitor(self, asset, symbol, candles, rules, now, error=None, quote=None, notify=True):
        if asset not in self.snapshot()["watches"]:
            return
        readings = []
        structural = rules.strategy_model == "smc_video"
        interval = rules.smc_entry_minutes*60000 if structural else M15
        label = f"{interval//60000}M"
        if error is None:
            try:
                validate_candles(candles, interval, now)
                if candles[-1].t != (now//interval-1)*interval:
                    raise DataError(f"Waiting for the latest completed {label} candle")
                readings = momentum_readings(candles, rules)
                if not readings:
                    raise DataError(f"Waiting for sufficient {label} momentum history")
            except (ValueError, TypeError) as exc:
                error = str(exc)
        rules_key = (f"smc-structure-v1:{interval}:{rules.smc_pivot_strength}" if structural else
                     f"roc-signal-v1:{rules.momentum_roc_period}:{rules.momentum_signal_period}")
        def update(document):
            watch = document["watches"].get(asset)
            if watch is None or watch["symbol"] != symbol:
                return
            watch["error"] = error
            if error:
                if notify:
                    refresh_momentum_alerts(document, asset, symbol, watch, None, now, self.market_mode)
                return
            baseline_time = watch["started_ms"]
            if watch["rules_key"] != rules_key:
                # A changed formula establishes a new baseline, never a false swing.
                baseline_time = now if watch.get("initialized") else watch["started_ms"]
                watch.update(rules_key=rules_key, last_bar_ms=None, reading=None)
            if watch["last_bar_ms"] is not None and readings[0]["bar_ms"] > watch["last_bar_ms"]+interval:
                watch.update(last_bar_ms=None, reading=None)
                baseline_time = now
                watch["error"] = "History gap: the latest candle establishes a new baseline; intervening swings are unknown."
            if watch["last_bar_ms"] is None:
                baseline = next((r for r in reversed(readings) if r["end_ms"] <= baseline_time), readings[0])
                watch.update(last_bar_ms=baseline["bar_ms"], reading=baseline, initialized=True)
            for reading in readings:
                if reading["bar_ms"] <= watch["last_bar_ms"]:
                    continue
                if structural and reading["event"] is None:
                    # A rolling feed can lose the candle that set the regime.
                    # Retain the saved break until a newly observed break replaces it.
                    reading = copy.deepcopy(reading)
                    for key in ("direction", "break_level", "break_ms"):
                        reading[key] = watch["reading"][key]
                previous = watch["reading"]["direction"]
                positions = [p for p in document["positions"] if p["asset"] == asset and p["symbol"] == symbol
                             and p["status"] == "open" and monitored_position(p, self.market_mode)
                             and alerts_enabled(p, "position_momentum") and p.get("target_mode") != "nn"
                             and max(p["opened_ms"], p.get("momentum_enabled_ms", 0), p.get("spot_correction", {}).get("corrected_ms", 0)) < reading["end_ms"]]
                if notify and positions and reading["direction"] != previous and reading["direction"] in {"bullish", "bearish"}:
                    for p in positions:
                        event_id = f"holding:{watch['id']}:{p['id']}:{reading['bar_ms']}:{reading['direction']}"
                        momentum_alert(document, p, reading, previous, quote, now, event_id, rules_key)
                watch.update(last_bar_ms=reading["bar_ms"], reading=reading)
            if notify:
                refresh_momentum_alerts(document, asset, symbol, watch, quote, now, self.market_mode)
        self.transaction(update)

    def monitor_take_profit(self, asset, symbol, high, low, quote, rules, now, clock_error=None, gex_context=None):
        if asset not in self.snapshot()["watches"]:
            return
        self.transaction(lambda document: monitor_holding_targets(
            document, asset, symbol, high, low, quote, rules, now, clock_error, gex_context))
