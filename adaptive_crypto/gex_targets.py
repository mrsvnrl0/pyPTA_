"""Point-in-time GEX/SMC confluence and shared liquidity-target selection.

GEX ranks existing untaken SMC targets. It never invents a target or moves a
saved one. The numerical proximity/basis limits are explicit model choices.
"""
import copy
from dataclasses import replace

from .core import DataError, finite, validate_candles
from .smc import higher_setups, liquidity_choices, structure_readings, swings
from .take_profit_alerts import fresh_quote

MAX_AGE_MS = 300000


def validate_target_mode(mode):
    if mode not in ("smc", "gex_smc"):
        raise DataError("Choose SMC or SMC + GEX for the target method")
    return mode


def rules_for_target(rules, mode):
    return replace(rules, smc_gex_targets=validate_target_mode(mode) == "gex_smc")


def near(a, b, bps):
    return abs(a-b)/b*10000 <= bps+1e-9


def validate_selection(selection, price, side):
    """Legacy records have no selection metadata; new records retain its provenance."""
    if selection is None:
        return
    if (not isinstance(selection, dict) or selection.get("method") not in {"smc", "gex_smc"}
            or selection.get("liquidity_price") != price or type(selection.get("selected_ms")) is not int
            or selection["selected_ms"] < 0 or not isinstance(selection.get("matches"), list)):
        raise DataError("Invalid saved GEX/SMC target selection")
    if selection["method"] == "smc":
        if selection["matches"]:
            raise DataError("SMC fallback cannot claim GEX alignment")
        return
    stamp = finite(selection.get("gex_asof_ms"), "saved GEX observation", 0)
    if not 0 <= selection["selected_ms"]-stamp < MAX_AGE_MS or not selection["matches"]:
        raise DataError("Saved GEX evidence was not current at target selection")
    for match in selection["matches"]:
        poi = match.get("poi", {})
        if not match.get("eligible") or poi.get("side") != side:
            raise DataError("Saved GEX match has the wrong target direction")
        level = finite(match.get("gex_price"), "saved GEX level", 1e-15)
        if poi.get("kind") in {"bsl", "ssl"}:
            aligned = price == poi.get("price") and near(price, level, selection["alignment_bps"])
        elif poi.get("kind") in {"premium_ob", "discount_ob"}:
            aligned = poi["low"] <= price <= poi["high"] and poi["low"] <= level <= poi["high"]
        elif poi.get("kind") == "mss":
            aligned = price > level if side == "long" else price < level
        else:
            aligned = False
        if not aligned:
            raise DataError("Saved GEX match does not support the recorded liquidity target")


