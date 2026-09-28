"""Closed-candle interpretation of the Smart Risk sweep / OB / FVG model.

Source and deliberately explicit automation choices: video_strategy_audit/rules.md.
All returned timestamps refer to when evidence was observable, not hindsight.
"""
from __future__ import annotations

import copy
from .core import atr_series, check, pivot


def swings(candles, strength):
    points = []
    for i in range(strength, len(candles)-strength):
        for high in (False, True):
            if pivot(candles, i, strength, len(candles)-1, high=high):
                points.append({"index": i, "price": candles[i].h if high else candles[i].l,
                               "high": high, "bar_ms": candles[i].t,
                               "known_ms": candles[i+strength].end})
    return points


def structure_readings(candles, strength):
    """A directional close break persists until an opposing close break."""
    points = swings(candles, strength)
    cursor, latest, broken = 0, {}, set()
    direction, last_break = "neutral", None
    readings = []
    for i, bar in enumerate(candles):
        while cursor < len(points) and points[cursor]["known_ms"] < bar.t:
            point = points[cursor]
            latest[point["high"]] = point
            cursor += 1
        event = None
        for high in (True, False):
            point = latest.get(high)
            if point is None or (high, point["bar_ms"]) in broken:
                continue
            if (bar.c > point["price"]) if high else (bar.c < point["price"]):
                broken.add((high, point["bar_ms"]))
                direction = "bullish" if high else "bearish"
                indices = range(point["index"]+1, i+1)
                origin = min(indices, key=lambda j: candles[j].l) if high else max(indices, key=lambda j: candles[j].h)
                event = {"direction": direction, "level": point["price"], "pivot_ms": point["bar_ms"],
                         "bar_ms": bar.t, "end_ms": bar.end, "index": i,
                         "origin_index": origin, "origin_ms": candles[origin].t}
                last_break = event
        if i >= 2*strength+1:
            readings.append({"direction": direction, "bar_ms": bar.t, "end_ms": bar.end, "close": bar.c,
                             "interval_ms": bar.interval, "basis": "structure", "pivot_strength": strength,
                             "swing_high": latest.get(True, {}).get("price"),
                             "swing_low": latest.get(False, {}).get("price"),
                             "break_level": last_break["level"] if last_break else None,
                             "break_ms": last_break["bar_ms"] if last_break else None, "event": event})
    return readings


def fair_value_gaps(candles):
    gaps = []
    for i in range(2, len(candles)):
        first, third = candles[i-2], candles[i]
        if third.l > first.h:
            gaps.append({"index": i, "first_index": i-2, "low": first.h, "high": third.l,
                         "direction": "bullish", "bar_ms": third.t, "end_ms": third.end})
        elif third.h < first.l:
            gaps.append({"index": i, "first_index": i-2, "low": third.h, "high": first.l,
                         "direction": "bearish", "bar_ms": third.t, "end_ms": third.end})
    return gaps


def liquidity_choices(candles, strength, side, entry, known_ms, observed_candles=()):
    choices = []
    for point in swings(candles, strength):
        high = side == "long"
        if point["high"] != high or point["known_ms"] > known_ms:
            continue
        if not (point["price"] > entry if high else point["price"] < entry):
            continue
        later = [b for b in candles[point["index"]+1:] if b.end <= known_ms]
        # A completed entry-timeframe candle can consume the target while its
        # enclosing setup-timeframe candle is still forming. Do not count the
        # lower bars inside the pivot candle itself as subsequent liquidity.
        pivot_end = candles[point["index"]].end
        later.extend(b for b in observed_candles if b.t > pivot_end and b.end <= known_ms)
        if any(b.h >= point["price"] if high else b.l <= point["price"] for b in later):
            continue
        choices.append(point)
    return sorted(choices, key=lambda p: abs(p["price"]-entry))


def opposing_liquidity(candles, strength, side, entry, known_ms, observed_candles=()):
    return next(iter(liquidity_choices(candles, strength, side, entry, known_ms, observed_candles)), None)


