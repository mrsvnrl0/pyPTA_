"""Read-only market context, independent of every paper balance and order."""
import copy
from dataclasses import replace

from .core import H4, DataError, check, finite, validate_candles
from .gex_targets import context
from .smc import scan, liquidity_choices, structure_readings
from .sweep_buys import sweep_buy
from .take_profit_alerts import fresh_quote


def analyse_market(high, low, quote, reading, rules, profile, now, symbol, structure=None):
    rules = replace(rules, strategy_model="smc_video", smc_gex_targets=False)
    observed_quote = quote or {}
    quote = fresh_quote(quote, now)
    if quote:
        try:
            quote["last"] = finite(observed_quote.get("last"), "last trade", 1e-15)
        except DataError:
            quote["last"] = (quote["bid"]+quote["ask"])/2
    reading = reading or {}
    signal = reading.get("signal")
    current_nn = bool(signal and not reading.get("error") and signal.get("signal_end") == now//H4*H4-1
                      and reading.get("expires_ms", 0) > now)
    result = {"asof_ms": now, "quote": copy.deepcopy(quote),
              "nn": {"signal": copy.deepcopy(signal) if current_nn else None,
                     "error": reading.get("error") or (None if current_nn else "Waiting for the latest completed 4H NN candle"),
                     "expires_ms": reading.get("expires_ms", now)},
              "smc_long": None, "smc_short": None, "suggestion": None, "momentum": None,
              "structure": copy.deepcopy(structure or {})}
    gex_rules = replace(rules, smc_gex_targets=True)
    result["gex_context"] = context(high, low, quote, gex_rules, profile, now, symbol)
    try:
        for bars, minutes in ((high, rules.smc_setup_minutes), (low, rules.smc_entry_minutes)):
            interval = minutes*60000
            validate_candles(bars, interval, now, 2*rules.smc_pivot_strength+2)
            if bars[-1].t != (now//interval-1)*interval:
                raise DataError("Waiting for the latest completed SMC candles")
        readings = structure_readings(low, rules.smc_pivot_strength)
        result["momentum"] = copy.deepcopy(readings[-1]) if readings else None
        for side in ("long", "short"):
            result["smc_"+side] = scan(high, low, rules, side)
            strategy = result["smc_"+side]
            strategy["status"] = strategy["status"].replace("AWAITING LIMIT ORDER", "ENTRY LEVEL AVAILABLE")
            # Show remaining stages even when an upstream prerequisite has not qualified.
            if not any(c["key"] == "smc_breakout" for c in strategy["checks"]):
                stages = [
                    ("smc_sweep", "Liquidity sweep and return inside", "Confirmed setup-timeframe liquidity swept, then reclaimed"),
                    ("smc_bos", "Higher-timeframe structure break", "Completed close through opposing confirmed swing"),
                    ("smc_ob", "Fresh originating order block", "Unmitigated opposite-colour candle at the reversal origin"),
                    ("smc_retest", "First order-block revisit", "First return to the fresh order block"),
                    ("smc_gap", "Entry confirmation and gap midpoint", "FVG / inverse FVG under the selected entry method"),
                    ("smc_stop", "Valid strategy stop", "Configured stop beyond the invalidation level"),
                    ("smc_target", "Untaken opposing liquidity target", "Confirmed liquidity beyond the entry")]
                present = {c["key"] for c in strategy["checks"]}
                strategy["checks"].extend(check(key, label, False, "Waiting for preceding qualification", required)
                                          for key, label, required in stages if key not in present)
        if not quote:
            raise DataError("Waiting for a fresh live quote for SMC entry and exit prices")
        candidates = copy.deepcopy(result["smc_long"]["entries"])
        if rules.market_mode == "spot":
            for source in result["smc_short"]["entries"]:
                try:
                    candidates.append(sweep_buy(source, high, low, quote, rules, now, result["gex_context"]))
                except DataError:
                    pass
        eligible = []
        for entry in candidates:
            if entry["side"] != "long" or not entry["stop"] < entry["limit"] < entry["target"]:
                continue
            if quote["bid"] <= entry["stop"] or quote["bid"] >= entry.get("target_liquidity_price", entry["target"]):
                continue
            if entry.get("execution") == "market":
                if not 0 < now-entry["signal_end"] <= 60000:
                    continue
                price = quote["ask"]
                if (quote["asof_ms"] <= entry["signal_end"] or quote["bid"] <= entry["breakout_level"]
                        or (price/entry["reference_price"]-1)*10000 > rules.smc_breakout_max_chase_bps):
                    continue
            else:
                # A displayed historical midpoint is not a currently available entry.
                if quote["ask"] <= entry["limit"]:
                    continue
                price = entry["limit"]
            choices = liquidity_choices(high, rules.smc_pivot_strength, "long", max(price, quote["ask"]), now, low)
            if not choices:
                continue
            upper = max(choices, key=lambda item: item["price"])
            eligible.append({**entry, "entry_price": price,
                             "exit_price": upper["price"]*(1+rules.smc_tp_sweep_buffer_bps/10000),
                             "exit_liquidity": upper["price"], "exit_pivot_ms": upper["bar_ms"],
                             "nearest_target": entry["target"]})
        if eligible:
            result["suggestion"] = min(eligible, key=lambda item: item["entry_price"])
        result["error"] = None
    except (DataError, TypeError, KeyError, IndexError) as exc:
        result["error"] = str(exc)
        waiting = {"status": str(exc), "entries": [], "setup": None,
                   "checks": [check("smc_feed", "Current SMC candles and live quote", False, str(exc))]}
        for key in ("smc_long", "smc_short"):
            if result[key] is None:
                result[key] = copy.deepcopy(waiting)
    from .structure_context import qualification_checks
    for side in ("long", "short"):
        result["smc_"+side]["checks"].extend(qualification_checks(structure, side))
    return result
