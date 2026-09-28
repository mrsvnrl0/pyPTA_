"""Signed open-interest gamma proxy with an independently refreshed cache."""
from __future__ import annotations

import copy
import math
import threading
import time
import requests
from .core import DataError, finite, normalise_pair, safe_error

YEAR_MS = 365.25 * 86400000
MAX_AGE_MS = 300000
SMC_MAX_AGE_MS = 45000


def exposure(option, spot):
    """Zero-rate Black-Scholes gamma, USD delta change per 1% spot move.

    Deribit option OI is already in base coins: do NOT multiply contract_size.
    Calls positive / puts negative is an assumption, not dealer inventory.
    """
    width = option["iv"] * math.sqrt(option["years"])
    d1 = (math.log(spot / option["strike"]) + width * width / 2) / width
    gamma = math.exp(-d1 * d1 / 2) / (math.sqrt(2 * math.pi) * spot * width)
    return gamma * option["oi"] * spot * spot * .01 * option["sign"]


def build_profile(instruments, summaries, base, spot, now):
    spot = finite(spot, "options index", 1e-15)
    books = {}
    for book in summaries:
        name = book["instrument_name"]
        if name in books:
            raise DataError("Ambiguous options snapshot: duplicate instrument summary")
        books[name] = book
    options, stamps, expiries = [], [], []
    seen = set()
    eligible = 0
    for inst in instruments:
        if inst.get("base_currency") != base or inst.get("kind") != "option" or not inst.get("is_active"):
            continue
        expiry = finite(inst["expiration_timestamp"], "expiry", 0)
        if expiry <= now:
            continue
        name = inst["instrument_name"]
        if name in seen:
            raise DataError("Ambiguous options snapshot: duplicate active instrument")
        seen.add(name)
        eligible += 1
        book = books.get(inst["instrument_name"])
        if book is None:
            raise DataError("Incomplete options snapshot: missing instrument summary")
        stamp = finite(book["creation_timestamp"], "options timestamp", 0)
        if not -2000 <= now - stamp <= MAX_AGE_MS:
            raise DataError("Stale options summary")
        stamps.append(stamp)
        oi = finite(book["open_interest"], "option open interest", 0)
        if oi == 0:
            continue
        kind = inst.get("option_type")
        if kind not in {"call", "put"}:
            raise DataError("Unknown option type")
        options.append({"strike": finite(inst["strike"], "strike", 1e-15),
                        "iv": finite(book["mark_iv"], "mark IV", 1e-15) / 100,
                        "oi": oi, "years": (expiry - now) / YEAR_MS,
                        "sign": 1 if kind == "call" else -1})
        expiries.append(expiry)
    if not options:
        raise DataError("No active options with positive open interest for this asset")
    strikes = {}
    for option in options:
        k = option["strike"]
        row = strikes.setdefault(k, {"strike": k, "call_gex": 0., "put_gex": 0., "net_gex": 0.})
        value = finite(exposure(option, spot), "gamma exposure")
        row["call_gex" if option["sign"] > 0 else "put_gex"] += value
        row["net_gex"] += value
    rows = sorted(strikes.values(), key=lambda r: r["strike"])
    calls = [r for r in rows if r["net_gex"] > 0]
    puts = [r for r in rows if r["net_gex"] < 0]
    call = max(calls, key=lambda r: r["net_gex"], default=None)
    put = min(puts, key=lambda r: r["net_gex"], default=None)
    def total(price):
        return finite(math.fsum(exposure(o, price) for o in options), "net gamma")
    # Reprice the entire chain with fixed IV/OI. A strike cumulative sum is NOT a flip.
    curve = [{"price": spot * (.7 + i * .005)} for i in range(121)]
    for point in curve:
        point["gex"] = total(point["price"])
    roots = []
    nonzero = [p for p in curve if p["gex"] != 0]
    for left, right in zip(nonzero, nonzero[1:]):
        if (left["gex"] > 0) == (right["gex"] > 0):
            continue
        low, high, sign = left["price"], right["price"], left["gex"] > 0
        for _ in range(32):
            mid = (low + high) / 2
            if (total(mid) > 0) == sign:
                low = mid
            else:
                high = mid
        roots.append((low + high) / 2)
    net = total(spot)
    return {"status": "ready", "stale": False, "error": None, "source": "Deribit",
            "base": base, "spot": spot, "asof_ms": int(min(stamps)), "calculated_ms": now,
            "expires_ms": int(min(expiries)), "option_count": len(options), "eligible_count": eligible,
            "strikes": rows, "net_gex": net,
            "regime": "positive" if net > 0 else "negative" if net < 0 else "neutral",
            "call_wall": copy.deepcopy(call), "put_wall": copy.deepcopy(put),
            "gamma_flip": min(roots, key=lambda p: abs(p-spot)) if roots else None,
            "flip_candidates": roots, "flip_range": [curve[0]["price"], curve[-1]["price"]],
            "curve": curve, "units": "USD delta per 1% index move"}