def context(high, low, quote, rules, profile, now, symbol):
    result = {"enabled": rules.smc_gex_targets, "ready": False, "selected_ms": now,
              "alignment_bps": rules.smc_gex_alignment_bps, "pois": [], "matches": [],
              "reason": "GEX target selection disabled", "regime": None}
    # Build SMC POIs even with unavailable GEX, so the map explains the fallback.
    try:
        quote = fresh_quote(quote, now)
        if not quote:
            raise DataError("Waiting for a fresh executable quote")
        for bars, minutes in ((high, rules.smc_setup_minutes), (low, rules.smc_entry_minutes)):
            validate_candles(bars, minutes*60000, now, 1, fresh=False)
            if bars[-1].t != (now//(minutes*60000)-1)*minutes*60000:
                raise DataError("Waiting for current completed SMC candles")
        mark = (quote["bid"]+quote["ask"])/2
        for side in ("long", "short"):
            for p in liquidity_choices(high, rules.smc_pivot_strength, side,
                                       quote["ask" if side == "long" else "bid"], now, low):
                result["pois"].append({"kind": "bsl" if side == "long" else "ssl",
                    "label": "Buy-side liquidity" if side == "long" else "Sell-side liquidity",
                    "price": p["price"], "pivot_ms": p["bar_ms"], "side": side})
        points = swings(high, rules.smc_pivot_strength)
        upper = next((p for p in reversed(points) if p["high"]), None)
        lower = next((p for p in reversed(points) if not p["high"]), None)
        equilibrium = (upper["price"]+lower["price"])/2 if upper and lower else None
        for side in ("short", "long"):
            for setup in higher_setups(high, rules, side):
                later = [b for b in high+low if b.t > setup["bos_end"] and b.end <= now]
                if any(b.l <= setup["zone_high"] and b.h >= setup["zone_low"] for b in later):
                    continue
                if any(b.c > setup["zone_high"] if side == "short" else b.c < setup["zone_low"] for b in later):
                    continue
                premium = equilibrium is not None and setup["zone_low"] >= equilibrium
                discount = equilibrium is not None and setup["zone_high"] <= equilibrium
                if not (premium if side == "short" else discount):
                    continue
                result["pois"].append({"kind": "premium_ob" if side == "short" else "discount_ob",
                    "label": "Premium order block" if side == "short" else "Discount order block",
                    "low": setup["zone_low"], "high": setup["zone_high"],
                    "price": (setup["zone_low"]+setup["zone_high"])/2, "equilibrium": equilibrium,
                    "pivot_ms": setup["ob_ms"], "side": "long" if side == "short" else "short"})
        readings = structure_readings(low, rules.smc_pivot_strength)
        if len(readings) > 1 and readings[-1].get("event"):
            reading, previous = readings[-1], readings[-2]
            event = reading["event"]
            # A continuation BOS or a neutral baseline is not a market structure shift.
            if previous["direction"] in {"bullish", "bearish"} and previous["direction"] != reading["direction"]:
                result["pois"].append({"kind": "mss", "label": "Confirmed market structure shift",
                    "price": event["level"], "pivot_ms": event["bar_ms"], "end_ms": event["end_ms"],
                    "side": "long" if reading["direction"] == "bullish" else "short",
                    "previous_close": low[-2].c, "close": low[-1].c})
        if not rules.smc_gex_targets:
            return result
        if not isinstance(profile, dict) or profile.get("status") != "ready" or profile.get("stale"):
            raise DataError("GEX unavailable or stale; nearest SMC liquidity used")
        stamp = finite(profile.get("asof_ms"), "GEX timestamp", 0)
        calculated = finite(profile.get("calculated_ms"), "GEX calculation time", 0)
        expiry = finite(profile.get("expires_ms"), "GEX expiry", 0)
        if not 0 <= now-stamp < MAX_AGE_MS or not 0 <= now-calculated < MAX_AGE_MS or expiry <= now:
            raise DataError("GEX expired or future-dated; nearest SMC liquidity used")
        if profile.get("base") != symbol.split("/")[0] or symbol.split("/")[-1] != "USD":
            raise DataError("GEX asset does not match the SMC market")
        spot = finite(profile.get("spot"), "GEX index", 1e-15)
        basis = abs(spot/mark-1)*10000
        result["basis_bps"] = basis
        if basis > rules.smc_gex_max_basis_bps:
            raise DataError("Deribit/Kraken basis exceeds the configured limit; nearest SMC liquidity used")
        regime = profile.get("regime")
        if regime not in {"positive", "negative", "neutral"}:
            raise DataError("Invalid GEX regime")
        levels = {"call": (profile.get("call_wall") or {}).get("strike"),
                  "put": (profile.get("put_wall") or {}).get("strike"), "flip": profile.get("gamma_flip")}
        for value in levels.values():
            if value is not None:
                finite(value, "GEX level", 1e-15)
        result.update(ready=True, regime=regime, gex_asof_ms=int(stamp),
                      valid_until_ms=int(min(stamp+MAX_AGE_MS, calculated+MAX_AGE_MS, expiry,
                                            quote["asof_ms"]+30000, low[-1].end+low[-1].interval)),
                      reason="No eligible GEX/SMC alignment; nearest SMC liquidity used")
        for poi in result["pois"]:
            key = "flip" if poi["kind"] == "mss" else "call" if poi["side"] == "long" else "put"
            level = levels[key]
            if level is None:
                continue
            aligned = poi["low"] <= level <= poi["high"] if "low" in poi else near(poi["price"], level, rules.smc_gex_alignment_bps)
            if not aligned:
                continue
            if key == "flip":
                crossed = (poi["previous_close"] <= level < poi["close"] if poi["side"] == "long"
                           else poi["previous_close"] >= level > poi["close"])
                eligible = crossed
                reason = "MSS and completed close cross the gamma flip" if crossed else "MSS near flip; no confirmed flip crossing"
            else:
                eligible = regime == "positive" and (level > quote["ask"] if key == "call" else level < quote["bid"])
                reason = "Positive-gamma wall aligns with SMC POI" if eligible else "Wall alignment is context only in this regime or price position"
            result["matches"].append({"key": key, "gex_price": level, "poi": copy.deepcopy(poi),
                                       "eligible": eligible, "reason": reason})
        if any(match["eligible"] for match in result["matches"]):
            result["reason"] = "Eligible GEX/SMC POI alignment available for prospective target selection"
    except (DataError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        result.update(ready=False, reason=str(exc), matches=[])
    return result


def select_target(high, low, rules, side, reference, known_ms, confluence=None):
    choices = liquidity_choices(high, rules.smc_pivot_strength, side, reference, known_ms, low)
    if not choices:
        return None
    chosen, matches = choices[0], []
    info = {"method": "smc", "reason": "Nearest untaken SMC liquidity", "matches": []}
    if rules.smc_gex_targets:
        info["reason"] = (confluence or {}).get("reason", "GEX unavailable; nearest SMC liquidity used")
        if confluence and confluence.get("ready") and confluence.get("selected_ms") == known_ms:
            info["reason"] = "No eligible GEX alignment for this target; nearest SMC liquidity used"
            ranked = []
            for point in choices:
                evidence = []
                for match in confluence["matches"]:
                    poi = match["poi"]
                    if not match["eligible"] or poi["side"] != side:
                        continue
                    if poi["kind"] in {"bsl", "ssl"}:
                        aligned = point["bar_ms"] == poi["pivot_ms"] and point["price"] == poi["price"]
                    elif poi["kind"] == "mss":
                        aligned = point["price"] > match["gex_price"] if side == "long" else point["price"] < match["gex_price"]
                    else:
                        aligned = poi["low"] <= point["price"] <= poi["high"]
                    if aligned:
                        evidence.append(match)
                if evidence:
                    ranked.append((point, evidence))
            if ranked:
                # Nearest qualifying confluence wins; do not inflate a distant target with a score.
                chosen, matches = ranked[0]
                info.update(method="gex_smc", reason="Nearest untaken SMC target with eligible GEX confluence",
                            matches=copy.deepcopy(matches), gex_asof_ms=confluence["gex_asof_ms"],
                            regime=confluence["regime"], alignment_bps=rules.smc_gex_alignment_bps)
    info.update(selected_ms=known_ms, nearest_smc_price=choices[0]["price"], liquidity_price=chosen["price"])
    return {**chosen, "target_selection": info}
