"""Pure reclaim, momentum, and entry-plan evaluation."""
from __future__ import annotations

import copy
from .core import (
    H4, M15, DataError, finite, atr_series, rvol, check, pivot, passed,
    trend_at, location, ema, rsi_series,
)


def sweep_evidence(bar, liquidity_low, previous_atr, minimum_depth):
    """Use the same candle and pre-candle ATR for qualification and its evidence."""
    low_below = liquidity_low-minimum_depth*previous_atr
    evidence = check(
        "sweep", "Sweep below liquidity and close back above",
        bar.l < low_below and bar.c > liquidity_low,
        {"low": bar.l, "close": bar.c,
         "sweep_depth_atr": (liquidity_low-bar.l)/previous_atr,
         "liquidity_low": liquidity_low, "previous_atr": previous_atr},
        {"low_below": low_below, "close_above": liquidity_low,
         "sweep_depth_atr_above": minimum_depth}, bar.t)
    evidence["note"] = ("Depth = (liquidity low − candle low) / previous 4H ATR. "
                        "Negative depth means the low is still above liquidity.")
    return evidence


def reclaim_scan(candles, rules, now, excluded=(), minimum_reclaim_end=0):
    """One evaluator supplies both the executable setup and its UI evidence."""
    empty = {"status": "WAITING FOR ORDER BLOCK", "checks": [check("block", "Last bullish block followed by a bearish break")], "setup": None}
    if len(candles) < max(rules.atr_period, rules.volume_period) + 5:
        return empty
    atr = atr_series(candles, rules.atr_period)
    results = []
    last = len(candles)-1
    for ob in range(max(rules.volume_period, last-rules.ob_lookback), last):
        block = candles[ob]
        if block.c <= block.o:
            continue
        for violation in range(ob+1, min(ob+rules.ob_follow_bars+1, len(candles))):
            bar = candles[violation]
            if bar.c > bar.o:
                break  # The block must be the last up candle.
            a = atr[violation-1]
            rv = rvol(candles, violation, rules.volume_period)
            if not a or not (bar.c < block.o and (bar.o-bar.c)/a >= rules.bearish_body_atr
                            and location(bar) <= rules.bearish_close_location
                            and (rules.bearish_rvol_min == 0 or (rv is not None and rv >= rules.bearish_rvol_min))):
                continue
            if now - bar.end > rules.setup_lifetime_bars * H4:
                break
            checks = [check("block", "Bearish close through last bullish block", True,
                            {"body_atr": (bar.o-bar.c)/a, "close_location": location(bar), "close": bar.c, "rvol": rv},
                            {"body_atr_min": rules.bearish_body_atr, "close_location_max": rules.bearish_close_location,
                             "close_below": block.o, "rvol_min": rules.bearish_rvol_min or "context only"}, bar.t)]
            # A pivot's right-hand bars must have closed BEFORE the violation.
            lows = [p for p in range(rules.pivot_strength, violation-rules.pivot_strength)
                    if pivot(candles, p, rules.pivot_strength, violation-1)]
            result = {"status": "WAITING FOR CONFIRMED LIQUIDITY LOW", "checks": checks, "setup": None, "time": bar.t}
            if not lows:
                checks.append(check("liquidity", "Pivot low known before violation", False))
                results.append(result)
                break
            ssl = candles[lows[-1]].l
            checks.append(check("liquidity", "Pivot low known before violation", True, ssl, None, candles[lows[-1]].t))
            sweep_checks = {s: sweep_evidence(candles[s], ssl, atr[s-1], rules.sweep_atr)
                            for s in range(violation, len(candles))}
            sweeps = [s for s, evidence in sweep_checks.items() if evidence["status"] == "pass"]
            if not sweeps:
                evidence = sweep_checks[last]
                evidence["note"] = ("Latest completed 4H candle; no qualifying sweep since the bearish break. "
                                    + evidence["note"])
                checks.append(evidence)
                result["status"] = "WAITING FOR LIQUIDITY SWEEP"
                results.append(result)
                break
            best_for_block = None
            for s in sweeps:
                for j in range(s, len(candles)):
                    reclaim = candles[j]
                    if reclaim.end < minimum_reclaim_end:
                        continue
                    if j < rules.mss_lookback:
                        if j == last:
                            results.append({"status": "WAITING FOR 4H STRUCTURE HISTORY", "setup": None,
                                            "checks": checks+[sweep_checks[s], check(
                                                "history", "Completed 4H candles before reclaim", False,
                                                j, rules.mss_lookback, reclaim.t)], "time": reclaim.t})
                        continue
                    a = atr[j-1]  # Uncontaminated volatility baseline.
                    rv = rvol(candles, j, rules.volume_period)
                    high3 = max(b.h for b in candles[j-rules.mss_lookback:j])
                    body = (reclaim.c-reclaim.o)/a if a else 0
                    tests = [
                        sweep_checks[s],
                        check("body", "Bullish reclaim body / previous ATR", body >= rules.reclaim_body_atr, body, rules.reclaim_body_atr, reclaim.t),
                        check("close", "Reclaim close location", location(reclaim) >= rules.reclaim_close_location, location(reclaim), rules.reclaim_close_location, reclaim.t),
                        check("volume", f"Reclaim volume / preceding {rules.volume_period}-bar mean", rv is not None and rv >= rules.reclaim_rvol_min, rv, rules.reclaim_rvol_min, reclaim.t),
                        check("break", "Close above block and prior structure", reclaim.c > max(block.c, high3), reclaim.c, max(block.c, high3), reclaim.t),
                    ]
                    if rules.reclaim_require_trend:
                        tests.append(check("trend", "4H close > EMA200 and EMA50 > EMA200", trend_at(candles, j) is True, trend_at(candles, j), True, reclaim.t))
                    if not passed(tests):
                        if j == last:
                            results.append({"status": "WAITING FOR BULLISH RECLAIM", "checks": checks+tests, "setup": None, "time": reclaim.t})
                        continue
                    # Anchor the entire sweep-to-reclaim leg, including the reclaim wick.
                    leg_low = min(b.l for b in candles[s:j+1])
                    leg_high = max(b.h for b in candles[s:j+1])
                    equilibrium = (leg_low+leg_high)/2
                    zone_low = max(block.o, leg_low)
                    zone_high = min(block.c, equilibrium)
                    stop = min(block.l, leg_low)-rules.stop_buffer_atr*atr[j]
                    key = f"reclaim:{block.t}:{reclaim.t}"
                    if key in excluded:
                        continue  # Only this identity is consumed; later reclaims can qualify.
                    later = candles[j+1:]
                    if any(b.c < block.o or b.l <= stop for b in later):
                        # This reclaim has failed. A distinct later reclaim is a new setup.
                        continue
                    tests.append(check("discount", "Block overlaps lower half of reclaim leg", zone_low < zone_high, [zone_low, zone_high], "positive width", reclaim.t))
                    if zone_low >= zone_high or stop <= 0:
                        results.append({"status": "NO DISCOUNT OVERLAP", "checks": checks+tests, "setup": None, "time": reclaim.t})
                        continue
                    setup = {"key": key, "ob_ms": block.t, "violation_ms": bar.t,
                             "ssl": ssl, "sweep_ms": candles[s].t, "sweep_low": candles[s].l,
                             "leg_low": leg_low, "leg_high": leg_high, "reclaim_ms": reclaim.t,
                             "reclaim_end": reclaim.end, "reclaim_close": reclaim.c,
                             "expires_ms": bar.end+rules.setup_lifetime_bars*H4,
                             "zone_low": zone_low, "zone_high": zone_high, "equilibrium": equilibrium,
                             "invalidation": block.o, "stop": stop, "atr": atr[j],
                             "rvol": rv, "checks": checks+tests}
                    # A later reclaim can share this sweep. Keep scanning so
                    # pending setups are superseded by the newest qualification.
                    if best_for_block is None or reclaim.t >= best_for_block["time"]:
                        best_for_block = {"status": "WAITING FOR 15M RETEST", "checks": setup["checks"], "setup": setup, "time": reclaim.t}
            if best_for_block:
                results.append(best_for_block)
            break  # One violation defines this block.
    qualified = [x for x in results if x["setup"] is not None]
    if qualified:
        return max(qualified, key=lambda x: x["time"])
    return max(results, key=lambda x: (sum(c["status"] == "pass" for c in x["checks"]), x["time"])) if results else empty