def higher_setups(candles, rules, side):
    """Sweep a previously untaken pivot, return inside, then close through BOS."""
    strength = rules.smc_pivot_strength
    points = swings(candles, strength)
    long = side == "long"
    setups, identities = [], set()
    for i, bar in enumerate(candles):
        available = [p for p in points if p["known_ms"] < bar.t]
        liquidity = [p for p in available if p["high"] != long
                     and not any(b.l < p["price"] if long else b.h > p["price"] for b in candles[p["index"]+1:i])]
        swept = [p for p in liquidity if bar.l < p["price"] if long] if long else [p for p in liquidity if bar.h > p["price"]]
        if not swept:
            continue
        pool = max(swept, key=lambda p: p["bar_ms"])
        opposite = [p for p in available if p["high"] == long
                    and not any(b.c > p["price"] if long else b.c < p["price"]
                                for b in candles[p["index"]+1:i])]
        if not opposite:
            continue
        structure = max(opposite, key=lambda p: p["bar_ms"])
        returned = next((j for j in range(i, min(len(candles), i+rules.smc_reversal_bars+1))
                         if (candles[j].c > pool["price"] if long else candles[j].c < pool["price"])), None)
        if returned is None:
            continue
        bos = next((j for j in range(returned, len(candles))
                    if (candles[j].c > structure["price"] if long else candles[j].c < structure["price"])), None)
        if bos is None:
            continue
        origin = min(range(i, returned+1), key=lambda j: candles[j].l) if long else max(range(i, returned+1), key=lambda j: candles[j].h)
        opposite_candles = [j for j in range(max(0, structure["index"]), origin+1)
                            if (candles[j].c < candles[j].o if long else candles[j].c > candles[j].o)]
        if not opposite_candles:
            continue
        ob_index = opposite_candles[-1]
        ob = candles[ob_index]
        # The post-return expansion cannot first make another deeper extreme.
        extreme = candles[origin].l if long else candles[origin].h
        if any(b.l < extreme if long else b.h > extreme for b in candles[returned+1:bos+1]):
            continue
        # The sweep candle's own close can depart and confirm BOS. Its wick
        # sequence is not used to invent an entry before that close.
        departed = next((j for j in range(origin, bos+1)
                         if (candles[j].c > ob.h if long else candles[j].c < ob.l)), None)
        if departed is None or any(b.l <= ob.h and b.h >= ob.l for b in candles[departed+1:bos+1]):
            continue  # Already mitigated before it became an actionable POI.
        key = f"smc:{side}:{ob.t}:{candles[bos].t}"
        if key in identities:
            continue
        identities.add(key)
        checks = [
            check("smc_sweep", "Liquidity swept and price returned inside", True,
                  {"liquidity_price": pool["price"], "sweep_extreme": extreme, "return_close": candles[returned].c},
                  {"sweep_below" if long else "sweep_above": pool["price"],
                   "return_above" if long else "return_below": pool["price"]}, bar.t),
            check("smc_bos", f"{rules.smc_setup_minutes}M close through opposing swing structure", True,
                  candles[bos].c, {"close_above" if long else "close_below": structure["price"]}, candles[bos].t),
            check("smc_ob", "Fresh originating order block", True,
                  {"zone_low": ob.l, "zone_high": ob.h}, "First revisit after BOS", ob.t),
        ]
        setups.append({"key": key, "side": side, "ob_ms": ob.t, "bos_ms": candles[bos].t,
                       "bos_end": candles[bos].end, "zone_low": ob.l, "zone_high": ob.h,
                       "sweep_ms": bar.t, "liquidity": pool["price"], "checks": checks})
    return setups


