"""Atomic ledger persistence, migrations, portfolio accounting, and exit replay."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from dataclasses import asdict
from .persistence import DocumentStore
from .core import (
    VERSION, ENGINE_VERSION, PREVIOUS_ENGINE_VERSION, LEGACY_ENGINE_VERSION,
    H4, M15, DataError, finite, passed, safe_error, utc,
)


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name+".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def fingerprint(assets, rules, engine_version=ENGINE_VERSION):
    values = asdict(rules)
    if rules.base_strategy == "legacy":
        # New SMC-only settings must not invalidate a preserved legacy study.
        values = {key: value for key, value in values.items() if key not in {"strategy_model", "market_mode"} and not key.startswith(("smc_", "nn_"))}
    return hashlib.sha256(json.dumps({"engine": engine_version, "assets": assets, "rules": values}, sort_keys=True).encode()).hexdigest()[:20]


def new_state(assets, rules):
    return {"version": VERSION, "fingerprint": fingerprint(assets, rules), "cash": rules.paper_equity,
            "realized_pnl": 0.0, "assets": {name: {"symbol": cfg["symbol"], "pending": None,
            "consumed": [], "trades": [], "last_reclaim_result": None,
            "reclaim_floor_ms": 0, "momentum_watch": None} for name, cfg in assets.items()},
            "outbox": [], "warnings": []}


def validate_state(document, assets, rules):
    if not isinstance(document, dict) or document.get("version") != VERSION or document.get("fingerprint") != fingerprint(assets, rules):
        raise DataError("State belongs to an older engine, pair list, or rule configuration")
    # Reject NaN/Infinity anywhere, including nested measurement records.
    json.dumps(document, allow_nan=False)
    finite(document["cash"], "paper cash", -1e-7)
    finite(document["realized_pnl"], "realized P/L")
    if set(document["assets"]) != set(assets) or not isinstance(document["outbox"], list):
        raise DataError("Malformed state sections")
    if not isinstance(document["warnings"], list):
        raise DataError("Malformed state warnings")
    for name, record in document["assets"].items():
        if record["symbol"] != assets[name]["symbol"] or not isinstance(record["consumed"], list) or not isinstance(record["trades"], list):
            raise DataError("Malformed asset state")
        if "last_reclaim_result" not in record or any(not isinstance(key, str) for key in record["consumed"]):
            raise DataError("Malformed asset progress")
        floor = record.get("reclaim_floor_ms", 0)
        if type(floor) is not int or floor < 0:
            raise DataError("Invalid reclaim chronology")
        watch = record.get("momentum_watch")
        if watch is not None:
            if not isinstance(watch, dict) or not isinstance(watch.get("key"), str) or not watch["key"].startswith("momentum:"):
                raise DataError("Invalid momentum watch")
            finite(watch["stop"], "momentum watch stop", 1e-15)
            if type(watch["end_ms"]) is not int or watch["end_ms"] < 0:
                raise DataError("Invalid momentum watch timestamp")
        pending = record["pending"]
        if pending is not None:
            if not isinstance(pending, dict) or not isinstance(pending["key"], str):
                raise DataError("Malformed pending setup")
            for key in ("stop", "zone_low", "zone_high", "atr", "invalidation"):
                finite(pending[key], key, 1e-15)
            if not pending["stop"] < pending["zone_low"] < pending["zone_high"]:
                raise DataError("Invalid pending price ordering")
            for key in ("reclaim_end", "expires_ms"):
                if type(pending[key]) is not int or pending[key] <= 0:
                    raise DataError("Invalid pending timestamp")
            if not isinstance(pending["checks"], list):
                raise DataError("Missing setup evidence")
            reset = pending.get("confirmation_reset_ms", 0)
            if type(reset) is not int or reset < 0:
                raise DataError("Invalid confirmation reset timestamp")
        for trade in record["trades"]:
            if trade["strategy"] not in {"reclaim", "momentum"} or not isinstance(trade["id"], str) or not isinstance(trade["evidence"], list):
                raise DataError("Invalid stored trade identity or evidence")
            if not trade["evidence"] or not passed(trade["evidence"]):
                raise DataError("Stored trade lacks passed qualification evidence")
            for key in ("asset", "symbol", "signal_key", "signal_end", "tracking_gap", "tp1_hit", "tp2_hit", "initial_risk_usd", "net_r1", "net_r2", "invalidation"):
                if key not in trade:
                    raise DataError(f"Missing stored trade field: {key}")
            if trade["strategy"] == "reclaim":
                finite(trade["invalidation"], "invalidation", 1e-15)
            for key in ("entry", "initial_stop", "stop", "tp1", "tp2", "quantity", "cost_per_unit", "risk_per_unit", "atr"):
                finite(trade[key], key, 1e-15)
            if not trade["initial_stop"] < trade["entry"] < trade["tp1"] < trade["tp2"]:
                raise DataError("Invalid stored trade levels")
            remaining = finite(trade["remaining"], "remaining", 0)
            if remaining > trade["quantity"]+1e-9 or trade["status"] not in {"active", "completed", "stopped", "invalidated", "nn_exit"}:
                raise DataError("Invalid trade lifecycle")
            if trade["status"] != "active" and remaining > 1e-9:
                raise DataError("Closed trade has remaining quantity")
            for key in ("opened_ms", "last_bar_end"):
                if type(trade[key]) is not int or trade[key] < 0:
                    raise DataError("Invalid trade timestamp")
            history = trade.get("stop_history", [])
            if not isinstance(history, list):
                raise DataError("Invalid stop history")
            previous_time, previous_stop = trade["opened_ms"], trade["initial_stop"]
            for change in history:
                stamp = change["effective_ms"]
                level = finite(change["stop"], "historical stop", previous_stop)
                if type(stamp) is not int or stamp < previous_time or level > trade["stop"]:
                    raise DataError("Invalid stop history ordering")
                previous_time, previous_stop = stamp, level
            if history and history[-1]["stop"] != trade["stop"]:
                raise DataError("Stop history does not match active stop")
            if "tp1_ms" in trade and (type(trade["tp1_ms"]) is not int or trade["tp1_ms"] < trade["opened_ms"]):
                raise DataError("Invalid TP1 timestamp")
            finite(trade["realized_pnl"], "trade P/L")
            for key in ("fee_rate", "slippage_rate"):
                if not 0 <= finite(trade[key], key, 0) < 0.05:
                    raise DataError("Invalid stored cost rate")
    ids = set()
    for event in document["outbox"]:
        if not isinstance(event, dict) or event["id"] in ids or event["kind"] not in {"telegram", "ai"}:
            raise DataError("Invalid event identity")
        if event["status"] not in {"queued", "running", "sent", "done", "failed", "uncertain", "cancelled"}:
            raise DataError("Invalid event status")
        ids.add(event["id"])


def upgrade_state(document, assets, rules):
    """Preserve compatible ledgers and tag the engine behind historical trades."""
    if not isinstance(document, dict) or document.get("version") != VERSION:
        return document
    previous_engine = next((version for version in (PREVIOUS_ENGINE_VERSION, LEGACY_ENGINE_VERSION)
                            if document.get("fingerprint") == fingerprint(assets, rules, version)), None)
    if previous_engine is None:
        return document
    upgraded = copy.deepcopy(document)
    upgraded["fingerprint"] = fingerprint(assets, rules)
    # The schema is unchanged. Validate before clearing anything so malformed
    # legacy state is still archived rather than partially accepted.
    validate_state(upgraded, assets, rules)
    for record in upgraded["assets"].values():
        floor = record.get("reclaim_floor_ms", 0)
        # A consumed newer identity must not expose older unconsumed structure
        # on upgrade. Matching the same pending reclaim again is permitted.
        for key in record["consumed"]:
            identity = re.fullmatch(r"reclaim:\d+:(\d+)", key)
            if identity:
                floor = max(floor, int(identity.group(1))+H4)
        if record["pending"] is not None:
            floor = max(floor, record["pending"]["reclaim_end"])
            if previous_engine == LEGACY_ENGINE_VERSION:
                record["pending"] = None
                record["last_reclaim_result"] = "Previous pending setup cleared for candle revalidation"
        record["reclaim_floor_ms"] = floor
        if previous_engine == LEGACY_ENGINE_VERSION:
            record["momentum_watch"] = None
        for trade in record["trades"]:
            trade.setdefault("engine", previous_engine)
    upgraded["warnings"].append(
        f"Upgraded {previous_engine} ledger to {ENGINE_VERSION}: cash, trades and consumed signals preserved. "
        "Pending setups will be rescanned; v9.2 confirmation resets and momentum watches are retained. "
        "Saved outcomes have not been recalculated under the corrected exit rules.")
    return upgraded


def prepare_state(source, assets, rules, *, strict=False):
    """Pure startup normalization; backup suffixes describe required source archives."""
    backups = []
    data = new_state(assets, rules)
    if source is not None:
        try:
            document = json.loads(source.decode("utf-8-sig"))
            previous = document
            document = upgrade_state(document, assets, rules)
            validate_state(document, assets, rules)
            if document is not previous:
                backups.append(f".pre-v9.3-{time.time_ns()}.json")
            data = document
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # Preserve the uninterpreted old file outside active state; never
            # label its old formulas or unknown exchange data as current.
            if strict and str(exc) != "State belongs to an older engine, pair list, or rule configuration":
                raise DataError("Malformed SQLite legacy state") from exc
            backups.append(f".archived-{time.time_ns()}.json")
            data["warnings"].append(f"Previous state archived: {safe_error(exc)}. New paper ledger started.")
    for event in data["outbox"]:
        if event["status"] == "running":
            event["status"] = "uncertain" if event["kind"] == "telegram" else "queued"
            event["error"] = "Process restarted during request; Telegram delivery is unknown." if event["kind"] == "telegram" else None
    return data, backups


class StateStore(DocumentStore):
    """Serialized durable state with detached snapshots and callback results."""
    def __init__(self, path, assets, rules, *, backend=None):
        self.assets, self.rules = assets, rules
        self._bind(path, backend, lambda doc: validate_state(doc, assets, rules),
                   lambda path, doc: atomic_json(path, doc))
        if backend is not None:
            backend.context = {"assets": assets, "rules": asdict(rules)}
        self._initialize(lambda source: prepare_state(source, assets, rules, strict=self.database_path is not None), eager=True)


def queue_event(document, event_id, kind, text, now, trade=None):
    if any(e["id"] == event_id for e in document["outbox"]):
        return
    document["outbox"].append({"id": event_id, "kind": kind, "status": "queued", "text": text,
                               "created_ms": now, "attempts": 0, "retry_ms": 0,
                               "payload": copy.deepcopy(trade), "error": None})


def trade_text(trade, event, now, exit_price=None):
    price = f"\nObserved exit level: {exit_price:.8g}" if exit_price is not None else ""
    return (f"{trade['asset']} / {trade['strategy'].upper()} — {event}\n"
            f"Paper signal tracker | {trade['symbol']} | {utc(now)}\n"
            f"Entry estimate: {trade['entry']:.8g}; stop: {trade['stop']:.8g}\n"
            f"TP1: {trade['tp1']:.8g} ({trade['net_r1']:g} net R); TP2: {trade['tp2']:.8g} ({trade['net_r2']:g} net R)\n"
            f"Original quantity: {trade['quantity']:.8g}; modeled initial risk: ${trade['initial_risk_usd']:.2f}\n"
            f"Signal candle closed: {utc(trade['signal_end'])}{price}\n"
            f"Event ID: {trade['id']}:{event}. No exchange order submitted.")


def portfolio(document):
    active = [t for a in document["assets"].values() for t in a["trades"] if t["status"] == "active"]
    # Cost equity avoids overstating available budget using unverified live gains.
    equity = document["cash"] + sum(t["remaining"]*t["cost_per_unit"] for t in active)
    # Reserve original per-unit risk until quantity exits; trailing stops cannot
    # manufacture extra risk capacity and both strategies share this ledger.
    open_risk = sum(t["remaining"]*t["risk_per_unit"] for t in active)
    return {"cash": document["cash"], "cost_equity": equity, "open_risk": open_risk,
            "realized_pnl": document["realized_pnl"], "active_positions": len(active)}


def open_trade(document, name, strategy, source, plan, now, pending=None):
    record = document["assets"][name]
    trade = {**plan, "id": f"{record['symbol']}:{source['key']}", "asset": name,
             "engine": ENGINE_VERSION,
             "symbol": record["symbol"], "strategy": strategy, "signal_key": source["key"],
             "signal_end": source["end_ms"], "opened_ms": now, "last_bar_end": now,
             "status": "active", "tp1_hit": False, "tp2_hit": False, "realized_pnl": 0.0,
             "invalidation": pending["invalidation"] if pending else None,
             "evidence": copy.deepcopy(source["checks"]), "tracking_gap": False,
             "stop_history": [{"effective_ms": now, "stop": plan["stop"]}]}
    document["cash"] -= plan["allocation_usd"]
    record["trades"].append(trade)
    record["consumed"].append(pending["key"] if pending else source["key"])
    if pending:
        record["reclaim_floor_ms"] = max(record.get("reclaim_floor_ms", 0), pending["reclaim_end"]+1)
    queue_event(document, trade["id"]+":entry", "telegram", trade_text(trade, "SIGNAL QUALIFIED", now), now)
    queue_event(document, trade["id"]+":ai", "ai", "Optional commentary on the saved signal", now, trade)
    return trade


def close_quantity(document, trade, qty, price, event, now):
    qty = min(qty, trade["remaining"])
    proceeds = price*(1-trade["slippage_rate"])*(1-trade["fee_rate"])*qty
    pnl = proceeds - trade["cost_per_unit"]*qty
    document["cash"] += proceeds
    document["realized_pnl"] += pnl
    trade["realized_pnl"] += pnl
    trade["remaining"] = max(0.0, trade["remaining"]-qty)
    if trade["remaining"] < trade["quantity"]*1e-10:
        trade["remaining"] = 0.0
        trade["status"] = "completed" if event == "TP2" else "invalidated" if event == "4H INVALIDATION" else "nn_exit" if event == "NN EXIT" else "stopped"
        trade["closed_ms"] = now
        trade["exit"] = price*(1-trade["slippage_rate"])
    if event == "TP1":
        trade["tp1_hit"] = True
        trade["tp1_ms"] = now
    if event == "TP2":
        trade["tp2_hit"] = True
    queue_event(document, trade["id"]+":"+event, "telegram", trade_text(trade, event, now, price), now)


def stop_at(trade, timestamp):
    """Use only a stop already in force at the beginning of a replayed bar."""
    stop = trade["initial_stop"]
    for change in trade.get("stop_history", []):
        if change["effective_ms"] <= timestamp:
            stop = change["stop"]
    return stop


def replay_prices(document, trade, bar):
    stop = stop_at(trade, bar.t)
    if bar.l <= stop:
        close_quantity(document, trade, trade["remaining"], min(bar.o, stop), "STOP", bar.end)
        return
    # Even when the low predates a raised stop, the closing price is known
    # after that change and can establish a breach without intrabar guesses.
    if bar.c <= stop_at(trade, bar.end-1):
        close_quantity(document, trade, trade["remaining"], bar.c, "STOP", bar.end)
        return
    if not trade["tp1_hit"] and bar.h >= trade["tp1"]:
        close_quantity(document, trade, trade["quantity"]/2, trade["tp1"], "TP1", bar.end)
    if trade["status"] == "active" and bar.h >= trade["tp2"]:
        close_quantity(document, trade, trade["remaining"], trade["tp2"], "TP2", bar.end)
    if (trade["status"] == "active" and trade["strategy"] == "momentum"
            and trade["tp1_hit"] and trade.get("tp1_ms", trade["opened_ms"]) <= bar.end):
        breakeven = trade["cost_per_unit"] / ((1-trade["fee_rate"])*(1-trade["slippage_rate"]))
        new_stop = max(trade["stop"], breakeven, bar.c-1.5*trade["atr"])
        if new_stop > trade["stop"]:
            trade["stop"] = new_stop
            trade["stop_history"].append({"effective_ms": bar.end, "stop": new_stop})


def monitor_positions(document, name, bid, candles4, candles15, now):
    """Replay non-overlapping price bars and structural closes in time order.

    Prefer complete 15M coverage. Otherwise a full post-entry 4H bar replaces
    its available 15M fragments, with stop precedence when order is unknown.
    Partial entry bars supply only their closing price, never full-bar extremes.
    Fallback cannot reconstruct earlier unobserved fills or revise saved exits.
    """
    starts15 = {b.t for b in candles15}
    for trade in document["assets"][name]["trades"]:
        if trade["status"] != "active":
            continue
        if "stop_history" not in trade:
            trade["stop_history"] = [{"effective_ms": trade["opened_ms"], "stop": trade["initial_stop"]}]
            if trade["stop"] != trade["initial_stop"]:
                # Older ledgers did not record when trailing stops changed.
                trade["stop_history"].append({"effective_ms": trade["last_bar_end"], "stop": trade["stop"]})
                trade["tracking_gap"] = True
        watermark = trade["last_bar_end"]
        next_start = ((watermark+M15)//M15)*M15
        if (candles15 and candles15[0].t > next_start) or (not candles15 and now-next_start >= M15):
            trade["tracking_gap"] = True
        fallback4 = [b for b in candles4 if b.t >= trade["opened_ms"] and b.end > watermark
                     and not all(t in starts15 for t in range(b.t, b.end+1, M15))]
        fallback_starts = {b.t for b in fallback4}
        prices = [b for b in candles15 if b.t >= trade["opened_ms"] and b.end > watermark
                  and (b.t//H4)*H4 not in fallback_starts]
        timeline = [(b.end, 0, b) for b in prices+fallback4]
        timeline += [(b.end, 1, b) for b in candles4
                     if b.end > max(trade["opened_ms"], trade.get("last_4h_end", 0))]
        for _, structural, bar in sorted(timeline, key=lambda item: (item[0], item[1])):
            if trade["status"] != "active":
                break
            if structural:
                if bar.c <= stop_at(trade, bar.end-1):
                    close_quantity(document, trade, trade["remaining"], bar.c, "STOP", bar.end)
                elif trade["strategy"] == "reclaim" and bar.c < trade["invalidation"]:
                    close_quantity(document, trade, trade["remaining"], bar.c, "4H INVALIDATION", bar.end)
                trade["last_4h_end"] = bar.end
            else:
                if bar.interval == H4:
                    trade["tracking_gap"] = True
                replay_prices(document, trade, bar)
                trade["last_bar_end"] = max(trade["last_bar_end"], bar.end)
        # Price tracking continues even if the 15m endpoint is unavailable.
        if trade["status"] == "active" and bid is not None:
            if bid <= trade["stop"]:
                close_quantity(document, trade, trade["remaining"], bid, "STOP", now)
            else:
                if not trade["tp1_hit"] and bid >= trade["tp1"]:
                    close_quantity(document, trade, trade["quantity"]/2, trade["tp1"], "TP1", now)
                if trade["status"] == "active" and bid >= trade["tp2"]:
                    close_quantity(document, trade, trade["remaining"], trade["tp2"], "TP2", now)
