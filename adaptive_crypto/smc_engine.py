"""Durable video-strategy limits; no retrospective entries or exchange orders."""
from __future__ import annotations

import copy
import uuid
from .core import DataError, check, finite, safe_error, utc, validate_candles
from .paper_neural import entry_confirmation, monitor_exits, trade_progress, decision
from .ledger import queue_event
from .smc import scan
from .sweep_buys import strategy_key, sweep_buy
from .position_alerts import entry_action, exit_action
from .smc_ledger import entry_plan, open_trade, close_trade, monitor_trades, monitor_take_profit_alerts


def cancel(record, reason):
    record["pending"] = None
    record["last_result"] = reason


def invalidation_close(order, candles):
    """The first observable setup-timeframe close beyond the recorded OB."""
    long = order.get("source_side", order["side"]) == "long"
    return next((b for b in candles if b.end > order["placed_ms"]
                 and (b.c < order["setup"]["zone_low"] if long else b.c > order["setup"]["zone_high"])), None)


def breakout_fill(candidate, quote, rules, now):
    """Quote-based market estimate; never fill at a past signal candle close."""
    if not 0 < now-candidate["signal_end"] <= 60000 or finite(quote["asof_ms"], "quote time") <= candidate["signal_end"]:
        raise DataError("Breakout expired: confirmation and entry quote must be current within 60 seconds")
    long = candidate["side"] == "long"
    sign = 1 if long else -1
    exit_quote = quote["bid" if long else "ask"]
    if sign*(exit_quote-candidate["breakout_level"]) <= 0:
        raise DataError("Breakout invalidated: quote is back through the confirmation level")
    mark = quote["ask" if long else "bid"]
    fill = mark*(1+sign*rules.slippage_rate)
    chase = sign*(fill/candidate["reference_price"]-1)*10000
    if chase > rules.smc_breakout_max_chase_bps:
        raise DataError("Breakout retired: price exceeds the configured chase limit including entry slippage")
    if sign*(fill-candidate["target_liquidity_price"]) >= 0:
        raise DataError("Breakout retired: entry would reach the liquidity target")
    return fill


