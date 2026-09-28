"""Public Kraken feeds and independently cached display-only charts."""
from __future__ import annotations

import copy
import threading
import time
import requests
from .core import H4, Candle, DataError, finite, normalise_pair, parse_kraken_rows, safe_error


class Kraken:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "AdaptiveCryptoDashboard/9"
        self.cache = {}
        self.offset_ms = 0
        self.clock_checked = 0

    def get(self, endpoint, **params):
        response = self.session.get("https://api.kraken.com/0/public/"+endpoint, params=params, timeout=(4, 8))
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("result"), dict):
            raise DataError(f"Kraken {endpoint}: {body.get('error', 'malformed result') if isinstance(body, dict) else 'malformed result'}")
        return body["result"]

    def now(self):
        local = int(time.time()*1000)
        if local-self.clock_checked >= 60000:
            before = int(time.time()*1000)
            server = finite(self.get("Time")["unixtime"], "server time", 1)*1000
            after = int(time.time()*1000)
            self.offset_ms = server-(before+after)//2
            self.clock_checked = after
        return int(time.time()*1000+self.offset_ms)

    def quotes(self, pairs):
        body = self.get("Ticker", pair=",".join(pairs), assetVersion=1)
        now = self.now()
        result = {}
        for key, row in body.items():
            pair = normalise_pair(key)
            result[pair] = {"bid": finite(row["b"][0], "bid", 1e-15), "ask": finite(row["a"][0], "ask", 1e-15),
                            "last": finite(row["c"][0], "last trade", 1e-15),
                            "low24": finite(row["l"][1], "24h low", 1e-15), "high24": finite(row["h"][1], "24h high", 1e-15),
                            "volume24_base": finite(row["v"][1], "24h base volume", 0), "asof_ms": now}
        return result

    def candles(self, pair, interval, now):
        key = (pair, interval)
        cached = self.cache.get(key)
        expected = (now//interval-1)*interval
        if cached and cached[-1].t == expected:
            return cached
        body = self.get("OHLC", pair=pair, interval=interval//60000, assetVersion=1)
        rows = [v for k, v in body.items() if k != "last"]
        if len(rows) != 1:
            raise DataError("Ambiguous Kraken OHLC response")
        candles = parse_kraken_rows(rows[0], interval, now)
        self.cache[key] = candles
        return candles


class LiveCharts:
    """Retain sixteen setup-timeframe bars, including the uncommitted current bar."""

    def __init__(self, assets, provider=None, ttl=15, fallback=None, interval=H4):
        self.assets = {name: cfg["symbol"] for name, cfg in assets.items()}
        self.provider = Kraken() if provider is None else provider
        self.ttl = finite(ttl, "chart cache lifetime", 0)
        self.fallback = fallback
        self.interval = interval
        self._locks = {name: threading.Lock() for name in self.assets}
        self._provider_lock = threading.Lock()
        self._cache = {}

    def _fetch(self, symbol):
        # This session/clock belongs only to charts. Serializing its calls keeps
        # simultaneous asset requests out of the same requests.Session.
        with self._provider_lock:
            body = self.provider.get("OHLC", pair=symbol, interval=self.interval//60000, assetVersion=1)
            try:
                now = finite(self.provider.now(), "chart observation time", 0)
            except Exception:
                # The chart is display-only. If Kraken's separate time request
                # is briefly unavailable, the OHLC payload can still be shown
                # using the local clock; signal qualification keeps its own
                # stricter clock path.
                now = float(int(time.time() * 1000))
        if not now.is_integer():
            raise DataError("Chart observation time must be integer milliseconds")
        now = int(now)
        if not isinstance(body, dict):
            raise DataError("Malformed Kraken chart response")
        series = [(key, value) for key, value in body.items() if key != "last"]
        if len(series) != 1 or normalise_pair(series[0][0]) != normalise_pair(symbol):
            raise DataError("Ambiguous or mismatched Kraken chart pair")
        rows = series[0][1]
        # Kraken can return 720 completed observations plus the forming candle.
        if not isinstance(rows, list) or not 2 <= len(rows) <= 721:
            raise DataError("Kraken chart requires 2 to 721 OHLC rows including the current candle")

        candles = []
        current_start = (now // self.interval) * self.interval
        previous = None
        for row in rows[-16:]:
            if not isinstance(row, list) or len(row) < 8:
                raise DataError("Malformed Kraken chart OHLC row")
            stamp = finite(row[0], "chart timestamp", 0)
            if not stamp.is_integer():
                raise DataError("Chart timestamp must be integer seconds")
            stamp = int(stamp) * 1000
            if stamp % self.interval or stamp > current_start:
                raise DataError("Misaligned or future chart candle")
            if previous is not None and stamp != previous + self.interval:
                raise DataError("Duplicate, unordered, or missing chart candles")
            o, h, low, c = (finite(row[i], "chart OHLC price", 1e-15) for i in (1, 2, 3, 4))
            if h < max(o, c, low) or low > min(o, c):
                raise DataError("Inconsistent chart OHLC bounds")
            candles.append({"t": stamp, "o": o, "h": h, "l": low, "c": c,
                            "current": stamp == current_start})
            previous = stamp
        if not candles or candles[-1]["t"] != current_start:
            raise DataError("Kraken chart current candle is stale")
        return {"symbol": symbol, "interval_ms": self.interval, "candles": candles,
                "asof_ms": now, "stale": False, "degraded": False, "error": None}

    def _fallback_result(self, name, symbol):
        if self.fallback is None:
            return None
        try:
            snapshot = self.fallback(name)
        except Exception:
            return None
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("candles"), list):
            return None
        candles = []
        for bar in snapshot["candles"][-16:]:
            try:
                if isinstance(bar, Candle):
                    stamp, o, h, low, c = bar.t, bar.o, bar.h, bar.l, bar.c
                else:
                    stamp, o, h, low, c = bar["t"], bar["o"], bar["h"], bar["l"], bar["c"]
                candles.append({"t": int(stamp), "o": float(o), "h": float(h), "l": float(low), "c": float(c), "current": False})
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
        if not candles:
            return None
        asof_ms = snapshot.get("asof_ms")
        quote = snapshot.get("quote")
        # A validated latest quote can keep a forming candle visible while
        # the separate OHLC request retries. Its high/low are explicitly
        # quote-only and never enter signal evaluation.
        if isinstance(quote, dict) and asof_ms is not None:
            try:
                last = finite(quote["last"], "chart quote", 1e-15)
                asof_ms = int(finite(asof_ms, "chart fallback time", 0))
                quote_time = finite(quote.get("asof_ms", asof_ms), "chart quote time", 0)
                if not -2000 <= time.time()*1000-quote_time <= 45000:
                    raise DataError("Chart fallback quote is stale")
                current_start = (asof_ms // self.interval) * self.interval
                if current_start > candles[-1]["t"]:
                    opening = candles[-1]["c"]
                    candles = candles[-15:] + [{"t": current_start, "o": opening,
                                                 "h": max(opening, last), "l": min(opening, last),
                                                 "c": last, "current": True}]
                    return {"symbol": symbol, "interval_ms": self.interval, "candles": candles,
                            "asof_ms": asof_ms, "stale": False, "degraded": True, "error": None}
            except (KeyError, TypeError, ValueError):
                pass
        return {"symbol": symbol, "interval_ms": self.interval, "candles": candles,
                "asof_ms": asof_ms, "stale": True, "degraded": False, "error": None}

    def get(self, name):
        # Reject unknown assets before touching the provider or making requests.
        if name not in self.assets:
            raise KeyError(name)
        with self._locks[name]:
            cached = self._cache.get(name)
            if cached is not None and time.monotonic() < cached["expires"]:
                return copy.deepcopy(cached["result"])
            try:
                result = self._fetch(self.assets[name])
            except Exception as exc:
                result = self._fallback_result(name, self.assets[name])
                if result is None and cached:
                    result = copy.deepcopy(cached["result"])
                    result.update(stale=True, degraded=False)
                if result is None:
                    result = {"symbol": self.assets[name], "interval_ms": self.interval,
                              "candles": [], "asof_ms": None, "degraded": False}
                result.update(stale=not result.get("degraded", False),
                              error=safe_error(exc) or "Chart data unavailable")
            # Cache errors too: an outage must not turn every page load into
            # another public API request. A failure retains the original as-of.
            self._cache[name] = {"expires": time.monotonic() + self.ttl, "result": result}
            return copy.deepcopy(result)
