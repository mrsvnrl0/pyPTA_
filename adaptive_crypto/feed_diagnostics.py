"""Observations of existing scans only: this module never polls a provider."""
from __future__ import annotations

import copy
import time

from .gex import SMC_MAX_AGE_MS
from .take_profit_alerts import fresh_quote


def _measurements(market, paper, errors, now, refresh):
    """Keep feed failures separate from a healthy strategy waiting for a setup."""
    observed = market.get("asof_ms") or paper.get("asof_ms")
    quote = market.get("quote") if market else paper.get("quote")
    quote = fresh_quote(quote, now)
    error_text = "; ".join(f"{key}: {value}" for key, value in sorted(errors.items()) if value)
    quote_error = error_text or (None if quote else "Waiting for a fresh Kraken bid/ask quote")
    nn = market.get("nn") if market else paper.get("neural")
    nn = nn or {}
    signal = nn.get("signal") or {}
    scan_until = observed+max(60000, refresh*3000) if observed is not None else None
    nn_until = min(nn.get("expires_ms") or 0, scan_until or 0) or None
    nn_error = nn.get("error") or (None if signal and nn_until and now < nn_until else
                                  "Waiting for the latest completed 4H NN reading")
    smc_error = market.get("error") or errors.get("clock")
    if not smc_error and not (market.get("smc_long") and market.get("smc_short")):
        smc_error = "Waiting for completed SMC analysis"
    if not smc_error and not quote:
        smc_error = "Waiting for a fresh Kraken bid/ask quote"
    quote_until = int(quote["asof_ms"]+30000) if quote else None
    smc_until = min(observed+SMC_MAX_AGE_MS, quote_until) if observed and quote_until else None
    return {
        "kraken": {"label": "Kraken", "error": quote_error,
                   "asof_ms": int(quote["asof_ms"]) if quote else None,
                   "valid_until_ms": quote_until},
        "nn": {"label": "NN", "error": nn_error, "asof_ms": signal.get("signal_end"),
               "valid_until_ms": nn_until},
        "smc": {"label": "SMC", "error": smc_error, "asof_ms": observed,
                "valid_until_ms": smc_until},
    }


def record_feed_observation(runtime, name, market, errors, observed_ms):
    """Record already-computed results while retaining the last successful scan."""
    with runtime.lock:
        histories = getattr(runtime, "feed_observations", None)
        if histories is None:
            histories = runtime.feed_observations = {}
        asset = histories.setdefault(name, {})
        rows = _measurements(market, runtime.data.get(name, {}), errors, observed_ms, runtime.refresh)
        for key, row in rows.items():
            previous = asset.get(key, {})
            result = {**copy.deepcopy(previous), **row, "last_attempt_ms": observed_ms}
            if row["error"]:
                result.update(last_failure_ms=observed_ms, last_failure=row["error"])
            else:
                result["last_success_ms"] = row["asof_ms"] if key == "kraken" else observed_ms
            asset[key] = result


def build_feed_status(runtime, now_ms=None):
    """Return small, age-aware diagnostics; no engine reads or provider calls."""
    now = int(time.time()*1000) if now_ms is None else now_ms
    with runtime.lock:
        assets = copy.deepcopy(runtime.assets)
        market = copy.deepcopy(runtime.market)
        data = copy.deepcopy(runtime.data)
        histories = copy.deepcopy(getattr(runtime, "feed_observations", {}))
        refresh, updated, error = runtime.refresh, runtime.updated_ms, runtime.error
        paused = runtime.paused
        gex = runtime.gex
    gex_rows = gex.diagnostics(now_ms=now)
    rows = []
    for name, cfg in assets.items():
        observed = market.get(name, {}).get("asof_ms") or data.get(name, {}).get("asof_ms")
        current = histories.get(name)
        if not current:
            current = _measurements(market.get(name, {}), data.get(name, {}),
                                    data.get(name, {}).get("errors", {}), now, refresh)
            for key, row in current.items():
                row["last_attempt_ms"] = observed
                row["last_success_ms"] = (row["asof_ms"] if key == "kraken" else observed) if not row["error"] else None
        feeds = []
        for key in ("kraken", "nn", "smc"):
            row = copy.deepcopy(current[key])
            until = row.get("valid_until_ms")
            status = "unknown" if row.get("last_attempt_ms") is None else "unavailable" if row.get("error") else "ready"
            if status == "ready" and (until is None or now >= until or now < row["last_attempt_ms"]-2000):
                status, row["error"] = "stale", "The last successful observation has aged; waiting for the next scan"
            row.update(key=key, status=status, next_eligible_ms=None,
                       next_check=f"Next scanner cycle ({refresh:g}s configured); no fixed retry time recorded")
            feeds.append(row)
        feeds.append({"key": "gex", "label": "Deribit GEX", **gex_rows[name]})
        rows.append({"name": name, "symbol": cfg["symbol"], "feeds": feeds})
    return {"generated_ms": now, "last_scan_ms": updated, "scan_error": error,
            "paper_paused": paused, "assets": rows}
