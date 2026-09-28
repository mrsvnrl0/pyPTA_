"""Separate collateral-based long/short paper study for the video strategy."""
from __future__ import annotations

import copy
import json
from math import isclose
import time
from dataclasses import asdict
from .core import SMC_ENGINE_VERSION, DataError, finite, passed
from .ledger import atomic_json, queue_event
from .persistence import DocumentStore
from .take_profit_alerts import cancel_near_alert, near_take_profit
from .position_alerts import entry_action, exit_action
from .sweep_buys import strategy_key, validate_sweep_buy
from .gex_targets import validate_selection


def portfolio(document):
    active = [t for a in document["assets"].values() for t in a["trades"] if t["status"] == "active"]
    return {"cash": document["cash"], "cost_equity": document["cash"]+sum(t["collateral"] for t in active),
            "open_risk": sum(t["initial_risk_usd"] for t in active), "active_positions": len(active),
            "realized_pnl": document["realized_pnl"]}


def validate(document):
    if not isinstance(document, dict) or document.get("version") != SMC_ENGINE_VERSION:
        raise DataError("Unsupported SMC study ledger")
    json.dumps(document, allow_nan=False)
    for key in ("cash", "realized_pnl"):
        finite(document[key], key)
    if not isinstance(document["assets"], dict) or not isinstance(document["outbox"], list) or not isinstance(document["warnings"], list):
        raise DataError("Invalid SMC study sections")
    identities = set()
    for record in document["assets"].values():
        if not isinstance(record["consumed"], list) or not isinstance(record["trades"], list):
            raise DataError("Invalid SMC asset progress")
        if any(not isinstance(key, str) for key in record["consumed"]) or len(set(record["consumed"])) != len(record["consumed"]):
            raise DataError("Invalid SMC consumed setup identities")
        order = record.get("pending")
        active = sum(t["status"] == "active" for t in record["trades"])
        if active > 1 or (active and order is not None):
            raise DataError("An asset may have one working limit or one active SMC trade")
        for item in ([order] if order else [])+record["trades"]:
            if item.get("execution", "limit") not in {"limit", "market"} or (item is order and item.get("execution") == "market"):
                raise DataError("Market entries cannot remain as pending limits")
            if not isinstance(item["id"], str) or not item["id"] or item["id"] in identities or item["side"] not in {"long", "short"}:
                raise DataError("Invalid SMC identity")
            identities.add(item["id"])
            entry = item["limit"] if item is order else item["entry"]
            for value in (entry, item["stop"], item["target"]):
                finite(value, "SMC price", 1e-15)
            if not (item["stop"] < entry < item["target"] if item["side"] == "long" else item["target"] < entry < item["stop"]):
                raise DataError("Invalid SMC price ordering")
            validate_sweep_buy(item)
            if "target_liquidity_price" in item or "target_sweep_buffer_bps" in item:
                level = finite(item.get("target_liquidity_price"), "target liquidity", 1e-15)
                validate_selection(item.get("target_selection"), level, item["side"])
                buffer = finite(item.get("target_sweep_buffer_bps"), "target sweep buffer", 1e-15)
                sign = 1 if item["side"] == "long" else -1
                if (buffer > 100 or sign*(level-entry) <= 0 or sign*(item["target"]-level) <= 0
                        or not isclose(item["target"], level*(1+sign*buffer/10000), rel_tol=1e-12)):
                    raise DataError("SMC take-profit does not match its recorded liquidity sweep")
            if not passed(item["checks"]) or not item["checks"]:
                raise DataError("SMC record lacks passed evidence")
            if (type(item["signal_end"]) is not int or item["signal_end"] < 0
                    or type(item["placed_ms"]) is not int or item["placed_ms"] <= item["signal_end"]):
                raise DataError("SMC order precedes its evidence")
            if (type(item["interval_ms"]) is not int or item["interval_ms"] not in {60000, 300000}
                    or type(item["last_bar_end"]) is not int or item["last_bar_end"] < item["placed_ms"]):
                raise DataError("Invalid SMC candle progress")
            if item is not order:
                if (item["status"] not in {"active", "target", "stopped", "nn_exit"} or type(item["opened_ms"]) is not int
                        or item["opened_ms"] < item["placed_ms"] or item["last_bar_end"] < item["opened_ms"]):
                    raise DataError("Invalid SMC trade lifecycle")
                for key in ("quantity", "collateral", "initial_risk_usd"):
                    finite(item[key], key, 1e-15)
                finite(item["entry_fee"], "entry fee", 0)
                finite(item["realized_pnl"], "trade P/L")
                for key in ("fee_rate", "slippage_rate"):
                    if not 0 <= finite(item[key], key, 0) < 0.05:
                        raise DataError("Invalid SMC trade cost rate")
                if item.get("execution") == "market":
                    quote = finite(item.get("entry_quote"), "breakout entry quote", 1e-15)
                    reference = finite(item.get("reference_price"), "breakout reference", 1e-15)
                    stamp = finite(item.get("entry_quote_ms"), "breakout quote time", 0)
                    chase = finite(item.get("max_chase_bps"), "breakout chase limit", 1e-15)
                    sign = 1 if item["side"] == "long" else -1
                    if (item.get("method") != "breakout" or chase > 100
                            or item["opened_ms"] != item["placed_ms"]
                            or not 0 < item["opened_ms"]-item["signal_end"] <= 60000
                            or not item["signal_end"] < stamp <= item["opened_ms"]+2000
                            or not isclose(entry, quote*(1+sign*item["slippage_rate"]), rel_tol=1e-12)
                            or sign*(entry/reference-1)*10000 > chase+1e-9):
                        raise DataError("Invalid saved breakout execution evidence")
                if type(item["tracking_gap"]) is not bool:
                    raise DataError("Invalid SMC tracking status")
                if item["status"] != "active":
                    finite(item["exit"], "exit", 1e-15)
                    if type(item["closed_ms"]) is not int or item["closed_ms"] < item["opened_ms"]:
                        raise DataError("SMC exit precedes entry")
    events = set()
    for event in document["outbox"]:
        if event["id"] in events or event["kind"] not in {"telegram", "ai"}:
            raise DataError("Invalid SMC event")
        if event["status"] not in {"queued", "running", "sent", "done", "failed", "uncertain", "cancelled"}:
            raise DataError("Invalid SMC delivery status")
        events.add(event["id"])


