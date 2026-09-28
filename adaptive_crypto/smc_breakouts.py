"""Optional closed-entry-candle sweep/reclaim breakouts, without a pullback."""
from .core import check
from .smc import swings


def sweep_key(setup, rules):
    """Share consumption with a later HTF interpretation of the same sweep."""
    stamp = setup.get("sweep_ms")
    if stamp is None:
        return setup["key"]
    interval = rules.smc_setup_minutes * 60000
    return f"smc-sweep:{setup['side']}:{stamp // interval * interval}"


def breakout_entries(high, low, rules, side):
    if not rules.smc_breakout_entry or not high or not low or (side == "short" and rules.market_mode == "spot"):
        return []
    long = side == "long"
    sign = 1 if long else -1
    high_points = swings(high, rules.smc_pivot_strength)
    low_points = swings(low, rules.smc_pivot_strength)
    last = low[-1]
    entries = []
    for i in range(max(0, len(low)-rules.smc_breakout_window_bars), len(low)):
        bar = low[i]
        if low[0].t > bar.t // (rules.smc_setup_minutes*60000) * (rules.smc_setup_minutes*60000):
            continue  # Missing early entry bars could conceal a prior sweep.
        pools = []
        for point in high_points:
            if point["high"] == long or point["known_ms"] >= bar.t:
                continue
            price = point["price"]
            if not (bar.l < price if long else bar.h > price):
                continue
            pivot_end = high[point["index"]].end
            prior = [b for b in high if pivot_end < b.t and b.end < bar.t]
            prior.extend(b for b in low[:i] if b.t > pivot_end)
            if not any(b.l < price if long else b.h > price for b in prior):
                pools.append(point)
        if not pools:
            continue
        pool = max(pools, key=lambda p: p["bar_ms"])
        structure = [p for p in low_points if p["high"] == long and p["known_ms"] < bar.t
                     and not any(sign*(b.c-p["price"]) > 0 for b in low[p["index"]+1:i])]
        if not structure:
            continue
        point = max(structure, key=lambda p: p["bar_ms"])
        # The first combined reclaim/break is the event. Later closes above
        # the level cannot refresh it after a restart or an execution rejection.
        ready = next((j for j in range(i, len(low))
                      if sign*(low[j].c-pool["price"]) > 0 and sign*(low[j].c-point["price"]) > 0), None)
        if ready != len(low)-1:
            continue
        if sign*(last.c-last.o) <= 0:
            continue
        returned = next(j for j in range(i, ready+1) if sign*(low[j].c-pool["price"]) > 0)
        origin = min(range(i, returned+1), key=lambda j: low[j].l) if long else max(range(i, returned+1), key=lambda j: low[j].h)
        swing = low[origin].l if long else low[origin].h
        if any(b.l < swing if long else b.h > swing for b in low[returned+1:ready+1]):
            continue
        key = f"smc-breakout:{side}:{bar.t}"
        checks = [
            check("smc_sweep", f"Confirmed {rules.smc_setup_minutes}M liquidity swept on completed {rules.smc_entry_minutes}M candles", True,
                  {"liquidity_price": pool["price"], "sweep_extreme": swing, "return_close": last.c},
                  {"return_above" if long else "return_below": pool["price"]}, bar.t),
            check("smc_breakout", f"{rules.smc_entry_minutes}M close reclaims liquidity and breaks the pre-sweep swing", True,
                  last.c, {"close_above" if long else "close_below": point["price"]}, last.t),
            check("smc_breakout_entry", "Enter on a fresh executable quote after confirmation", True,
                  {"reference_price": last.c}, "No order-block revisit or gap-midpoint pullback required", last.t),
        ]
        setup = {"key": key, "kind": "breakout", "side": side, "sweep_ms": bar.t,
                 "bos_ms": last.t, "bos_end": last.end, "liquidity": pool["price"],
                 "zone_low": min(b.l for b in low[i:ready+1]),
                 "zone_high": max(b.h for b in low[i:ready+1]), "checks": checks}
        entries.append({"key": key, "setup_key": key, "side": side, "method": "breakout",
                        "execution": "market", "limit": last.c, "reference_price": last.c,
                        "breakout_level": max(pool["price"], point["price"]) if long else min(pool["price"], point["price"]),
                        "swing": swing, "swing_ms": low[origin].t, "signal_end": last.end,
                        "signal_ms": last.t, "checks": checks.copy(), "setup": setup})
    return entries