def ltf_structure_evidence(candles, index, lookback):
    """Evaluate the price conditions once, retaining failed candidate values."""
    bar = candles[index]
    previous_high = max(b.h for b in candles[index-lookback:index]) if index >= lookback else None
    close_change = bar.c-bar.o
    close_location = location(bar)
    evidence = check("trigger", f"Bullish 15M break above prior {lookback} highs and valid confirmation age",
                     previous_high is not None and close_change > 0 and bar.c > previous_high and close_location >= 0.6,
                     {"open": bar.o, "high": bar.h, "low": bar.l, "close": bar.c,
                      "close_change": close_change, "close_location": close_location},
                     {"close_above": previous_high, "close_change_above": 0, "close_location_min": 0.6}, bar.t)
    if index < lookback:
        evidence["measured"]["prior_bars"] = index
        evidence["required"]["prior_bars_min"] = lookback
    return evidence


def ltf_scan(candles, setup, rules, now):
    evidence = copy.deepcopy(setup["checks"])
    result = {"status": "WAITING FOR 15M RETEST", "checks": evidence, "trigger": None, "failed": False}
    if now > setup["expires_ms"]:
        return {**result, "status": "SETUP EXPIRED", "failed": True}
    relevant = [i for i, b in enumerate(candles) if b.t > setup["reclaim_end"]]
    if not relevant or candles[0].t > setup["reclaim_end"] + 1:
        result["status"] = "WAITING FOR COMPLETE 15M HISTORY"
        return result
    retest = None
    trigger = None
    trigger_check = trigger_retest = None
    candidate_check = candidate_index = candidate_retest = None
    candidate_failed_break = False
    for i in relevant:
        bar = candles[i]
        if bar.l <= setup["stop"]:
            return {**result, "status": "SETUP STOP BREACHED", "failed": True}
        # After an observed live failure, only full subsequent candles can
        # establish a new retest and break. Earlier touches cannot revive it.
        if bar.t < setup.get("confirmation_reset_ms", 0):
            continue
        failed_break = trigger is not None and bar.c <= trigger["mss_level"]
        if failed_break:
            trigger = None
            trigger_check = trigger_retest = None
            retest = None
        overlap = bar.l <= setup["zone_high"] and bar.h >= setup["zone_low"]
        if overlap:
            retest = i
        candidate_check = ltf_structure_evidence(candles, i, rules.mss_lookback)
        candidate_index, candidate_retest = i, retest
        candidate_failed_break = failed_break
        if failed_break:
            continue  # A failure candle cannot also confirm a replacement break.
        if retest is None or i-retest > rules.ltf_retest_lifetime_bars or i < rules.mss_lookback:
            continue
        # A completed candle that touches and closes above prior highs is sufficient
        # bar-level evidence; no unobservable intrabar entry is simulated.
        if candidate_check["status"] == "pass":
            trigger_check, trigger_retest = candidate_check, retest
            trigger = {"key": f"{setup['key']}:{bar.t}", "bar_ms": bar.t, "end_ms": bar.end,
                       "close": bar.c, "retest_ms": candles[retest].t, "mss_level": candidate_check["required"]["close_above"],
                       "retest_end": candles[retest].end, "retest_low": candles[retest].l,
                       "fvg_context": i >= 2 and bar.l > candles[i-2].h}
    retest_valid = retest is not None and now-candles[retest].end <= rules.ltf_retest_lifetime_bars*M15
    fresh = (trigger is not None and now-trigger["end_ms"] <= rules.trigger_fresh_bars*M15
             and now-trigger["retest_end"] <= rules.ltf_retest_lifetime_bars*M15)
    displayed_retest = trigger_retest if fresh else retest
    retest_bar = candles[displayed_retest] if displayed_retest is not None else None
    retest_check = check("retest", "Completed 15M zone retest is still valid", retest_valid or fresh,
                         {"low": retest_bar.l if retest_bar else None, "high": retest_bar.h if retest_bar else None,
                          "age_bars": (now-retest_bar.end)/M15 if retest_bar else None},
                         {"zone": [setup["zone_low"], setup["zone_high"]], "max_age_bars": rules.ltf_retest_lifetime_bars},
                         retest_bar.t if retest_bar else None)
    retest_check["note"] = "The candle's range must overlap the retest zone."
    if trigger and displayed_retest != trigger_retest:
        retest_check["note"] += " This newer retest needs its own structure break; the break below uses its original retest."
    evidence.append(retest_check)

    displayed_check = copy.deepcopy(trigger_check if trigger else candidate_check)
    if displayed_check is None:
        displayed_check = check("trigger", "Waiting for a completed 15M confirmation candle", False,
                                None, {"close_change_above": 0, "close_location_min": 0.6})
        displayed_check["note"] = "Only full candles after the reclaim and any confirmation reset can establish a new break."
    else:
        paired_retest = trigger_retest if trigger else candidate_retest
        paired_bar = candles[paired_retest] if paired_retest is not None else None
        candle_end = trigger["end_ms"] if trigger else candles[candidate_index].end
        displayed_check["measured"].update(
            age_bars=(now-candle_end)/M15,
            retest_age_bars=(now-paired_bar.end)/M15 if paired_bar else None,
            retest_opened_ms=paired_bar.t if paired_bar else None)
        displayed_check["note"] = ("Recorded structure break and its original retest. " if trigger else
                                   "Latest completed 15M candidate and its preceding retest, if present. ")
        displayed_check["note"] += "Close location = (close − low) / (high − low). A retest must occur before or on the break candle."
        if not trigger and candidate_failed_break:
            displayed_check["measured"]["replacement_confirmation_allowed"] = False
            displayed_check["required"]["replacement_confirmation_allowed"] = True
            displayed_check["note"] += " This candle invalidated the previous break; a later candle must confirm a replacement."
    displayed_check["status"] = "pass" if fresh else "wait"
    displayed_check["required"].update(max_age_bars=rules.trigger_fresh_bars,
                                      max_retest_age_bars=rules.ltf_retest_lifetime_bars)
    evidence.append(displayed_check)
    if fresh:
        result.update(status="15M TRIGGER QUALIFIED", trigger=trigger)
    elif retest_valid:
        result["status"] = "WAITING FOR 15M STRUCTURE BREAK"
    elif retest is not None:
        result["status"] = "WAITING FOR A FRESH 15M RETEST"
    return result