class NaiveGEX:
    """Independent cache: options outages cannot block candle scans or trades."""
    def __init__(self, assets, provider=None, ttl=120):
        self.assets = {name: normalise_pair(cfg["symbol"]) for name, cfg in assets.items()}
        self.provider = provider
        self.ttl = ttl
        self.cache = {}
        self.locks = {name: threading.Lock() for name in assets}
        self.snapshot_lock = threading.Lock()
        self.published = {}
        self.refreshing = set()
        self.next_refresh = {}
        self.feed_history = {}

    def diagnostics(self, now_ms=None):
        """Read published metadata without fetching or taking a provider lock."""
        now = int(time.time()*1000) if now_ms is None else now_ms
        monotonic_now = time.monotonic()
        with self.snapshot_lock:
            published = copy.deepcopy(self.published)
            history = copy.deepcopy(self.feed_history)
            refreshing = set(self.refreshing)
            next_refresh = dict(self.next_refresh)
        rows = {}
        for name, symbol in self.assets.items():
            base, _, quote = symbol.partition("/")
            profile = published.get(name, {})
            details = history.get(name, {})
            supported = base in {"BTC", "ETH", "SOL"} and quote == "USD"
            stamp, expiry = profile.get("asof_ms"), profile.get("expires_ms")
            valid_until = min(stamp+MAX_AGE_MS, expiry) if stamp is not None and expiry else None
            status, error = profile.get("status", "unknown"), profile.get("error")
            if not supported:
                status, error = "unsupported", "Naive GEX supports BTC/USD, ETH/USD and SOL/USD only"
            elif status == "ready" and (valid_until is None or now >= valid_until or now < stamp-2000):
                status, error = "stale", "Options snapshot aged or an included expiry settled"
            busy = name in refreshing or details.get("in_progress", False)
            # HTTP requests honor the provider cache deadline; background scans
            # can have a later throttle after reading that cache. Do not promise
            # that a scan starts a fetch as soon as the HTTP cache is eligible.
            retry_at = details.get("retry_at", 0)
            eligible_ms = now+max(0, int((retry_at-monotonic_now)*1000)) if supported and not busy else None
            scan_at = max(retry_at, next_refresh.get(name, 0))
            rows[name] = {"status": status, "error": error, "asof_ms": stamp,
                          "valid_until_ms": valid_until, "refreshing": bool(busy),
                          "last_attempt_ms": details.get("last_attempt_ms"),
                          "last_success_ms": details.get("last_success_ms"),
                          "last_failure_ms": details.get("last_failure_ms"),
                          "last_failure": details.get("last_failure"),
                          "next_eligible_ms": eligible_ms,
                          "next_scan_eligible_ms": now+max(0, int((scan_at-monotonic_now)*1000)) if supported and not busy else None,
                          "next_check": "Unsupported pair" if not supported else
                          "Refresh in progress" if busy else
                          "Eligible for a GEX request; scans use their separate refresh schedule" if retry_at <= monotonic_now else
                          "Cache retry eligibility; a later GEX request or eligible scan starts the refresh"}
        return rows

    def snapshot(self, name):
        """Return immediately; options network work never holds up exits or scans."""
        with self.snapshot_lock:
            result = copy.deepcopy(self.published.get(name))
            if name not in self.refreshing and time.monotonic() >= self.next_refresh.get(name, 0):
                self.refreshing.add(name)
                def refresh():
                    try:
                        self.get(name)
                    finally:
                        with self.snapshot_lock:
                            self.refreshing.discard(name)
                            self.next_refresh[name] = time.monotonic()+self.ttl
                threading.Thread(target=refresh, daemon=True, name="gex-"+name).start()
        return result

    @staticmethod
    def fetch(base, currency):
        with requests.Session() as session:
            def get(method, **params):
                response = session.get("https://www.deribit.com/api/v2/public/" + method,
                                       params=params, timeout=(4, 8))
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("error") or "result" not in body:
                    raise DataError("Deribit returned an invalid options response")
                return body["result"]
            instruments = get("get_instruments", currency=currency, kind="option", expired="false")
            summaries = get("get_book_summary_by_currency", currency=currency, kind="option")
            index = get("get_index_price", index_name=base.lower() + ("_usdc" if currency == "USDC" else "_usd"))
            return instruments, summaries, index["index_price"]

    def get(self, name):
        if name not in self.assets:
            raise KeyError(name)
        base, _, quote = self.assets[name].partition("/")
        if base not in {"BTC", "ETH", "SOL"} or quote != "USD":
            return {"status": "unsupported", "stale": False, "asof_ms": None,
                    "error": "Naive GEX supports BTC/USD, ETH/USD and SOL/USD only"}
        with self.locks[name]:
            now = int(time.time() * 1000)
            cached = self.cache.get(name)
            if cached and time.monotonic() < cached["retry_at"]:
                result = copy.deepcopy(cached["result"])
            else:
                with self.snapshot_lock:
                    history = self.feed_history.setdefault(name, {})
                    history.update(last_attempt_ms=now, in_progress=True)
                try:
                    currency = "USDC" if base == "SOL" else base
                    instruments, summaries, spot = (self.provider or self.fetch)(base, currency)
                    now = int(time.time() * 1000)
                    result = build_profile(instruments, summaries, base, spot, now)
                    result["market"] = f"{base} options · {currency} settlement · all active expiries"
                    result["quote_currency"] = "USDC" if currency == "USDC" else "USD"
                    result["units"] = result["quote_currency"] + " delta per 1% index move"
                    with self.snapshot_lock:
                        self.feed_history[name]["last_success_ms"] = now
                except Exception as exc:
                    result = copy.deepcopy(cached["result"]) if cached else {"asof_ms": None}
                    result.update(status="stale" if result.get("asof_ms") else "unavailable",
                                  stale=True, error=safe_error(exc))
                    with self.snapshot_lock:
                        self.feed_history[name].update(last_failure_ms=int(time.time()*1000),
                                                       last_failure=result["error"])
                self.cache[name] = {"result": copy.deepcopy(result), "retry_at": time.monotonic() + self.ttl}
                with self.snapshot_lock:
                    self.feed_history[name].update(in_progress=False, retry_at=self.cache[name]["retry_at"])
            if result.get("status") == "ready" and (
                    not -2000 <= now - result["asof_ms"] <= MAX_AGE_MS or now >= result["expires_ms"]):
                result.update(status="stale", stale=True, error="Options snapshot aged or an included expiry settled")
            with self.snapshot_lock:
                self.published[name] = copy.deepcopy(result)
            return result


