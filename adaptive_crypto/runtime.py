"""Market scan scheduling and thread-safe dashboard snapshots."""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
import uuid
from .core import H4, M15, M5, ENGINE_VERSION, SMC_ENGINE_VERSION, DataError, safe_error
from .engine import Engine
from .ledger import portfolio
from .market_data import Kraken
from .positions import PositionStore, position_pnl
from .position_alerts import exit_action
from .position_options import POSITION_TYPES, monitored_position, position_type, target_basis
from .position_neural import PositionNeuralReader, monitor_neural_positions
from .take_profit_alerts import fresh_quote
from .smc_engine import SMCEngine
from .smc_ledger import portfolio as smc_portfolio
from .gex import NaiveGEX
from .deribit import DeribitClient
from .deribit_settings import DeribitCredentials
from .connections import Connections
from .gex_targets import context as target_context, rules_for_target
from .administration import ActivityGate
from .feed_diagnostics import record_feed_observation


class DashboardRuntime:
    def __init__(self, assets, rules, store, refresh=15, provider=None, position_store=None, gex_provider=None, settings_path=None):
        self.assets, self.rules, self.store, self.refresh = assets, rules, store, refresh
        self.is_smc = rules.base_strategy == "smc_video"
        self.is_neural = rules.strategy_model == "neural_network"
        self.is_combined = rules.combined
        self.high_interval = rules.smc_setup_minutes*60000 if self.is_smc else H4
        self.low_interval = rules.smc_entry_minutes*60000 if self.is_smc else M5 if self.is_neural else M15
        if self.is_neural:
            from .neural_engine import NeuralEngine
            model_path = Path(rules.nn_model_path) if rules.nn_model_path else None
            if model_path is not None and not model_path.is_absolute() and settings_path:
                model_path = Path(settings_path).resolve().parent/model_path
            self.engine = NeuralEngine(assets, rules, store, model_path=model_path, settings_path=settings_path)
        else:
            self.engine = (SMCEngine if self.is_smc else Engine)(assets, rules, store)
        if getattr(store, "database_path", None) is not None and position_store is None:
            raise DataError("SQLite runtime requires explicit holdings from the same database")
        if getattr(store, "database_path", None) is not None and getattr(position_store, "database_path", None) != store.database_path:
            raise DataError("Paper and holdings must use the same SQLite database")
        self.positions = position_store or PositionStore(store.path.with_suffix(".positions.json"), market_mode=rules.market_mode)
        self.positions.market_mode = rules.market_mode
        self.position_errors = {}
        self.position_neural = PositionNeuralReader()
        self.paper_neural = PositionNeuralReader()
        self.provider = provider or Kraken()
        self.settings_path = Path(settings_path).resolve() if settings_path is not None else None
        credential_directory = self.settings_path.parent if self.settings_path else Path(store.path).resolve().parent
        self.deribit_credentials = DeribitCredentials(credential_directory)
        self.connections = Connections(credential_directory)
        self.store.connections = self.positions.connections = self.connections
        self.deribit = DeribitClient(self.deribit_credentials.read)
        self.gex = NaiveGEX(assets, provider=gex_provider if gex_provider is not None else self.deribit.fetch)
        self.lock = threading.RLock()
        self.activity_gate = ActivityGate()
        self.store.activity_gate = self.positions.activity_gate = self.activity_gate
        self.store.paper_alert_store = self.positions
        self.store.paper_namespace = rules.paper_namespace
        self.generation = uuid.uuid4().hex
        self.paused = bool(store.snapshot().get("monitoring_paused"))
        self.last_administration = None
        self.data = {}
        self.market = {}
        self.feed_observations = {}
        self.structure_baselined = set()
        self.chart_fallback = {}
        self.error = None
        self.updated_ms = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        from .market_brief import MarketBrief
        self.market_brief = MarketBrief(self, credential_directory)

    def scan_once(self):
        with self.activity_gate.activity():
            if not self.stop.is_set():
                self._scan_once()

    def _scan_neural_paper(self):
        """Give every NN asset its execution window before advisory feed work."""
        feeds = {}
        paper = self.store.snapshot()
        for name, cfg in self.assets.items():
            errors, cache = {}, {}
            try:
                now = self.provider.now()
            except Exception as exc:
                now = int(time.time()*1000)
                errors["clock"] = safe_error(exc)
            needs_stops = self.rules.nn_limitations or any(t["status"] == "active" and t["stop"] is not None
                                                          for t in paper["assets"][name]["trades"])
            for interval in (H4, M5) if needs_stops else (H4,):
                try:
                    cache[interval] = self.provider.candles(cfg["symbol"], interval, now)
                except Exception as exc:
                    cache[interval] = []
                    errors[f"{interval//60000}m"] = safe_error(exc)
            try:
                observed = self.provider.now()
            except Exception as exc:
                observed = int(time.time()*1000)
                errors["clock"] = safe_error(exc)
            # Resolve inference before obtaining the executable quote and clock.
            # evaluate() reuses this prediction, but checks its actual execution age.
            self.engine.read(name, cache[H4], observed, errors)
            try:
                quote = self.provider.quotes([cfg["symbol"]]).get(cfg["symbol"])
            except Exception as exc:
                quote = None
                errors["quote"] = safe_error(exc)
            try:
                observed = self.provider.now()
            except Exception as exc:
                observed = int(time.time()*1000)
                errors["clock"] = safe_error(exc)
            if errors.get("clock"):
                quote = None
            feeds[name] = {"cache": cache, "errors": errors, "quote": quote, "observed": observed}
            if not self.paused:
                try:
                    result = self.engine.evaluate(name, quote, cache[H4], cache.get(M5, []), observed, errors)
                    with self.lock:
                        self.data[name] = result
                except Exception as exc:
                    with self.lock:
                        prior = copy.deepcopy(self.data.get(name, {"name": name, "symbol": cfg["symbol"]}))
                        prior["scan_error"] = safe_error(exc)
                        self.data[name] = prior
        return feeds

    def _scan_once(self):
        neural_feeds = self._scan_neural_paper() if self.is_neural else {}
        quotes = {}
        if not self.is_neural:
            try:
                quotes = self.provider.quotes([a["symbol"] for a in self.assets.values()])
            except Exception:
                for cfg in self.assets.values():
                    try:
                        quotes.update(self.provider.quotes([cfg["symbol"]]))
                    except Exception:
                        pass
        for name, cfg in self.assets.items():
            feed = neural_feeds.get(name, {})
            errors, cache = dict(feed.get("errors", {})), dict(feed.get("cache", {}))
            try:
                now = self.provider.now()
            except Exception as exc:
                now = int(time.time()*1000)
                errors["clock"] = safe_error(exc)
            intervals = {H4, self.high_interval, self.low_interval,
                         self.rules.smc_setup_minutes*60000, self.rules.smc_entry_minutes*60000}
            for interval in sorted(intervals, reverse=True):
                if interval in cache:
                    continue
                try:
                    cache[interval] = self.provider.candles(cfg["symbol"], interval, now)
                except Exception as exc:
                    cache[interval] = []
                    errors[f"{interval//60000}m"] = safe_error(exc)
            try:
                profile = self.gex.snapshot(name)
            except Exception as exc:
                profile = {"status": "unavailable", "error": safe_error(exc)}
            try:
                observed = self.provider.now()
            except Exception as exc:
                observed = int(time.time()*1000)
                errors["clock"] = safe_error(exc)
            quote = None if errors.get("clock") else (feed.get("quote") if self.is_neural else quotes.get(cfg["symbol"]))
            if self.is_neural and not errors.get("clock") and not fresh_quote(quote, observed):
                # Advisory requests may take longer than a quote's lifetime.
                try:
                    quote = self.provider.quotes([cfg["symbol"]]).get(cfg["symbol"])
                    errors.pop("quote", None)
                except Exception as exc:
                    quote = None
                    errors["quote"] = safe_error(exc)
                try:
                    observed = self.provider.now()
                except Exception as exc:
                    observed = int(time.time()*1000)
                    errors["clock"] = safe_error(exc)
                    quote = None
            reading = (self.engine.read(name, cache[H4], observed, errors) if self.is_neural else
                       {"signal": None, "error": errors["clock"], "expires_ms": observed} if errors.get("clock") else
                       self.paper_neural.read(cache[H4], cfg["symbol"].split("/")[0], self.rules, self.settings_path, observed))
            high, low = cache[self.rules.smc_setup_minutes*60000], cache[self.rules.smc_entry_minutes*60000]
            from .market_analysis import analyse_market
            from .position_structure import monitor_structure
            structure = self.positions.transaction(lambda doc: monitor_structure(
                doc, name, cfg["symbol"], cache[H4], observed, self.rules.market_mode,
                error=errors.get("clock"), allow_alerts=name in self.structure_baselined))
            if not structure.get("error"):
                self.structure_baselined.add(name)
            market = analyse_market(high, low, quote, reading, self.rules, profile, observed, cfg["symbol"], structure=structure)
            with self.lock:
                self.market[name] = market
                record_feed_observation(self, name, market, errors, observed)
                self.chart_fallback[name] = {"candles": copy.deepcopy(cache[H4]),
                                             "quote": copy.deepcopy(quote), "asof_ms": observed}
            if not self.paused and not self.is_neural:
                try:
                    result = self.engine.evaluate(name, quote, cache[self.high_interval], cache[self.low_interval], observed, errors,
                                                  **({"gex_profile": profile} if self.is_smc else {}),
                                                  **({"neural_reading": reading} if self.is_combined else {}))
                    with self.lock:
                        self.data[name] = result
                except Exception as exc:
                    with self.lock:
                        prior = copy.deepcopy(self.data.get(name, {"name": name, "symbol": cfg["symbol"]}))
                        prior["scan_error"] = safe_error(exc)
                        self.data[name] = prior
            self._monitor_personal(name, cfg, self.positions.snapshot(), cache[self.high_interval],
                                   cache[self.low_interval], quote, observed, errors, profile, cache, reading)
        with self.lock:
            self.updated_ms = int(time.time()*1000)

    def _monitor_personal(self, name, cfg, holdings, high, low, quote, observed, errors,
                          gex_profile=None, feed_cache=None, neural_reading=None):
        positions = [p for p in holdings["positions"] if p["asset"] == name and p["symbol"] == cfg["symbol"] and p["status"] == "open"]
        watches = [w for w in holdings.get("buy_watches", []) if w["asset"] == name and w["status"] in {"watching", "reached"}]
        if not positions and not watches:
            return
        rules = replace(self.rules, strategy_model="smc_video", smc_gex_targets=True)
        cache = dict(feed_cache or {self.high_interval: high, self.low_interval: low})
        def candles(interval):
            if interval not in cache:
                try:
                    cache[interval] = self.provider.candles(cfg["symbol"], interval, observed)
                except Exception:
                    cache[interval] = []
            return cache[interval]
        holding_high = candles(rules.smc_setup_minutes*60000)
        holding_low = candles(rules.smc_entry_minutes*60000)
        neural_bars = candles(H4) if positions else []
        clock_error = errors.get("clock")
        try:
            now = self.provider.now()
        except Exception as exc:
            now, clock_error = int(time.time()*1000), safe_error(exc)
        quote = None if clock_error else fresh_quote(quote, now)
        try:
            profile = gex_profile if gex_profile is not None else self.gex.snapshot(name)
        except Exception as exc:
            profile = {"status": "unavailable", "error": safe_error(exc)}
        try:
            # Always calculate confluence for both target boxes, regardless of the paper model.
            gex_context = target_context(holding_high, holding_low, quote, rules, profile, now, cfg["symbol"])
            self.positions.monitor(name, cfg["symbol"], holding_low, rules, now, clock_error, quote=quote, notify=False)
            self.positions.monitor_buy_watches(name, cfg["symbol"], holding_high, holding_low, quote, rules, now, clock_error, gex_context)
            if positions:
                from .position_guidance import monitor_position_guidance
                reading = (neural_reading if neural_reading is not None else
                           self.engine.read(name, neural_bars, now, errors) if self.is_neural else
                           self.position_neural.read(neural_bars, cfg["symbol"].split("/")[0],
                                                     self.rules, self.settings_path, now))
                if clock_error:
                    reading = {"signal": None, "error": clock_error, "expires_ms": now}
                self.positions.transaction(lambda doc: monitor_position_guidance(
                    doc, name, cfg["symbol"], holding_high, holding_low, quote, reading, rules, gex_context, now))
            with self.lock:
                self.position_errors.pop(name, None)
        except Exception as exc:
            with self.lock:
                self.position_errors[name] = safe_error(exc)

    def run(self):
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                self.scan_once()
                self.error = None
            except Exception as exc:
                self.error = safe_error(exc)
            self.wake.wait(max(1, self.refresh-(time.monotonic()-start)))
            self.wake.clear()

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        state = self.store.snapshot()
        ai = self.connections.ai_configuration()
        with self.lock:
            data = copy.deepcopy(self.data)
            market = copy.deepcopy(self.market)
            updated = self.updated_ms
            position_errors = copy.deepcopy(self.position_errors)
        holdings = self.positions.snapshot()
        now = int(time.time()*1000)
        open_ids = {p["id"] for p in holdings["positions"] if p["status"] == "open"
                    and self.assets.get(p["asset"], {}).get("symbol") == p["symbol"]}
        open_watch_ids = {w["id"] for w in holdings.get("buy_watches", []) if w["status"] in {"watching", "reached"}
                          and self.assets.get(w["asset"], {}).get("symbol") == w["symbol"]}
        for event in holdings["outbox"]:
            event["is_current"] = bool(event["status"] != "cancelled" and not event.get("retired") and event.get("expires_ms", 0) > now
                                       and (open_ids.intersection(event.get("position_ids", []))
                                            or open_watch_ids.intersection(event.get("buy_watch_ids", []))))
        for watch in holdings.get("buy_watches", []):
            quote = fresh_quote(market.get(watch["asset"], data.get(watch["asset"], {})).get("quote"), now) if watch["id"] in open_watch_ids else None
            watch.update(current_ask=quote["ask"] if quote else None, current_quote_ms=quote["asof_ms"] if quote else None)
            watch["latest_alert"] = max((e for e in holdings["outbox"] if watch["id"] in e.get("buy_watch_ids", [])
                                          and not e.get("retired")), key=lambda e: e.get("observed_ms", e["created_ms"]), default=None)
        for position in holdings["positions"]:
            asset = position["asset"]
            position["review_required"] = not monitored_position(position, self.rules.market_mode)
            position["side_label"] = POSITION_TYPES[position_type(position, self.rules.market_mode)][1].upper()
            position["exit_action"] = "REVIEW RECORDED SHORT" if position["review_required"] else exit_action(position["side"])
            position["latest_alert"] = max((e for e in holdings["outbox"]
                                             if position["id"] in e.get("position_ids", [])
                                             and not (position.get("spot_correction") and e.get("retired"))),
                                            key=lambda e: (e.get("observed_ms", e["created_ms"]),
                                                           e.get("alert_type") == "target_reached", e["created_ms"]),
                                            default=None)
            position["feed_enabled"] = self.assets.get(asset, {}).get("symbol") == position["symbol"]
            watch = holdings["watches"].get(asset, {})
            reading = watch.get("reading")
            row = market.get(asset, data.get(asset, {}))
            quote = fresh_quote(row.get("quote"), now) if position["feed_enabled"] and not row.get("errors", {}).get("clock") else None
            price = position["close"] if position["status"] == "closed" else (
                quote["bid" if position["side"] == "long" else "ask"] if quote else reading["close"] if reading else None)
            position.update(mark_price=price, mark_source="Recorded close" if position["status"] == "closed" else
                            "Live bid" if quote and position["side"] == "long" else "Live ask" if quote else f"Last completed {(reading or {}).get('interval_ms', self.low_interval)//60000}M close",
                            mark_ms=position["closed_ms"] if position["status"] == "closed" else
                            quote["asof_ms"] if quote else reading["end_ms"] if reading else None)
            from .position_guidance import target_advice
            position["target_guidance"] = {method: target_advice(position, position.get("position_targets", {}).get(method), quote, now)
                                           for method in ("smc", "gex_smc")}
            guidance = position.get("nn_guidance")
            if guidance and (not quote or guidance.get("expires_ms", 0) <= now):
                guidance.update(light="WAIT", action_label="WAIT FOR FRESH NN / QUOTE")
            advice = position.get("neural_advice")
            if advice:
                advice["is_current"] = bool(position["status"] == "open" and position["feed_enabled"] and advice.get("expires_ms", 0) > now and quote)
                if position["status"] == "open" and not advice["is_current"]:
                    advice.update(action="WAIT", action_label="WAIT FOR FRESH SIGNAL / QUOTE",
                                  reason="The previous action has expired. Waiting for a current NN reading and fresh exit quote.")
            if position["review_required"]:
                position.pop("take_profit", None)
                position["take_profit_error"] = "This record needs correction to a spot buy or explicit margin mode."
            if price is not None and not position["review_required"]:
                try:
                    position.update(position_pnl(position, price))
                except DataError as exc:
                    position["pnl_error"] = str(exc)
        return {"engine": "neural-paper-v1" if self.is_neural else SMC_ENGINE_VERSION if self.is_smc else ENGINE_VERSION, "data": data, "state": state,
                "is_neural": self.is_neural, "is_combined": self.is_combined, "market": market,
                "neural_model": (self._public_neural_model() if self.is_neural and self.engine.model else None),
                "is_smc": self.is_smc, "setup_minutes": self.high_interval//60000,
                "entry_minutes": self.low_interval//60000,
                "holdings": holdings, "position_errors": position_errors,
                "portfolio": (smc_portfolio if self.is_smc or self.is_neural else portfolio)(state), "updated_ms": updated, "error": self.error,
                "telegram_configured": self.connections.status("telegram")["configured"],
                "ai_configured": ai["ready"], "ai": ai,
                "rules": asdict(self.rules),
                "administration": {"settings_file": str(self.settings_path) if self.settings_path else None,
                                   "paused": self.paused, "last_result": copy.deepcopy(self.last_administration)}}

    def _public_neural_model(self):
        model = self.engine.model
        details = {k: v for k, v in model.metadata.items() if k not in {"volume_stats", "training_assets"}}
        if self.rules.nn_model_id == "parente_mlp_v1":
            details.update(display_name="Parente 5/2", architecture_id="parente_mlp_v1",
                           artifact_id=model.identity,
                           coverage=sorted(getattr(model, "volume_stats", model.metadata.get("volume_stats", {}))),
                           target_horizon=2, lookback=1, report_url=None)
        else:
            details["lookback"] = getattr(model, "required_history_bars", 319) - 255
        details["model_id"] = self.rules.nn_model_id
        return details