def prepare_smc(source, assets, rules):
    """Normalize startup state without writing files or invoking providers."""
    backups = []
    data = {"version": SMC_ENGINE_VERSION, "cash": rules.paper_equity, "realized_pnl": 0,
                 "settings": asdict(rules), "warnings": [], "outbox": [],
                 "assets": {name: {"symbol": cfg["symbol"], "pending": None, "consumed": [],
                                    "trades": [], "last_result": None} for name, cfg in assets.items()}}
    if source is not None:
        document = json.loads(source.decode("utf-8-sig"))
        validate(document)  # Damaged paper studies are not silently reset.
        data = document
        # GEX preferences apply to future selections; recorded levels stay fixed.
        selection_preferences = {"smc_tp_alert_bps", "smc_gex_targets", "smc_gex_alignment_bps", "smc_gex_max_basis_bps"}
        strategy_settings = lambda values: {k: v for k, v in values.items() if k not in selection_preferences and not k.startswith("nn_")}
        if strategy_settings(document["settings"]) != strategy_settings(asdict(rules)):
            backups.append(f".pre-settings-{time.time_ns()}.json")
            for record in document["assets"].values():
                record["pending"] = None
            document["settings"] = asdict(rules)
            document["warnings"].append("Settings changed: pending limits cancelled. Existing paper trades retain their recorded levels and costs.")
        document["settings"] = asdict(rules)
        for name, cfg in assets.items():
            if name in document["assets"] and document["assets"][name]["symbol"] != cfg["symbol"]:
                raise DataError("An SMC study cannot change an asset's pair; use a new --state path")
            document["assets"].setdefault(name, copy.deepcopy(SMCStore._record(cfg["symbol"])))
        for name, record in document["assets"].items():
            if rules.market_mode == "spot":
                if record["pending"] and record["pending"]["side"] == "short":
                    record.update(pending=None, last_result="Short limit cancelled: spot mode permits buy entries only")
                if any(t["status"] == "active" and t["side"] == "short" for t in record["trades"]):
                    warning = f"{name}: legacy margin paper short is paused in spot mode; its original accounting is preserved."
                    if warning not in document["warnings"]:
                        document["warnings"].append(warning)
            for trade in record["trades"]:
                trade.setdefault("strategy", "smc_"+trade["side"])
            if name not in assets:
                record["pending"] = None
                if any(t["status"] == "active" for t in record["trades"]):
                    warning = f"{name} is disabled with an active SMC paper trade; re-enable its pair to resume monitoring."
                    if warning not in document["warnings"]:
                        document["warnings"].append(warning)
        for event in document["outbox"]:
            if rules.market_mode == "spot" and (event.get("payload") or {}).get("side") == "short" and event["status"] == "queued":
                event.update(status="cancelled", error="Short actions disabled in spot mode.")
            if event.get("alert_type") == "near_take_profit" and event["status"] == "queued":
                event.update(status="cancelled", error="Restarted: waiting for a fresh take-profit observation.")
            if event["status"] == "running":
                event.update(status="uncertain" if event["kind"] == "telegram" else "queued",
                             error="Process restarted during delivery")
    validate(data)
    return data, backups


