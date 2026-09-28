"""Apply qualification decisions and position monitoring in one ledger transaction."""
from __future__ import annotations

import copy
from .core import H4, M15, DataError, check, finite, rvol, safe_error, utc, validate_candles
from .ledger import monitor_positions, open_trade, portfolio
from .strategy import reclaim_scan, ltf_scan, momentum_scan, entry_plan
from .paper_neural import entry_confirmation, monitor_exits, trade_progress, decision


def reclaim_failure(setup, bid, candles4, candles15, now):
    """Apply identical terminal checks to saved and newly discovered setups."""
    if now > setup["expires_ms"]:
        return "SETUP EXPIRED"
    if ((bid is not None and bid <= setup["stop"])
            or any(b.end > setup["reclaim_end"] and b.l <= setup["stop"] for b in candles4)
            or any(b.t > setup["reclaim_end"] and b.l <= setup["stop"] for b in candles15)):
        return "SETUP STOP BREACHED"
    if any(b.end > setup["reclaim_end"] and b.c < setup["invalidation"] for b in candles4):
        return "SETUP 4H INVALIDATED"
    return None


def retire_reclaim(record, setup, reason):
    if setup["key"] not in record["consumed"]:
        record["consumed"].append(setup["key"])
    record["pending"] = None
    # Retire every block interpretation of this completed reclaim candle.
    # Otherwise consuming its selected key can expose the same candle against
    # an older block, resurrecting the obsolete zone under a different key.
    record["reclaim_floor_ms"] = max(record.get("reclaim_floor_ms", 0), setup["reclaim_end"]+1)
    record["last_reclaim_result"] = reason


def momentum_stop_breached(watch, bid, candles4, candles15):
    return ((bid is not None and bid <= watch["stop"])
            or any(b.t > watch["end_ms"] and b.l <= watch["stop"] for b in candles4)
            or any(b.t > watch["end_ms"] and b.l <= watch["stop"] for b in candles15))


