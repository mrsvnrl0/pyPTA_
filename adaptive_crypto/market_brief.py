"""One independent 15-minute research worker; never accesses trading records."""
import copy
import json
import math
import threading
import time
from pathlib import Path

from .core import H4, DataError, utc
from .ledger import atomic_json
from .market_brief_provider import grounded_brief

INTERVAL_MS = 15*60*1000


def market_context(runtime, now):
    # Copy only public prices and candle baselines. No positions, balances,
    # credentials or private alert payloads enter a research request.
    with runtime.lock:
        assets = copy.deepcopy(runtime.assets)
        market = copy.deepcopy(runtime.market)
        charts = copy.deepcopy(runtime.chart_fallback)
        generation = runtime.generation
    prices = []
    for name, cfg in assets.items():
        quote = market.get(name, {}).get("quote") or charts.get(name, {}).get("quote") or {}
        stamp, price = quote.get("asof_ms", 0), quote.get("last")
        if not isinstance(stamp, (int, float)) or not -2000 <= now-stamp <= 45000:
            continue
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            continue
        row = {"asset": name, "pair": cfg["symbol"], "price": price, "quote_at_utc": utc(stamp)}
        candles = charts.get(name, {}).get("candles", [])
        # Use six complete 4H bars when contiguous, explicitly labelled by time.
        if len(candles) >= 6:
            recent = candles[-6:]
            if recent[-1].t == now//H4*H4-H4 and all(b.t-a.t == H4 for a, b in zip(recent, recent[1:])) and recent[0].o > 0:
                row.update(change_percent=round((price/recent[0].o-1)*100, 3),
                           change_since_utc=utc(recent[0].t), baseline="open of six most recent completed 4H candles")
        prices.append(row)
    return {"asof_utc": utc(now), "asof_ms": now, "prices": prices,
            "tracked_assets": list(assets), "scope": "General crypto market; news and original public posts from the last 24 hours"}, generation


class MarketBrief:
    def __init__(self, runtime, directory, *, requester=grounded_brief, clock=None):
        self.runtime, self.requester = runtime, requester
        self.clock = clock or (lambda: int(time.time()*1000))
        self.path = Path(directory)/"market-brief-timing.json"
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.busy = False
        self.requested = False
        self.result = None
        self.error = None
        self.selection = None
        self.last_attempt = 0
        self.persisted_profile = None
        try:
            if self.path.stat().st_size <= 2048:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
                stamp = doc["last_attempt_ms"]
                if type(stamp) is int and 0 <= stamp <= self.clock()+2000:
                    self.last_attempt = stamp
                    self.persisted_profile = (doc["provider"], doc["model"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _config(self):
        return self.runtime.connections.credentials.read("ai")

    @staticmethod
    def _selection(config):
        return (config["provider"], config["model"], config["api_key"])

    def status(self):
        now = self.clock()
        try:
            config = self._config()
        except DataError:
            return {"state": "error", "error": "Check the saved AI connection in Settings.", "brief": None, "refresh_seconds": 900}
        if not config:
            return {"state": "off", "error": None, "brief": None, "refresh_seconds": 900}
        with self.lock:
            same = self.selection == self._selection(config)
            brief = copy.deepcopy(self.result) if same else None
            if brief and brief["generation"] != self.runtime.generation:
                brief = None
            if brief:
                brief.pop("generation", None)
            age = now-brief["asof_ms"] if brief else None
            state = "updating" if self.busy or self.requested else "stale" if brief and age >= INTERVAL_MS else "ready" if brief else "waiting"
            error = self.error if same else None
            if error:
                state = "stale" if brief else "error"
            return {"state": state, "brief": brief, "error": error,
                    "provider": config["provider"], "model": config["model"], "refresh_seconds": 900,
                    "next_update_ms": self.last_attempt+INTERVAL_MS if self.last_attempt else None,
                    "can_refresh": not self.busy and not self.requested and (not self.last_attempt or now-self.last_attempt >= 60000)}

    def request_refresh(self):
        if not self._config():
            raise DataError("Save an enabled AI connection in Settings first.")
        with self.lock:
            if self.busy or self.requested:
                return
            if self.last_attempt and self.clock()-self.last_attempt < 60000:
                raise DataError("Please wait one minute between summary requests.")
            self.requested = True
            self.wake.set()

    def tick(self):
        now = self.clock()
        if self.runtime.stop.is_set():
            return False
        try:
            config = self._config()
        except DataError:
            return False
        if not config:
            with self.lock:
                self.result = None
                self.requested = False
            return False
        selection = self._selection(config)
        with self.lock:
            if self.busy:
                return False
            if selection != self.selection:
                self.result, self.error = None, None
                if self.selection is not None or self.persisted_profile != selection[:2]:
                    self.last_attempt = 0
                self.selection = selection
            if not self.requested and self.last_attempt and now-self.last_attempt < INTERVAL_MS:
                return False
            self.busy = True
        try:
            context, generation = market_context(self.runtime, now)
            if not context["prices"]:
                return False
            # Persist scheduling metadata only, never researched content or keys.
            # Restarting cannot accidentally duplicate a charged request.
            with self.lock:
                self.last_attempt, self.requested = now, False
                atomic_json(self.path, {"last_attempt_ms": now, "provider": config["provider"], "model": config["model"]})
                self.error = None
            result = self.requester(config, context)
            if self.runtime.stop.is_set() or self._config() != config or generation != self.runtime.generation:
                return False
            with self.lock:
                self.result = {**result, "asof_ms": now, "generated_ms": self.clock(), "generation": generation,
                               "provider": config["provider"], "model": config["model"], "prices": context["prices"]}
            return True
        except DataError as exc:
            with self.lock:
                self.error = str(exc)
        except Exception:
            with self.lock:
                self.error = "Market summary unavailable. Check the AI connection and model search support in Settings."
        finally:
            with self.lock:
                self.busy = False
        return False

    def run(self):
        while not self.runtime.stop.is_set():
            self.tick()
            self.wake.wait(5)
            self.wake.clear()


def register_market_brief_routes(app, runtime, position_payload):
    from flask import jsonify, request
    from .deribit_settings import local_credential_request

    @app.get("/api/market-brief")
    def market_brief_status():
        response = jsonify({**runtime.market_brief.status(), "editable": local_credential_request(request)})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/market-brief/refresh")
    def market_brief_refresh():
        if not local_credential_request(request):
            return jsonify(error="Refresh from this computer's local dashboard address."), 403
        try:
            position_payload(set())
            runtime.market_brief.request_refresh()
            return jsonify(message="Summary update requested."), 202
        except DataError as exc:
            return jsonify(error=str(exc)), 400