class SMCStore(DocumentStore):
    def __init__(self, path, assets, rules, *, backend=None):
        self._bind(path, backend, validate, lambda path, doc: atomic_json(path, doc))
        if backend is not None:
            backend.context = {"assets": assets, "rules": asdict(rules)}
        self._initialize(lambda source: prepare_smc(source, assets, rules), eager=True)

    @staticmethod
    def _record(symbol):
        return {"symbol": symbol, "pending": None, "consumed": [], "trades": [], "last_result": None}


def entry_plan(order, entry, rules, document):
    """Reserve unlevered collateral for either side; short proceeds are not cash."""
    if rules.market_mode == "spot" and order["side"] == "short":
        raise DataError("Spot mode permits buy entries only; a short sale requires margin mode")
    entry = finite(entry, "entry", 1e-15)
    long = order["side"] == "long"
    if order["side"] not in {"long", "short"} or not (order["stop"] < entry < order["target"] if long else order["target"] < entry < order["stop"]):
        raise DataError("Entry must be between the structural stop and liquidity target")
    sign = 1 if long else -1
    stop_fill = order["stop"]*(1-rules.slippage_rate if long else 1+rules.slippage_rate)
    risk_unit = sign*(entry-stop_fill)+rules.fee_rate*(entry+stop_fill)
    reward_unit = sign*(order["target"]-entry)-rules.fee_rate*(entry+order["target"])
    if risk_unit <= 0 or reward_unit <= 0:
        raise DataError("Liquidity target does not cover the configured trading costs")
    book = portfolio(document)
    risk = min(max(0, book["cost_equity"])*rules.risk_per_trade,
               max(0, book["cost_equity"]*rules.max_total_risk-book["open_risk"]),
               max(0, book["cost_equity"]-rules.paper_floor-book["open_risk"]))
    quantity = min(risk/risk_unit, max(0, book["cash"])/(entry*(1+rules.fee_rate)),
                   max(0, book["cost_equity"])*rules.max_allocation/(entry*(1+rules.fee_rate)))
    if quantity <= 0 or quantity*entry < rules.minimum_notional:
        raise DataError("Insufficient paper cash or risk budget")
    return {"entry": entry, "quantity": quantity, "collateral": quantity*entry,
            "entry_fee": quantity*entry*rules.fee_rate, "initial_risk_usd": quantity*risk_unit,
            "net_r": reward_unit/risk_unit, "fee_rate": rules.fee_rate, "slippage_rate": rules.slippage_rate}


def open_trade(document, name, order, entry, rules, now, bar_end=None):
    record = document["assets"][name]
    existing = next((t for t in record["trades"] if t["id"] == order["id"]), None)
    if existing is not None:
        return existing
    market = order.get("execution") == "market"
    if market:
        if record["pending"] is not None or any(t["status"] == "active" for t in record["trades"]):
            raise DataError("A breakout requires an empty paper slot")
        if now != order["placed_ms"] or order["signal_end"] >= now:
            raise DataError("A breakout must fill at its current observation after confirmation")
    elif record["pending"] is None or record["pending"]["id"] != order["id"]:
        raise DataError("Only a recorded pending limit can be filled")
    if now < order["placed_ms"]:
        raise DataError("A limit cannot fill before it was recorded")
    plan = entry_plan(order, entry, rules, document)
    trade = {**copy.deepcopy(order), **plan, "opened_ms": now, "status": "active", "realized_pnl": 0,
             "strategy": strategy_key(order),
             "last_bar_end": bar_end if bar_end is not None else now, "tracking_gap": False}
    document["cash"] -= trade["collateral"]+trade["entry_fee"]
    document["assets"][name]["trades"].append(trade)
    document["assets"][name]["pending"] = None
    queue_event(document, trade["id"]+":filled", "telegram",
                f"{name} #{trade['id'][:8]} · PAPER ENTRY FILLED · {entry_action(trade['side'])}\n"
                f"{name} {trade['side'].upper()} SMC paper {'breakout' if market else 'limit'} filled at ${entry:,.8f}\n"
                +(f"Confirmed sweep-reclaim breakout; no pullback required.\nExecutable quote ${order['entry_quote']:,.8f}; entry slippage included.\n" if market else "")+
                f"Entry price: ${entry:,.8f}\nExit action: {exit_action(trade['side'])}\n"
                f"Stop ${trade['stop']:,.8f}; {'sweep take-profit' if 'target_liquidity_price' in trade else 'liquidity target'} ${trade['target']:,.8f}; quantity {trade['quantity']:.8g}\n"
                "Paper observation only; no exchange order.", now, trade)
    return trade


