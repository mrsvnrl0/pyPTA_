"""Independent 4H structure: confirmed 2/2 pivots and close-confirmed protected levels."""
import copy

from .core import H4, DataError, check, pivot, validate_candles

VERSION = "protected-4h-2x2-v1"


def advance_structure(candles, now, previous=None, error=None):
    """Return (bounded durable state, reset). Process each closed bar only once.

    Latest pivots and protected levels are distinct. The latter survive minor
    pivots and rolling feed windows until replaced by a BOS or marked breached.
    """
    previous = previous or {}
    try:
        if error:
            raise DataError(error)
        validate_candles(candles, H4, now, 5)
        if candles[-1].t != (now // H4 - 1) * H4:
            raise DataError("Waiting for the latest completed 4H structure candle")
    except (DataError, TypeError, AttributeError) as exc:
        return {**copy.deepcopy(previous), "error": str(exc), "expires_ms": now}, True

    cursor = previous.get("bar_ms")
    overlap = next((b for b in candles if b.t == cursor), None)
    reusable = (previous.get("version") == VERSION and cursor is not None
                and candles[3].t <= cursor <= candles[-1].t
                and (overlap is None or [overlap.o, overlap.h, overlap.l, overlap.c] == previous.get("last_ohlc")))
    state = copy.deepcopy(previous) if reusable else {
        "version": VERSION, "interval_ms": H4, "pivot_strength": 2,
        "swing_high": None, "swing_low": None, "protected_high": None,
        "protected_low": None, "last_break": None, "last_breach": None,
        "direction": "neutral", "bar_ms": None, "sweeps": []}
    for i, bar in enumerate(candles):
        if reusable and bar.t <= cursor:
            continue
        state["sweeps"] = []
        # A pivot becomes observable at the close of its second following bar.
        if i >= 4:
            for high, key in ((True, "swing_high"), (False, "swing_low")):
                j = i - 2
                if pivot(candles, j, 2, i, high=high):
                    between = candles[j+1:i+1]
                    origin = min(between, key=lambda b: b.l) if high else max(between, key=lambda b: b.h)
                    state[key] = {"price": candles[j].h if high else candles[j].l,
                                  "bar_ms": candles[j].t, "known_ms": bar.end, "broken_ms": None,
                                  "origin_price": origin.l if high else origin.h, "origin_ms": origin.t}
        # Detect a breach of the existing protected level before a new BOS can
        # replace it on this same candle. Wicks alone never generate a breach.
        for high, key in ((True, "protected_high"), (False, "protected_low")):
            level = state[key]
            if not level or level["broken_ms"] is not None:
                continue
            crossed = bar.c > level["price"] if high else bar.c < level["price"]
            if crossed:
                level["broken_ms"] = bar.end
                state["last_breach"] = {
                    "kind": key, "level": level["price"], "level_ms": level["bar_ms"],
                    "protected_ms": level["protected_ms"], "direction": "bullish" if high else "bearish",
                    "bar_ms": bar.t, "end_ms": bar.end, "close": bar.c}
            elif (bar.h > level["price"] and bar.c < level["price"]) if high else (bar.l < level["price"] and bar.c > level["price"]):
                state["sweeps"].append({"kind": key, "level": level["price"]})
        for high, key in ((True, "swing_high"), (False, "swing_low")):
            point = state[key]
            if not point:
                continue
            extreme = bar.l if high else bar.h
            if (extreme < point["origin_price"]) if high else (extreme > point["origin_price"]):
                point.update(origin_price=extreme, origin_ms=bar.t)
            if point["broken_ms"] is not None:
                continue
            crossed = bar.c > point["price"] if high else bar.c < point["price"]
            if crossed:
                point["broken_ms"] = bar.end
                state["direction"] = "bullish" if high else "bearish"
                state["last_break"] = {"direction": state["direction"], "level": point["price"],
                                       "pivot_ms": point["bar_ms"], "bar_ms": bar.t,
                                       "end_ms": bar.end, "close": bar.c}
                state["protected_low" if high else "protected_high"] = {
                    "price": point["origin_price"], "bar_ms": point["origin_ms"],
                    "protected_ms": bar.end, "break_level": point["price"], "broken_ms": None}
            elif (bar.h > point["price"] and bar.c < point["price"]) if high else (bar.l < point["price"] and bar.c > point["price"]):
                state["sweeps"].append({"kind": key, "level": point["price"]})
        state.update(bar_ms=bar.t, end_ms=bar.end, close=bar.c,
                     last_ohlc=[bar.o, bar.h, bar.l, bar.c])
    state.update(error=None, expires_ms=state["end_ms"]+H4+1)
    return state, not reusable


def qualification_checks(structure, side):
    """Visible context checks; these never gate SMC entries or NN orders."""
    structure = structure or {}
    valid = not structure.get("error") and structure.get("end_ms") is not None
    high, low = structure.get("swing_high"), structure.get("swing_low")
    key = "protected_low" if side == "long" else "protected_high"
    level = structure.get(key)
    checks = [
        check("structure_4h_pivots", "4H confirmed swings · context",
              bool(valid and high and low),
              {"swing_high": high["price"] if high else None, "swing_low": low["price"] if low else None},
              "2 candles left / 2 right; both following candles closed", candle=structure.get("bar_ms")),
        check("structure_4h_protected", "4H " + key.replace("_", " ") + " intact · context",
              bool(valid and level and level["broken_ms"] is None),
              {"protected_level": level["price"] if level else None,
               "close": structure.get("close"),
               "state": "Waiting for current 4H candles" if not valid else "Waiting for a qualifying 4H break" if not level else
                        "Broken by a completed close" if level["broken_ms"] is not None else "Intact"},
              "Close must not break " + ("below the protected low" if side == "long" else "above the protected high"),
              candle=structure.get("bar_ms"))]
    for item in checks:
        item["note"] = structure.get("error") or "Separate 4H context; does not change SMC entry qualification or NN paper orders."
    return checks