class Engine:
    def __init__(self, assets, rules, store):
        self.assets, self.rules, self.store = assets, rules, store

    def evaluate(self, name, quote, candles4, candles15, now, errors=None, neural_reading=None):
        rules = self.rules
        errors = dict(errors or {})
        # Each feed is checked separately so missing 15m data cannot block
        # momentum qualification, or missing OHLC data block a live stop.
        valid4, valid15, valid_quote = [], [], None
        try:
            valid4 = validate_candles(candles4, H4, now, max(rules.volume_period, rules.atr_period)+5)
        except (ValueError, TypeError) as exc:
            errors["4h"] = safe_error(exc)
        try:
            valid15 = validate_candles(candles15, M15, now, rules.mss_lookback+1)
        except (ValueError, TypeError) as exc:
            errors["15m"] = safe_error(exc)
        # The feed's short freshness grace is useful for monitoring/retries,
        # but it cannot authorize entry before the just-closed bar is present.
        current4 = bool(valid4) and valid4[-1].t == (now//H4-1)*H4
        current15 = bool(valid15) and valid15[-1].t == (now//M15-1)*M15
        if valid4 and not current4:
            errors["4h"] = "Waiting for latest completed 4H candle"
        if valid15 and not current15:
            errors["15m"] = "Waiting for latest completed 15M candle"
        try:
            if not isinstance(quote, dict) or errors.get("clock"):
                raise DataError("Quote is unavailable")
            bid, ask = finite(quote["bid"], "bid", 1e-15), finite(quote["ask"], "ask", 1e-15)
            age = now - finite(quote["asof_ms"], "quote time", 0)
            if ask < bid or not -2000 <= age <= 30000:
                raise DataError("Stale, future, or inverted quote")
            valid_quote = {**quote, "bid": bid, "ask": ask}
        except (ValueError, KeyError, TypeError) as exc:
            errors["quote"] = safe_error(exc)

        def advance(document):
            record = document["assets"][name]
            before = trade_progress(record)
            if not errors.get("clock"):
                monitor_positions(document, name, valid_quote["bid"] if valid_quote else None, valid4, valid15, now)
                if rules.combined:
                    monitor_exits(document, name, neural_reading, valid_quote, now, "legacy")
            exited = rules.combined and any(before.get(t["id"], (None,))[0] == "active" and t["status"] != "active" for t in record["trades"])
            pending = record["pending"]
            result = {"status": "DATA UNAVAILABLE", "checks": [], "setup": None}
            if pending:
                failure = reclaim_failure(pending, valid_quote["bid"] if valid_quote else None, valid4, valid15, now)
                if failure:
                    retire_reclaim(record, pending, failure)
                    pending = None
            active_reclaim = next((t for t in reversed(record["trades"]) if t["strategy"] == "reclaim" and t["status"] == "active"), None)
            if active_reclaim:
                result = {"status": "ACTIVE PAPER TRADE", "checks": active_reclaim["evidence"], "setup": None}
            elif valid4 and not current4:
                result = {"status": "WAITING FOR LATEST COMPLETED 4H CANDLE", "setup": pending,
                          "checks": copy.deepcopy(pending["checks"]) if pending else []}
                result["checks"].append(check("candle_alignment", "Latest completed 4H candle available", False,
                                              utc(valid4[-1].t), utc((now//H4-1)*H4), valid4[-1].t))
            elif current4:
                # Rescan completed structure even while a setup is pending.
                # Never move back to older structure after a newer setup fails.
                floor = max(record.get("reclaim_floor_ms", 0), pending["reclaim_end"] if pending else 0)
                scan = reclaim_scan(valid4, rules, now, record["consumed"], floor)
                candidate = scan["setup"]
                if pending is None or (candidate and candidate["reclaim_end"] > pending["reclaim_end"]):
                    superseded = pending is not None
                    if superseded:
                        retire_reclaim(record, pending, "Previous setup superseded by a newer 4H reclaim")
                    result = scan
                    pending = candidate
                    if pending:
                        record["pending"] = copy.deepcopy(pending)
                        record["reclaim_floor_ms"] = max(floor, pending["reclaim_end"])
                        if not superseded:
                            record["last_reclaim_result"] = None
                if pending:
                    failure = reclaim_failure(pending, valid_quote["bid"] if valid_quote else None, valid4, valid15, now)
                    if failure:
                        result = {"status": failure, "checks": copy.deepcopy(pending["checks"]),
                                  "trigger": None, "failed": True}
                        result["checks"].append(check("validity", "Setup remains valid at observation", False, failure, candle=now))
                    elif current15:
                        result = ltf_scan(valid15, pending, rules, now)
                    elif valid15:
                        result = {"status": "WAITING FOR LATEST COMPLETED 15M CANDLE",
                                  "checks": copy.deepcopy(pending["checks"]), "trigger": None}
                        result["checks"].append(check("candle_alignment", "Latest completed 15M candle available", False,
                                                      utc(valid15[-1].t), utc((now//M15-1)*M15), valid15[-1].t))
                    else:
                        result = {"status": "WAITING FOR VALID 15M DATA", "checks": pending["checks"], "trigger": None}
                    result["setup"] = pending
                    if result.get("failed"):
                        retire_reclaim(record, pending, result["status"])
                        result["setup"] = None
                    elif result.get("trigger") and not exited:
                        trigger = {**result["trigger"], "checks": copy.deepcopy(result["checks"])}
                        if valid_quote and valid_quote["bid"] <= trigger["mss_level"]:
                            pending["confirmation_reset_ms"] = now
                            record["pending"] = copy.deepcopy(pending)
                            record["last_reclaim_result"] = "15M breakout failed at the observed bid; fresh confirmation required"
                            result = ltf_scan(valid15, pending, rules, now)
                            result.update(status="WAITING FOR FRESH 15M CONFIRMATION", setup=pending)
                            result["checks"].append(check("entry", "Observed bid holds above the confirmed 15M break", False,
                                                          valid_quote["bid"], f"> {trigger['mss_level']}", now))
                        elif valid_quote:
                            try:
                                book = portfolio(document)
                                plan = entry_plan(valid_quote["ask"], valid_quote["bid"], trigger["close"], pending["stop"], pending["atr"], rules,
                                                  book["cash"], book["cost_equity"], book["open_risk"], valid4)
                                result["plan"] = copy.deepcopy(plan)
                                if rules.combined:
                                    evidence = entry_confirmation("long", neural_reading, valid_quote, now)
                                    plan.update(neural_entry=evidence, combined_model=rules.strategy_model)
                                    result["checks"].append(check("nn_confirmation", "NN confirms the qualified reclaim entry", True,
                                                                  evidence["label"], "BUY", evidence["signal_end"]))
                                    trigger["checks"] = copy.deepcopy(result["checks"])
                                trade = open_trade(document, name, "reclaim", trigger, plan, now, pending)
                                result["status"] = "ACTIVE PAPER TRADE"
                                result["checks"].append(check("entry", "Live entry and portfolio risk passed", True, plan["entry"], None, now))
                                trade["evidence"] = copy.deepcopy(result["checks"])
                                record["pending"] = None
                            except DataError as exc:
                                result["status"] = "WAITING FOR ACCEPTABLE ENTRY"
                                result["entry_error"] = str(exc)
                                result["checks"].append(check("entry", "Live entry and portfolio risk", False, str(exc)))
                        else:
                            result["status"] = "WAITING FOR FRESH QUOTE"
                            result["checks"].append(check("entry", "Fresh observable quote", False, errors.get("quote")))
            # Momentum gets its own error boundary and never waits for reclaim.
            momentum = momentum_scan(valid4, rules) if current4 and not rules.combined else {
                "status": "WAITING FOR LATEST COMPLETED 4H CANDLE" if valid4 else "DATA UNAVAILABLE",
                "checks": [], "signal": None}
            if rules.combined:
                momentum["status"] = "Combined Legacy uses Sweep / Reclaim entries only"
                record["momentum_watch"] = None
            active_momentum = next((t for t in reversed(record["trades"]) if t["strategy"] == "momentum" and t["status"] == "active"), None)
            if active_momentum:
                momentum = {"status": "ACTIVE PAPER TRADE", "checks": active_momentum["evidence"], "signal": None}
            source = momentum.get("signal")
            watch = record.get("momentum_watch")
            if active_momentum:
                record["momentum_watch"] = watch = None
            if watch:
                if watch["key"] in record["consumed"]:
                    record["momentum_watch"] = watch = None
                elif momentum_stop_breached(watch, valid_quote["bid"] if valid_quote else None, valid4, valid15):
                    record["consumed"].append(watch["key"])
                    record["momentum_watch"] = watch = None
                elif now-watch["end_ms"] > rules.momentum_cross_window*H4:
                    record["momentum_watch"] = watch = None
            if source and source["key"] not in record["consumed"] and not active_momentum:
                entry_age_ms = now-source["end_ms"]
                entry_fresh = entry_age_ms <= rules.trigger_fresh_bars*M15
                momentum["checks"].append(check("entry_age", "Momentum confirmation age for a new entry", entry_fresh,
                                                {"age_minutes": entry_age_ms/60000},
                                                {"max_age_minutes": rules.trigger_fresh_bars*15}, source["bar_ms"]))
                if source["stop"] > 0:
                    watch = {key: source[key] for key in ("key", "end_ms", "stop")}
                    record["momentum_watch"] = watch
                else:
                    record["momentum_watch"] = watch = None
                if watch and momentum_stop_breached(watch, valid_quote["bid"] if valid_quote else None, valid4, valid15):
                    record["consumed"].append(source["key"])
                    record["momentum_watch"] = None
                    momentum["status"] = "MOMENTUM SIGNAL INVALIDATED BY STOP"
                    momentum["signal"] = None
                    momentum["checks"].append(check("validity", "Momentum stop remains unbreached since confirmation", False,
                                                    source["stop"], candle=now))
                elif not entry_fresh:
                    momentum["status"] = "SIGNAL TOO OLD FOR A NEW ENTRY"
                elif valid_quote:
                    try:
                        book = portfolio(document)
                        plan = entry_plan(valid_quote["ask"], valid_quote["bid"], source["close"], source["stop"], source["atr"], rules,
                                          book["cash"], book["cost_equity"], book["open_risk"], valid4)
                        trade = open_trade(document, name, "momentum", source, plan, now)
                        record["momentum_watch"] = None
                        momentum["status"] = "ACTIVE PAPER TRADE"
                        momentum["checks"].append(check("entry", "Live entry and portfolio risk passed", True, plan["entry"], None, now))
                        trade["evidence"] = copy.deepcopy(momentum["checks"])
                    except DataError as exc:
                        momentum["status"] = "WAITING FOR ACCEPTABLE ENTRY"
                        momentum["entry_error"] = str(exc)
                        momentum["checks"].append(check("entry", "Live entry and portfolio risk", False, str(exc)))
                else:
                    momentum["status"] = "WAITING FOR FRESH QUOTE"
                    momentum["checks"].append(check("entry", "Fresh observable quote", False, errors.get("quote")))
            elif source and source["key"] in record["consumed"] and not active_momentum:
                momentum["status"] = "SIGNAL ALREADY PROCESSED"
            return {"name": name, "symbol": record["symbol"], "asof_ms": now, "quote": valid_quote,
                    "latest_4h_ms": valid4[-1].t if valid4 else None,
                    "latest_4h_rvol": rvol(valid4, len(valid4)-1, rules.volume_period) if valid4 else None,
                    "latest_15m_ms": valid15[-1].t if valid15 else None,
                    "reclaim": result, "momentum": momentum, "errors": errors,
                    "last_reclaim_result": record["last_reclaim_result"],
                    **({"combined": decision(record, before, neural_reading, valid_quote, now)} if rules.combined else {})}
        return self.store.transaction(advance)