def momentum_indicators(candles, rules):
    """Shared indicator inputs for entry qualification and held-position alerts."""
    closes = [b.c for b in candles]
    p = rules.momentum_roc_period
    roc = [100*(closes[i]/closes[i-p]-1) for i in range(p, len(closes))]
    return {"roc": roc, "signal": ema(roc, rules.momentum_signal_period),
            "rsi": rsi_series(closes, rules.momentum_rsi_period),
            "atr": atr_series(candles, rules.atr_period)}


def momentum_scan(candles, rules):
    minimum = max(250 if rules.momentum_require_trend else 0,
                  rules.momentum_roc_period+rules.momentum_signal_period*5,
                  rules.volume_period+1, rules.momentum_rsi_period+2)
    if len(candles) < minimum:
        return {"status": "WAITING FOR MOMENTUM HISTORY", "checks": [check("history", "Completed 4H history", False, len(candles), minimum)], "signal": None}
    p = rules.momentum_roc_period
    indicators = momentum_indicators(candles, rules)
    roc, signal = indicators["roc"], indicators["signal"]
    crosses = [i for i in range(1, len(roc)) if roc[i-1] <= signal[i-1] and roc[i] > signal[i]]
    # A cross can gain volume confirmation on either of the next two bars;
    # continued momentum above the signal is still required on this bar.
    recent = [i for i in crosses if len(roc)-1-i < rules.momentum_cross_window]
    rv = rvol(candles, len(candles)-1, rules.volume_period)
    rsi = indicators["rsi"][-1]
    bar = candles[-1]
    checks = [
        check("cross", "ROC crossed above EMA within confirmation window", bool(recent), len(roc)-1-recent[-1] if recent else None, rules.momentum_cross_window, bar.t),
        check("roc", "ROC positive and above its signal", roc[-1] > max(0, signal[-1]), {"roc_percent": roc[-1], "ema": signal[-1]}, "> 0 and > signal", bar.t),
        check("volume", f"Momentum volume / preceding {rules.volume_period}-bar mean", rv is not None and rv >= rules.momentum_rvol_min, rv, rules.momentum_rvol_min, bar.t),
        check("rsi", "Wilder RSI within configured interval", rules.momentum_rsi_min <= rsi <= rules.momentum_rsi_max, rsi, [rules.momentum_rsi_min, rules.momentum_rsi_max], bar.t),
        check("bullish", "Confirmation candle closes above open", bar.c > bar.o, bar.c-bar.o, "> 0", bar.t),
    ]
    if rules.momentum_require_trend:
        trend = trend_at(candles, len(candles)-1)
        checks.append(check("trend", "4H close > EMA200 and EMA50 > EMA200", trend is True, trend, True, bar.t))
    a = indicators["atr"][-1]
    record = None
    if passed(checks) and a and a > 0:
        cross_bar = candles[recent[-1]+p]
        record = {"key": f"momentum:{cross_bar.t}", "bar_ms": bar.t, "end_ms": bar.end,
                  "close": bar.c, "atr": a, "stop": bar.c-rules.momentum_stop_atr*a,
                  "rvol": rv, "checks": checks}
    return {"status": "MOMENTUM QUALIFIED" if record else "WAITING FOR MOMENTUM", "checks": checks, "signal": record}