def lower_entries(candles, setup, rules):
    """Return entries confirmed during the order block's first visit.

    The first visit may span multiple overlapping candles. After a completed
    candle lies entirely beyond the block in the reversal direction (its low
    above the block for a long, or high below it for a short), another overlap
    starts a second visit and closes the confirmation window. That return
    cannot reset the setup, even if its swing origin still points to the older
    extreme. Candidates confirmed before it remain subject to the ordinary
    midpoint/swing freshness checks over all later candles; no time expiry is
    inferred from the discretionary video.
    """
    long = setup["side"] == "long"
    direction = "bullish" if long else "bearish"
    evidence = copy.deepcopy(setup["checks"])
    result = {"status": f"WAITING FOR {rules.smc_entry_minutes}M ORDER-BLOCK REVISIT",
              "checks": evidence, "setup": setup, "entries": []}
    if not candles or candles[0].t > setup["bos_end"]+1:
        return {**result, "status": "WAITING FOR COMPLETE ENTRY-TIMEFRAME HISTORY"}
    relevant = [i for i, b in enumerate(candles) if b.t > setup["bos_end"]]
    touch = next((i for i in relevant if candles[i].l <= setup["zone_high"] and candles[i].h >= setup["zone_low"]), None)
    evidence.append(check("smc_retest", "First revisit of the originating order block", touch is not None,
                          {"zone_low": setup["zone_low"], "zone_high": setup["zone_high"]},
                          "Candle range overlaps the block after BOS", candles[touch].t if touch is not None else None))
    if touch is None:
        return result
    departed = next((i for i in range(touch+1, len(candles))
                     if (candles[i].l > setup["zone_high"] if long else candles[i].h < setup["zone_low"])), None)
    second_touch = next((i for i in range(departed+1, len(candles))
                         if candles[i].l <= setup["zone_high"] and candles[i].h >= setup["zone_low"]), None) if departed is not None else None
    confirmation_end = second_touch if second_touch is not None else len(candles)
    gaps = fair_value_gaps(candles[:confirmation_end])
    readings = structure_readings(candles[:confirmation_end], rules.smc_pivot_strength)
    shifts = [r["event"] for r in readings if r["event"] and r["direction"] == direction
              and r["event"]["index"] >= touch and r["event"]["origin_index"] >= touch]
    candidates = []
    if rules.smc_entry_method in {"both", "conservative"}:
        for shift in shifts:
            for gap in gaps:
                if gap["direction"] != direction or gap["first_index"] < shift["origin_index"]:
                    continue
                ready = max(shift["index"], gap["index"])
                if gap["index"] > shift["index"]:
                    leg = candles[shift["index"]+1:ready+1]
                    origin = candles[shift["origin_index"]]
                    if any(b.c <= shift["level"] if long else b.c >= shift["level"] for b in leg):
                        continue
                    if any(b.l < origin.l if long else b.h > origin.h for b in leg):
                        continue
                    if any(r["event"] and r["direction"] != direction
                           and shift["index"] < r["event"]["index"] <= ready for r in readings):
                        continue  # A later FVG belongs to a different reversal leg.
                # A midpoint revisit before MSS confirmation cannot be filled retrospectively.
                midpoint = (gap["low"]+gap["high"])/2
                if any(b.l <= midpoint if long else b.h >= midpoint for b in candles[gap["index"]+1:ready+1]):
                    continue
                candidates.append((ready, "conservative", gap, shift))
    if rules.smc_entry_method in {"both", "aggressive"}:
        for gap in gaps:
            if gap["direction"] == direction or candles[gap["first_index"]].t <= setup["bos_end"]:
                continue
            inversion = next((i for i in range(gap["index"]+1, confirmation_end)
                              if (candles[i].c > gap["high"] if long else candles[i].c < gap["low"])), None)
            if inversion is not None and inversion >= touch:
                candidates.append((inversion, "aggressive", gap, next((s for s in shifts if s["index"] == inversion), None)))
    evidence.append(check("smc_mss", f"{rules.smc_entry_minutes}M structure shift · conservative method",
                          bool(shifts), shifts[-1]["level"] if shifts else None,
                          "Close beyond the reversal swing; optional for aggressive entry",
                          shifts[-1]["bar_ms"] if shifts else None, context=rules.smc_entry_method == "aggressive"))
    identities = set()
    for ready, method, gap, shift in sorted(candidates, key=lambda c: (
            c[0], c[1] != "conservative", -(c[3]["index"] if c[3] else -1))):
        identity = (ready, method, gap["bar_ms"])
        if identity in identities:
            continue
        identities.add(identity)
        midpoint = (gap["low"]+gap["high"])/2
        origin_range = range(touch, ready+1)
        origin = min(origin_range, key=lambda i: candles[i].l) if long else max(origin_range, key=lambda i: candles[i].h)
        swing = candles[origin].l if long else candles[origin].h
        # Once the reversal swing is exceeded, this confirmation is no longer fresh.
        if any(b.l <= swing if long else b.h >= swing for b in candles[ready+1:]):
            continue
        if any(b.l <= midpoint if long else b.h >= midpoint for b in candles[ready+1:]):
            continue  # The first limit opportunity has already happened without this app.
        entry_checks = copy.deepcopy(evidence[:4])
        if method == "conservative":
            entry_checks.append(check("smc_mss", "Completed close confirms the structure shift", True,
                                      candles[shift["index"]].c,
                                      {"close_above" if long else "close_below": shift["level"]}, shift["bar_ms"]))
        else:
            entry_checks.append(check("smc_ifvg", "Opposing FVG inverted by a close through its far edge", True,
                                      candles[ready].c, {"close_above" if long else "close_below": gap["high"] if long else gap["low"]}, candles[ready].t))
            entry_checks.append(check("smc_mss_context", "Simultaneous MSS is additional confluence", bool(shift),
                                      bool(shift), "Optional for aggressive entry", candles[ready].t, context=True))
        entry_checks.append(check("smc_gap", "Limit entry at the confirmed gap midpoint", True,
                                  {"gap_low": gap["low"], "gap_high": gap["high"], "midpoint": midpoint},
                                  "(Gap low + gap high) / 2", gap["bar_ms"]))
        result["entries"].append({"key": f"{setup['key']}:{method}:{candles[ready].t}:{gap['bar_ms']}",
                                  "setup_key": setup["key"], "side": setup["side"], "method": method,
                                  "limit": midpoint, "gap_low": gap["low"], "gap_high": gap["high"],
                                  "swing": swing, "swing_ms": candles[origin].t, "signal_end": candles[ready].end,
                                  "signal_ms": candles[ready].t, "checks": entry_checks, "setup": setup})
    result["status"] = ("GAP CONFIRMED · LIMIT CANDIDATE" if result["entries"] else
                        "ORDER BLOCK ALREADY REVISITED · FIRST VISIT CONFIRMATION MISSED" if second_touch is not None else
                        "WAITING FOR FRESH FVG / INVERSE-FVG CONFIRMATION")
    return result