def close_trade(document, name, trade, price, status, now):
    if trade["status"] != "active":
        return  # Replayed observations cannot release collateral a second time.
    if status not in {"stopped", "target", "nn_exit"} or now < trade["opened_ms"]:
        raise DataError("Invalid paper exit chronology or status")
    price = finite(price, "exit price", 1e-15)
    sign = 1 if trade["side"] == "long" else -1
    fill = price*(1-sign*trade["slippage_rate"]) if status in {"stopped", "nn_exit"} else price
    exit_fee = fill*trade["quantity"]*trade["fee_rate"]
    pnl = sign*(fill-trade["entry"])*trade["quantity"]-trade["entry_fee"]-exit_fee
    document["cash"] += trade["collateral"]+pnl+trade["entry_fee"]
    document["realized_pnl"] += pnl
    trade.update(status=status, exit=fill, closed_ms=now, realized_pnl=pnl)
    cancel_near_alert(document, trade["id"]+":near-tp", "Paper trade closed before the take-profit warning was delivered.")
    queue_event(document, trade["id"]+":closed", "telegram",
                f"{name} #{trade['id'][:8]} · PAPER EXIT RECORDED · {exit_action(trade['side'])}\n"
                f"Reason: {'STOP LOSS' if status == 'stopped' else 'NN ADVERSE SIGNAL' if status == 'nn_exit' else 'TAKE-PROFIT HIT'}\n"
                f"Recorded entry price: ${trade['entry']:,.8f}\nModeled exit price: ${fill:,.8f}\n"
                f"Quantity: {trade['quantity']:.8g} {name}\nNet modeled P/L ${pnl:+,.2f}\n"
                "Paper trade closed; no exchange order was submitted.", now, trade)


def monitor_trades(document, name, candles, quote, now, market_mode="margin"):
    for trade in document["assets"][name]["trades"]:
        if trade["status"] != "active":
            continue
        if market_mode == "spot" and trade["side"] == "short":
            continue
        long = trade["side"] == "long"
        # A quote fill happened inside its candle. Its earlier wick cannot
        # establish an exit, but its completed close is known after the fill.
        partial = next((b for b in candles if b.t <= trade["opened_ms"] < b.end
                        and b.end > trade["last_bar_end"]), None)
        if partial is not None:
            stop_hit = partial.c <= trade["stop"] if long else partial.c >= trade["stop"]
            target_hit = partial.c >= trade["target"] if long else partial.c <= trade["target"]
            if stop_hit or target_hit:
                close_trade(document, name, trade, partial.c if stop_hit else trade["target"],
                            "stopped" if stop_hit else "target", partial.end)
                continue
            trade["last_bar_end"] = partial.end
        relevant = [b for b in candles if b.t > trade["last_bar_end"] and b.t >= trade["opened_ms"]]
        expected = (trade["last_bar_end"]//trade["interval_ms"]+1)*trade["interval_ms"]
        if (relevant and relevant[0].t > expected) or (not relevant and now >= expected+trade["interval_ms"]):
            trade["tracking_gap"] = True
        for bar in relevant:
            stop_hit = bar.l <= trade["stop"] if long else bar.h >= trade["stop"]
            target_hit = bar.h >= trade["target"] if long else bar.l <= trade["target"]
            if stop_hit:
                price = min(bar.o, trade["stop"]) if long else max(bar.o, trade["stop"])
                close_trade(document, name, trade, price, "stopped", bar.end)
                break  # Conservative stop precedence when both extrema are present.
            if target_hit:
                close_trade(document, name, trade, trade["target"], "target", bar.end)
                break
            trade["last_bar_end"] = bar.end
        next_start = (trade["last_bar_end"]//trade["interval_ms"]+1)*trade["interval_ms"]
        if trade["status"] == "active" and now >= next_start+trade["interval_ms"]:
            trade["tracking_gap"] = True  # A valid replay can still have a missing tail.
        if trade["status"] == "active" and quote:
            mark = quote["bid" if long else "ask"]
            if mark <= trade["stop"] if long else mark >= trade["stop"]:
                close_trade(document, name, trade, mark, "stopped", now)
            elif mark >= trade["target"] if long else mark <= trade["target"]:
                close_trade(document, name, trade, trade["target"], "target", now)


def monitor_take_profit_alerts(document, name, quote, rules, now):
    for trade in document["assets"][name]["trades"]:
        event_id = trade["id"]+":near-tp"
        if trade["status"] != "active" or (rules.market_mode == "spot" and trade["side"] == "short"):
            cancel_near_alert(document, event_id, "Paper trade is no longer active.")
            continue
        near_take_profit(document, event_id, name, trade["side"], trade["target"], quote,
                         now, rules.smc_tp_alert_bps, "paper", trade.get("target_liquidity_price"),
                         position={**trade, "asset": name, "symbol": document["assets"][name]["symbol"]})