def nearby_resistance(candles, entry, strength):
    highs = [bar.h for i, bar in enumerate(candles) if bar.h > entry
             and pivot(candles, i, strength, len(candles)-1, high=True)
             and not any(later.h >= bar.h for later in candles[i+1:])]
    return min(highs) if highs else None


def entry_plan(ask, bid, reference, stop, atr, rules, cash, equity, open_risk, candles):
    ask, bid, reference, stop, atr = [finite(x, "entry-plan value", 1e-15) for x in (ask, bid, reference, stop, atr)]
    if bid > ask:
        raise DataError("Inverted quote")
    spread = (ask-bid)/((ask+bid)/2)*10000
    if spread > rules.max_spread_bps:
        raise DataError("Spread exceeds configured limit")
    if ask > reference + rules.max_chase_atr*atr:
        raise DataError("Current ask exceeds maximum chase distance")
    if bid <= stop:
        raise DataError("Current bid has already reached the stop")
    # Paper entry uses observable ask plus assumed adverse slippage, not old close.
    entry = ask*(1+rules.slippage_rate)
    cost = entry*(1+rules.fee_rate)
    stop_proceeds = stop*(1-rules.slippage_rate)*(1-rules.fee_rate)
    risk_unit = cost-stop_proceeds
    if stop >= entry or risk_unit <= 0:
        raise DataError("Invalid long entry/stop ordering")
    risk_budget = min(equity*rules.risk_per_trade,
                      max(0, equity*rules.max_total_risk-open_risk),
                      max(0, equity-rules.paper_floor-open_risk))
    quantity = min(risk_budget/risk_unit, max(0, cash)/cost, equity*rules.max_allocation/cost)
    if quantity*entry < rules.minimum_notional:
        raise DataError("Insufficient available paper cash or portfolio risk budget")
    exit_factor = (1-rules.fee_rate)*(1-rules.slippage_rate)
    target1 = (cost+rules.target1_net_r*risk_unit)/exit_factor
    target2 = (cost+rules.target2_net_r*risk_unit)/exit_factor
    resistance = nearby_resistance(candles, entry, rules.pivot_strength)
    if rules.require_clear_target1 and resistance and resistance < target1:
        raise DataError("Nearest unconsumed pivot is below the required TP1")
    return {"entry": entry, "observed_ask": ask, "observed_bid": bid, "reference_close": reference,
            "stop": stop, "initial_stop": stop, "tp1": target1, "tp2": target2,
            "quantity": quantity, "remaining": quantity, "cost_per_unit": cost,
            "risk_per_unit": risk_unit, "initial_risk_usd": quantity*risk_unit,
            "allocation_usd": quantity*cost, "net_r1": rules.target1_net_r, "net_r2": rules.target2_net_r,
            "fee_rate": rules.fee_rate, "slippage_rate": rules.slippage_rate,
            "nearby_resistance": resistance, "resistance_before_tp1": bool(resistance and resistance < target1),
            "spread_bps": spread, "atr": atr}