def advance_limit(document, name, candles, quote, rules, now, setup_candles=(), neural_reading=None):
    record = document["assets"][name]
    order = record["pending"]
    if order is None:
        return
    if rules.market_mode == "spot" and order["side"] == "short":
        cancel(record, "Short limit cancelled: spot mode permits buy entries only")
        return
    long = order["side"] == "long"
    liquidity_price = order.get("target_liquidity_price", order["target"])
    invalidation = invalidation_close(order, setup_candles)
    invalidation_reason = "Limit cancelled: order block invalidated by a completed setup-timeframe close before any verified fill"
    relevant = [b for b in candles if b.t > order["last_bar_end"]]
    expected = (order["last_bar_end"]//order["interval_ms"]+1)*order["interval_ms"]
    if relevant and relevant[0].t > expected:
        cancel(record, "Limit cancelled: missing entry-timeframe history; fills during the gap are unknown")
        return
    for bar in relevant:
        if invalidation is not None and bar.t > invalidation.end:
            cancel(record, invalidation_reason)
            return
        if order.get("entry_origin") == "bearish_sweep" and bar.h >= order["source_stop"]:
            cancel(record, "Spot-buy limit cancelled: bearish source invalidated before a verified lower fill")
            return
        touched = bar.l <= order["limit"] if long else bar.h >= order["limit"]
        target_hit = bar.h >= liquidity_price if long else bar.l <= liquidity_price
        beyond_stop_at_open = bar.o <= order["stop"] if long else bar.o >= order["stop"]
        if target_hit or beyond_stop_at_open:
            cancel(record, "Limit cancelled: target consumed or opening price beyond stop before a verified fill")
            return
        if touched:
            if rules.combined:
                # A current NN class cannot authorize an earlier candle's fill.
                # Keep the level under observation and require a live quote.
                if bar.l <= order["stop"] if long else bar.h >= order["stop"]:
                    cancel(record, "Combined entry cancelled: stop breached before a confirmed live fill")
                    return
                order["last_bar_end"] = bar.end
                continue
            # Persisted limit existed before this whole candle began. Use its limit
            # price, never a favourable historical low/high. Same-bar TP is unknown.
            try:
                trade = open_trade(document, name, order, order["limit"], rules, bar.t, bar.end)
            except DataError as exc:
                cancel(record, "Limit cancelled at touch: "+str(exc))
                return
            if bar.l <= trade["stop"] if long else bar.h >= trade["stop"]:
                close_trade(document, name, trade, trade["stop"], "stopped", bar.end)
            else:
                monitor_trades(document, name, candles, quote, now, rules.market_mode)
            return
        order["last_bar_end"] = bar.end
    if invalidation is not None:
        if invalidation.end >= expected and (not relevant or relevant[-1].end < invalidation.end):
            invalidation_reason += "; missing entry history means earlier fills are unknown"
        cancel(record, invalidation_reason)
        return
    if quote:
        if order.get("entry_origin") == "bearish_sweep" and quote["bid"] >= order["source_stop"]:
            cancel(record, "Spot-buy limit cancelled: bearish source invalidated before a verified lower fill")
            return
        mark = quote["ask" if long else "bid"]
        if mark <= order["stop"] if long else mark >= order["stop"]:
            cancel(record, "Limit cancelled: observed entry quote is beyond the stop")
        elif (quote["bid"] >= liquidity_price) if long else (quote["ask"] <= liquidity_price):
            cancel(record, "Limit cancelled: liquidity target reached before entry")
        elif mark <= order["limit"] if long else mark >= order["limit"]:
            try:
                if rules.combined:
                    evidence = entry_confirmation(order["side"], neural_reading, quote, now)
                    spread = (quote["ask"]-quote["bid"])/((quote["ask"]+quote["bid"])/2)*10000
                    if spread > rules.max_spread_bps:
                        raise DataError("Spread exceeds configured execution limit")
                    order["neural_entry"] = evidence
                    order["checks"] = [c for c in order["checks"] if c["key"] != "nn_confirmation"]
                    order["checks"].append(check("nn_confirmation", "NN confirms entry at the strategy level", True,
                                                 evidence["label"], "BUY" if long else "SELL", evidence["signal_end"]))
                open_trade(document, name, order, mark, rules, now)
                # The executable exit side of a wide spread can already breach
                # the stop even though the entry side is still inside it.
                monitor_trades(document, name, [], quote, now, rules.market_mode)
            except DataError as exc:
                if rules.combined:
                    record["last_result"] = str(exc)
                else:
                    cancel(record, "Limit cancelled at touch: "+str(exc))


class SMCEngine:
    def __init__(self, assets, rules, store):
        self.assets, self.rules, self.store = assets, rules, store

    def evaluate(self, name, quote, candles_high, candles_low, now, errors=None, gex_profile=None, neural_reading=None):
        rules = self.rules
        high_interval, low_interval = rules.smc_setup_minutes*60000, rules.smc_entry_minutes*60000
        errors = dict(errors or {})
        high, low, valid_quote = [], [], None
        for bars, interval, label in ((candles_high, high_interval, f"{rules.smc_setup_minutes}m"),
                                      (candles_low, low_interval, f"{rules.smc_entry_minutes}m")):
            try:
                # Older but valid completed bars can still establish an exit.
                # Entry qualification separately requires current warm history.
                valid = validate_candles(bars, interval, now, 1, fresh=False)
                if interval == high_interval:
                    high = valid
                else:
                    low = valid
                if valid[-1].t != (now//interval-1)*interval:
                    errors[label] = f"Waiting for the latest completed {interval//60000}M candle"
                elif len(valid) < 2*rules.smc_pivot_strength+2:
                    errors[label] = f"Need {2*rules.smc_pivot_strength+2} completed {interval//60000}M candles"
            except (ValueError, TypeError) as exc:
                errors[label] = safe_error(exc)
        current_high = bool(high) and not errors.get(f"{rules.smc_setup_minutes}m") and high[-1].t == (now//high_interval-1)*high_interval
        current_low = bool(low) and not errors.get(f"{rules.smc_entry_minutes}m") and low[-1].t == (now//low_interval-1)*low_interval
        try:
            if not isinstance(quote, dict) or errors.get("clock"):
                raise DataError("Fresh quote and verified clock required")
            bid, ask = finite(quote["bid"], "bid", 1e-15), finite(quote["ask"], "ask", 1e-15)
            if bid > ask or not -2000 <= now-finite(quote["asof_ms"], "quote time") <= 30000:
                raise DataError("Stale, future or inverted quote")
            valid_quote = {**quote, "bid": bid, "ask": ask}
        except (ValueError, TypeError, KeyError) as exc:
            errors["quote"] = safe_error(exc)

        from .gex_targets import context, select_target
        gex_context = context(high, low, valid_quote if not errors else None, rules,
                              gex_profile, now, self.assets[name]["symbol"])

        def advance(document):
            record = document["assets"][name]
            before = trade_progress(record)
            if not errors.get("clock"):
                monitor_trades(document, name, low, valid_quote, now, rules.market_mode)
                if rules.combined:
                    monitor_exits(document, name, neural_reading, valid_quote, now, "smc_video")
            exited = rules.combined and any(before.get(t["id"], (None,))[0] == "active" and t["status"] != "active" for t in record["trades"])
            invalidated = record["pending"] is not None and invalidation_close(record["pending"], high) is not None
            if not errors.get("clock") and (current_low or invalidated):
                # On a delayed invalidation feed, honor earlier verified fills
                # first. Missing history cannot justify a later quote fill.
                advance_limit(document, name, low, valid_quote if current_low else None, rules, now, high, neural_reading)
            results = {}
            for side in ("long", "short"):
                result = scan(high, low, rules, side, record["consumed"]) if current_high and current_low and not errors.get("clock") else {
                    "status": "WAITING FOR CURRENT COMPLETED CANDLES", "checks": [], "setup": None, "entries": []}
                results["smc_"+side] = result
                if rules.smc_gex_targets and valid_quote and not errors:
                    for entry in result["entries"]:
                        reference = max(entry["limit"], valid_quote["ask"]) if side == "long" else min(entry["limit"], valid_quote["bid"])
                        original_taken = valid_quote["bid"] >= entry["target_liquidity_price"] if side == "long" else valid_quote["ask"] <= entry["target_liquidity_price"]
                        point = select_target(high, low, rules, side, reference, now, gex_context) if not original_taken else None
                        if point:
                            sign = 1 if side == "long" else -1
                            selection = point["target_selection"]
                            if selection["method"] == "gex_smc":
                                entry.update(target=point["price"]*(1+sign*rules.smc_tp_sweep_buffer_bps/10000),
                                             target_ms=point["bar_ms"], target_liquidity_price=point["price"])
                            else:
                                selection.update(liquidity_price=entry["target_liquidity_price"], nearest_smc_price=entry["target_liquidity_price"])
                            entry["target_selection"] = selection
                            entry["gross_r"] = sign*(entry["target"]-entry["limit"])/abs(entry["limit"]-entry["stop"])
                            for evidence in entry["checks"]:
                                if evidence["key"] == "smc_target":
                                    evidence.update(label="GEX/SMC liquidity target" if point["target_selection"]["method"] == "gex_smc" else "SMC liquidity target · GEX fallback",
                                                    measured={"liquidity_price": entry["target_liquidity_price"], "target_price": entry["target"], "sweep_buffer_bps": rules.smc_tp_sweep_buffer_bps},
                                                    required=selection["reason"], candle_ms=entry["target_ms"])
                    if result["entries"]:
                        result["checks"] = copy.deepcopy(result["entries"][0]["checks"])
                if side == "short" and rules.market_mode == "spot":
                    sources = result["entries"]
                    result.update(entries=[], execution_allowed=False, buy_watch_enabled=True,
                                  entry_error="SHORT setup watches for a spot buy at its lower sweep level. The higher reference is not a purchase.")
                    for evidence in result["checks"]:
                        if evidence["key"] == "smc_target":
                            evidence["label"] = "Lower liquidity sweep · prospective SPOT-BUY level"
                    for source in sources:
                        if valid_quote:
                            try:
                                converted = sweep_buy(source, high, low, valid_quote, rules, now, gex_context)
                                result["entries"].append(converted)
                                result.update(checks=converted["checks"], status="LOWER SPOT-BUY LIMIT QUALIFIED · WAITING FOR PAPER SLOT")
                            except DataError as exc:
                                result.update(status="BEARISH BUY WATCH · EXECUTION WAITING", entry_error=str(exc))
            active = next((t for t in reversed(record["trades"]) if t["status"] == "active"), None)
            if active is None and record["pending"] is None and current_high and current_low and valid_quote and not exited:
                candidates = [e for result in results.values() for e in result["entries"]]
                if candidates:
                    candidate = max(candidates, key=lambda e: (e["setup"]["bos_end"], -e["signal_end"], e["method"] == "conservative"))
                    result = results[strategy_key(candidate)]
                    retired = False
                    try:
                        # Keep the intended limit resting; never chase a past midpoint.
                        mark = valid_quote["ask" if candidate["side"] == "long" else "bid"]
                        market = candidate.get("execution") == "market"
                        midpoint_reached = not market and (mark <= candidate["limit"] if candidate["side"] == "long" else mark >= candidate["limit"])
                        liquidity_price = candidate.get("target_liquidity_price", candidate["target"])
                        target_reached = (valid_quote["bid"] >= liquidity_price if candidate["side"] == "long"
                                          else valid_quote["ask"] <= liquidity_price)
                        if midpoint_reached or target_reached:
                            # A live touch is permanent evidence even before the
                            # candle closes. Cost/spread recovery must not revive it.
                            retired = True
                            record["consumed"].append(candidate["setup_key"])
                            if candidate.get("sweep_key") and candidate["sweep_key"] not in record["consumed"]:
                                record["consumed"].append(candidate["sweep_key"])
                            record["last_result"] = ("Midpoint" if midpoint_reached else "Liquidity target")+" already reached before this order was recorded; wait for a new setup"
                            raise DataError(record["last_result"])
                        spread = (valid_quote["ask"]-valid_quote["bid"])/((valid_quote["ask"]+valid_quote["bid"])/2)*10000
                        if spread > rules.max_spread_bps:
                            raise DataError("Spread exceeds configured execution limit")
                        fill = candidate["limit"]
                        if market:
                            try:
                                fill = breakout_fill(candidate, valid_quote, rules, now)
                            except DataError as exc:
                                retired = True
                                record["consumed"].append(candidate["setup_key"])
                                if candidate.get("sweep_key") and candidate["sweep_key"] not in record["consumed"]:
                                    record["consumed"].append(candidate["sweep_key"])
                                record["last_result"] = str(exc)
                                raise
                        plan = entry_plan(candidate, fill, rules, document)
                        order = {**copy.deepcopy(candidate), "id": uuid.uuid4().hex, "placed_ms": now,
                                 "last_bar_end": now, "interval_ms": low_interval, "planned_net_r": plan["net_r"]}
                        if rules.combined:
                            order["combined_model"] = rules.strategy_model
                        if market:
                            if rules.combined:
                                evidence = entry_confirmation(order["side"], neural_reading, valid_quote, now)
                                order["neural_entry"] = evidence
                                order["checks"].append(check("nn_confirmation", "NN confirms strategy breakout", True,
                                                             evidence["label"], "BUY" if order["side"] == "long" else "SELL", evidence["signal_end"]))
                            order.update(entry_quote=mark, entry_quote_ms=valid_quote["asof_ms"],
                                         max_chase_bps=rules.smc_breakout_max_chase_bps,
                                         gross_r=abs(order["target"]-fill)/abs(fill-order["stop"]))
                            trade = open_trade(document, name, order, fill, rules, now)
                            queue_event(document, order["id"]+":ai", "ai",
                                        f"{name} confirmed sweep-reclaim breakout paper entry at ${fill:,.8f}; no pullback required.", now, trade)
                            monitor_trades(document, name, [], valid_quote, now, rules.market_mode)
                        else:
                            record["pending"] = order
                        record["last_result"] = None
                        record["consumed"].append(order["setup_key"])
                        if order.get("sweep_key") and order["sweep_key"] not in record["consumed"]:
                            record["consumed"].append(order["sweep_key"])
                        title = ("SHORT SETUP · SPOT-BUY LIMIT RECORDED · WAIT TO BUY" if order.get("entry_origin") == "bearish_sweep"
                                 else "PAPER ENTRY LIMIT · "+entry_action(order["side"]))
                        origin = (f"Bearish reference ${order['reference_price']:,.8f}; no purchase at this reference.\n"
                                  f"Wait for the lower sweep BUY limit ${order['limit']:,.8f}.\n"
                                  if order.get("entry_origin") == "bearish_sweep" else "")
                        text = (f"{name} #{order['id'][:8]} · {title}\n"+origin+
                                f"Entry limit price: ${order['limit']:,.8f}\n"
                                f"Planned exit: {exit_action(order['side'])} · target ${order['target']:,.8f} · stop ${order['stop']:,.8f}\n"
                                f"{name} {order['side'].upper()} · {rules.smc_setup_minutes}M/{rules.smc_entry_minutes}M {order['method']} SMC limit\n"
                                f"Confirmation closed {utc(order['signal_end'])}\nLimit midpoint ${order['limit']:,.8f}; "
                                f"stop ${order['stop']:,.8f}; take-profit ${order['target']:,.8f}\n"
                                +(f"Liquidity {'high' if order['side'] == 'long' else 'low'} ${order['target_liquidity_price']:,.8f}; "
                                  f"sweep buffer {order['target_sweep_buffer_bps']:g} bps; exit on the sweep, without waiting for reversal.\n"
                                  if 'target_liquidity_price' in order else "")+
                                f"Gross R {order['gross_r']:.2f}; modeled net R {plan['net_r']:.2f}\n"
                                +("Strategy level recorded. Entry requires a future live quote at this level AND an aligned current 4H NN signal. No buy or sell has executed."
                                  if rules.combined else "Paper limit awaiting a future touch; no exchange order."))
                        if not market:
                            queue_event(document, order["id"]+":limit", "telegram", text, now, order)
                            queue_event(document, order["id"]+":ai", "ai", text, now, order)
                    except DataError as exc:
                        result.update(status="SETUP RETIRED · WAITING FOR A NEW SETUP" if retired else "SETUP QUALIFIED · EXECUTION WAITING",
                                      entry_error=str(exc))
            # Exit processing runs first, so a crossed target never gets a late
            # "approaching" warning, including on the same scan as a fill.
            monitor_take_profit_alerts(document, name, valid_quote if not errors.get("clock") else None, rules, now)
            order = record["pending"]
            if order:
                results[strategy_key(order)] = {"status": "SPOT-BUY LIMIT WORKING · WAITING FOR LOWER SWEEP" if order.get("entry_origin") == "bearish_sweep" else "PAPER LIMIT WORKING", "setup": order["setup"],
                                                "checks": copy.deepcopy(order["checks"]), "entries": [], "order": order}
            active = next((t for t in reversed(record["trades"]) if t["status"] == "active"), None)
            if active:
                results[strategy_key(active)] = {"status": "ACTIVE PAPER TRADE", "setup": None,
                                                 "checks": copy.deepcopy(active["checks"]), "entries": []}
                if rules.market_mode == "spot" and active["side"] == "short":
                    results["smc_short"].update(status="LEGACY MARGIN PAPER SHORT · PAUSED",
                                                entry_error="Original paper record preserved; short actions are disabled in spot mode.")
            return {"name": name, "symbol": record["symbol"], "asof_ms": now, "quote": valid_quote,
                    "latest_setup_ms": high[-1].t if high else None, "latest_entry_ms": low[-1].t if low else None,
                    **results, "gex_context": gex_context, "errors": errors, "last_result": record["last_result"],
                    **({"combined": decision(record, before, neural_reading, valid_quote, now)} if rules.combined else {})}
        return self.store.transaction(advance)