def map_smc(profile, row, now):
    """Exact wall/block overlap with current, healthy SMC evidence only."""
    result = copy.deepcopy(profile)
    quote = row.get("quote") or {}
    price, quote_until, scan_until = None, None, None
    try:
        bid = finite(quote.get("bid"), "Kraken bid", 1e-15)
        ask = finite(quote.get("ask"), "Kraken ask", 1e-15)
        last = finite(quote.get("last"), "Kraken last", 1e-15)
        stamp = finite(quote.get("asof_ms"), "Kraken quote time", 0)
        if bid <= ask and -2000 <= now-stamp <= SMC_MAX_AGE_MS and not row.get("errors", {}).get("clock"):
            price, quote_until = last, int(stamp+SMC_MAX_AGE_MS)
        scan_stamp = finite(row.get("asof_ms"), "SMC scan time", 0)
        if -2000 <= now-scan_stamp <= SMC_MAX_AGE_MS:
            scan_until = int(scan_stamp+SMC_MAX_AGE_MS)
    except (DataError, TypeError, AttributeError):
        pass
    fresh = (price is not None and scan_until is not None and
             not row.get("errors") and not row.get("scan_error"))
    result["price"] = price
    result["price_valid_until_ms"] = quote_until
    result["smc_valid_until_ms"] = min(quote_until, scan_until) if fresh else None
    result["options_valid_until_ms"] = (min(result["asof_ms"]+MAX_AGE_MS, result["expires_ms"])
                                         if result.get("asof_ms") is not None and result.get("expires_ms") else None)
    if result.get("status") == "ready" and (result.get("stale") or
            result["options_valid_until_ms"] is None or now >= result["options_valid_until_ms"]
            or now < result["asof_ms"]-2000):
        result.update(status="stale", stale=True, error="Options snapshot aged or an included expiry settled")
    ready = result.get("status") == "ready"
    result["smc_fresh"] = bool(fresh)
    result["basis_percent"] = (100*(result["spot"]/price-1)
                               if ready and price is not None and result.get("spot") else None)
    result["levels"] = []
    for key, label, value in (("call", "Call wall", (result.get("call_wall") or {}).get("strike")),
                              ("flip", "Gamma flip", result.get("gamma_flip")),
                              ("put", "Put wall", (result.get("put_wall") or {}).get("strike"))):
        distance = 100*(value/price-1) if ready and value is not None and price is not None else None
        result["levels"].append({"key": key, "label": label, "price": value, "distance_percent": distance,
                                 "relation": None if distance is None else
                                 "above" if distance > 0 else "below" if distance < 0 else "at"})
    zones = []
    result["zone_errors"] = []
    if fresh:
        for key, wall_key, label in (("smc_short", "call_wall", "Bearish HTF order block"),
                                     ("smc_long", "put_wall", "Bullish HTF order block")):
            strategy = row.get(key) or {}
            setup = strategy.get("setup")
            if not setup:
                continue
            try:
                if setup.get("kind") == "breakout":
                    continue  # A lower-timeframe breakout range is not an HTF order block.
                low = finite(setup["zone_low"], "order block low", 1e-15)
                high = finite(setup["zone_high"], "order block high", low)
                confirmed = finite(setup["bos_end"], "structure confirmation time", 0)
                if confirmed > now:
                    raise DataError("Order block confirmation is in the future")
            except (DataError, KeyError, TypeError) as exc:
                result["zone_errors"].append(f"{label} unavailable: {safe_error(exc)}")
                continue
            wall = result.get(wall_key)
            reference_only = any(word in str(strategy.get("status", "")).upper() for word in
                                 ("MISSED", "EXPIRED", "RETIRED", "HISTORY"))
            overlap = (low <= wall["strike"] <= high) if wall else False
            nearest = low if price < low else high if price > high else price
            zones.append({"label": label, "low": low, "high": high,
                          "side": "short" if key == "smc_short" else "long",
                          "confirmed_ms": int(confirmed), "wall_price": wall["strike"] if wall else None,
                          "distance_percent": 100*(nearest/price-1),
                          "relation": "above" if price < low else "below" if price > high else "inside",
                          "reference_only": reference_only,
                          "confluence": bool(overlap and not reference_only and ready)})
    result["zones"] = zones
    confluence = copy.deepcopy(row.get("gex_context") or {})
    selection_fresh = (fresh and ready and confluence.get("ready") and
                       confluence.get("gex_asof_ms") == result.get("asof_ms") and
                       now < confluence.get("valid_until_ms", 0))
    result["pois"] = confluence.get("pois", []) if fresh else []
    result["target_confluence"] = {**confluence, "ready": bool(selection_fresh),
        "matches": confluence.get("matches", []) if selection_fresh else [],
        "reason": confluence.get("reason", "Waiting for SMC target analysis") if selection_fresh or not confluence.get("ready")
                  else "Waiting for target analysis with current GEX and SMC data"}
    return result