def scan(candles_high, candles_low, rules, side, consumed=()):
    from .smc_breakouts import breakout_entries, sweep_key
    setups = higher_setups(candles_high, rules, side)
    breakouts = breakout_entries(candles_high, candles_low, rules, side)
    setups.extend(e["setup"] for e in breakouts)
    results = []
    for setup in reversed(setups):
        if setup["key"] in consumed or sweep_key(setup, rules) in consumed:
            continue
        long = side == "long"
        later = [b for b in candles_high if b.t > setup["bos_end"]]
        if any(b.c < setup["zone_low"] if long else b.c > setup["zone_high"] for b in later):
            continue
        breakout = next((e for e in breakouts if e["setup_key"] == setup["key"]), None)
        result = ({"status": "BREAKOUT CONFIRMED · AWAITING FRESH QUOTE", "setup": setup,
                   "checks": copy.deepcopy(breakout["checks"]), "entries": [breakout]}
                  if breakout else lower_entries(candles_low, setup, rules))
        accepted = []
        for entry in result["entries"]:
            entry["sweep_key"] = sweep_key(setup, rules)
            target = opposing_liquidity(candles_high, rules.smc_pivot_strength, side,
                                        entry["limit"], entry["signal_end"], candles_low)
            if target is None:
                result["status"] = "WAITING FOR OPPOSING HIGHER-TIMEFRAME LIQUIDITY"
                result["checks"].append(check("smc_target", "Untaken opposing liquidity available", False, None, "Confirmed higher-timeframe swing"))
                continue
            if any(b.h >= target["price"] if long else b.l <= target["price"] for b in candles_low if b.t > entry["signal_end"]):
                result["status"] = "SETUP EXPIRED · TARGET LIQUIDITY ALREADY TAKEN"
                result["checks"].append(check("smc_target", "Opposing target remains untaken", False,
                                              target["price"], "Target not reached since confirmation"))
                continue
            stop_basis = entry["swing"] if rules.smc_stop_basis != "order_block" or breakout else setup["zone_low" if long else "zone_high"]
            buffer = stop_basis*rules.smc_stop_buffer_bps/10000
            if rules.smc_stop_basis == "atr":
                history = [b for b in candles_low if b.end <= entry["signal_end"]]
                a = atr_series(history, rules.atr_period)[-1]
                if a is None:
                    result["status"] = "WAITING FOR COMPLETED ATR HISTORY FOR THE CONFIGURED STOP"
                    continue
                buffer = rules.stop_buffer_atr*a
            stop = stop_basis-buffer if long else stop_basis+buffer
            # Take profit inside the anticipated sweep beyond the key high/low.
            # Freshness above still tests the raw liquidity, never this extension.
            target_price = target["price"]*(1+(1 if long else -1)*rules.smc_tp_sweep_buffer_bps/10000)
            if not (0 < stop < entry["limit"] < target["price"] < target_price if long else
                    0 < target_price < target["price"] < entry["limit"] < stop):
                result["status"] = "WAITING FOR VALID STRUCTURAL STOP AND TARGET"
                result["checks"].append(check("smc_stop", "Stop and target bracket the limit", False,
                                              {"stop_price": stop, "entry_price": entry["limit"], "target_price": target_price},
                                              "Stop below entry below target" if long else "Target below entry below stop"))
                continue
            entry.update(stop=stop, target=target_price, target_ms=target["bar_ms"],
                         target_liquidity_price=target["price"], target_sweep_buffer_bps=rules.smc_tp_sweep_buffer_bps,
                         gross_r=abs(target_price-entry["limit"])/abs(entry["limit"]-stop))
            entry["checks"].extend([
                check("smc_target", "Take profit on a sweep beyond opposing liquidity", True,
                      {"liquidity_price": target["price"], "sweep_buffer_bps": rules.smc_tp_sweep_buffer_bps,
                       "target_price": target_price},
                      f"Confirmed {rules.smc_setup_minutes}M {'high × (1 + buffer / 10000)' if long else 'low × (1 − buffer / 10000)'}; exit at sweep price",
                      target["bar_ms"]),
                check("smc_stop", "Configured structural stop", True,
                      {"swing_price": stop_basis, "stop_price": stop},
                      f"{'swing' if breakout and rules.smc_stop_basis == 'order_block' else rules.smc_stop_basis} with {'ATR' if rules.smc_stop_basis == 'atr' else str(rules.smc_stop_buffer_bps)+' bps'} buffer", entry["swing_ms"]),
            ])
            accepted.append(entry)
        result["entries"] = accepted
        if accepted:
            result.update(status="BREAKOUT CONFIRMED · AWAITING FRESH QUOTE" if breakout else "SETUP QUALIFIED · AWAITING LIMIT ORDER",
                          checks=copy.deepcopy(accepted[0]["checks"]))
        results.append(result)
    actionable = [r for r in results if r["entries"]]
    if actionable:
        return max(actionable, key=lambda r: r["setup"]["bos_end"])
    return max(results, key=lambda r: r["setup"]["bos_end"]) if results else {
        "status": "WAITING FOR LIQUIDITY SWEEP AND STRUCTURE BREAK", "setup": None, "entries": [],
        "checks": [check("smc_setup", f"{rules.smc_setup_minutes}M sweep → reversal → BOS → fresh order block", False,
                         None, "Closed-candle sequence; no ATR, RSI, volume or EMA entry gate")]}
