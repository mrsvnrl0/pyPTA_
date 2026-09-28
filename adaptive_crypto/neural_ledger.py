"""Independent durable long-only ledger for neural-network paper trades."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from math import isclose
import time

from .core import DataError, H4, Rules, finite
from .ledger import atomic_json, queue_event
from .persistence import DocumentStore
from .smc_ledger import portfolio

VERSION = "neural-paper-v1"


def record(symbol):
    return {"symbol": symbol, "trades": [], "consumed": [], "last_signal_end": 0,
            "last_result": None, "model_id": None}


def validate(document):
    try:
        if document["version"] != VERSION:
            raise DataError("Unsupported neural study ledger")
        json.dumps(document, allow_nan=False)
        Rules(**document["settings"]).validate()
        if not isinstance(document["assets"], dict):
            raise DataError("Invalid neural assets")
        cash = finite(document["cash"], "neural cash", -1e-7)
        realized = finite(document["realized_pnl"], "neural P/L")
        initial = finite(document["initial_equity"], "initial neural equity", 1e-15)
        if not isinstance(document["warnings"], list) or not isinstance(document["outbox"], list):
            raise DataError("Invalid neural state sections")
        ids, collateral, paid_fees, closed_pnl = set(), 0., 0., 0.
        for name, row in document["assets"].items():
            if not isinstance(row["symbol"], str) or type(row["last_signal_end"]) is not int or row["last_signal_end"] < 0:
                raise DataError("Invalid neural candle progress")
            if row["last_signal_end"] and (row["last_signal_end"]+1) % H4:
                raise DataError("Neural progress must identify a completed 4H candle")
            if not isinstance(row["trades"], list) or not isinstance(row["consumed"], list):
                raise DataError("Invalid neural history")
            # Mode changes never invalidate already-recorded positions. The
            # one-position restriction is an entry rule, not a ledger invariant.
            signal_ends = set()
            for t in row["trades"]:
                if (not isinstance(t["id"], str) or not t["id"] or t["id"] in ids or t["asset"] != name
                        or t["side"] != "long" or t["strategy"] != "neural_network" or t["symbol"] != row["symbol"]):
                    raise DataError("Invalid neural trade identity")
                ids.add(t["id"])
                if t["signal_end"] in signal_ends:
                    raise DataError("Duplicate neural entry candle")
                signal_ends.add(t["signal_end"])
                limited = t.get("limitations_enabled", True)
                if type(limited) is not bool:
                    raise DataError("Invalid recorded neural limitations mode")
                for key in ("entry", "quantity", "collateral", "initial_risk_usd"):
                    finite(t[key], key, 1e-15)
                if (limited and not 0 < finite(t["stop"], "stop") < t["entry"]
                        or not limited and (t["stop"] is not None or t["status"] == "stopped")
                        or t["status"] not in {"active", "sold", "stopped"}):
                    raise DataError("Invalid neural trade levels/status")
                for key in ("fee_rate", "slippage_rate"):
                    if not 0 <= finite(t[key], key) < .05:
                        raise DataError("Invalid recorded neural costs")
                if not isclose(t["collateral"], t["entry"]*t["quantity"], rel_tol=1e-10):
                    raise DataError("Neural collateral mismatch")
                if not isclose(t["entry_fee"], t["collateral"]*t["fee_rate"], rel_tol=1e-10, abs_tol=1e-10):
                    raise DataError("Neural entry fee mismatch")
                stop_fill = t["stop"]*(1-t["slippage_rate"]) if limited else 0.
                # Without a stop, all invested cash (including entry fee) is at risk.
                risk = t["quantity"]*(t["entry"]-stop_fill+t["fee_rate"]*(t["entry"]+stop_fill))
                if not isclose(t["initial_risk_usd"], risk, rel_tol=1e-10, abs_tol=1e-10):
                    raise DataError("Neural recorded risk mismatch")
                if (type(t["opened_ms"]) is not int or type(t["signal_end"]) is not int
                        or not 0 <= t["signal_end"] < t["opened_ms"] or type(t["last_bar_end"]) is not int
                        or (t["signal_end"]+1) % H4 or row["last_signal_end"] < t["signal_end"]
                        or t["last_bar_end"] < t["opened_ms"] or type(t["tracking_gap"]) is not bool):
                    raise DataError("Invalid neural chronology")
                if (t["label"] != "BUY" or not isinstance(t["model_id"], str) or not t["model_id"]
                        or set(t["probabilities"]) != {"BUY", "HOLD", "SELL"}
                        or any(not 0 <= finite(p) <= 1 for p in t["probabilities"].values())
                        or not isclose(sum(t["probabilities"].values()), 1., abs_tol=1e-6)):
                    raise DataError("Invalid recorded neural prediction")
                if t["status"] == "active":
                    if finite(t["realized_pnl"]) != 0:
                        raise DataError("Active neural trade cannot realize P/L")
                    collateral += t["collateral"]
                    paid_fees += t["entry_fee"]
                else:
                    finite(t["exit"], "neural exit", 1e-15)
                    if type(t["closed_ms"]) is not int or t["closed_ms"] < t["opened_ms"]:
                        raise DataError("Neural exit precedes entry")
                    expected = t["quantity"]*(t["exit"]-t["entry"])-t["entry_fee"]-t["exit_fee"]
                    if (not isclose(t["exit_fee"], t["quantity"]*t["exit"]*t["fee_rate"], abs_tol=1e-8)
                            or not isclose(t["realized_pnl"], expected, abs_tol=1e-8)):
                        raise DataError("Neural realized P/L mismatch")
                    closed_pnl += expected
        if not isclose(realized, closed_pnl, abs_tol=1e-6) or not isclose(cash+collateral+paid_fees, initial+realized, abs_tol=1e-6):
            raise DataError("Neural cash does not reconcile")
        events = set()
        for event in document["outbox"]:
            if (not isinstance(event["id"], str) or event["id"] in events or event["kind"] not in {"telegram", "ai"}
                    or event["status"] not in {"queued", "running", "sent", "done", "failed", "uncertain", "cancelled"}):
                raise DataError("Invalid neural delivery record")
            if (not isinstance(event["text"], str) or any(type(event[k]) is not int or event[k] < 0
                                                        for k in ("created_ms", "retry_ms", "attempts"))):
                raise DataError("Invalid neural delivery progress")
            events.add(event["id"])
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        raise DataError(f"Malformed neural ledger: {exc}") from exc


def prepare_neural(source, assets, rules):
    backups = []
    if source is None:
        data = {"version": VERSION, "cash": rules.paper_equity, "initial_equity": rules.paper_equity,
                "realized_pnl": 0., "settings": asdict(rules), "warnings": [], "outbox": [],
                "assets": {name: record(cfg["symbol"]) for name, cfg in assets.items()}}
    else:
        data = json.loads(source.decode("utf-8-sig"))
        validate(data)
        if data["settings"] != asdict(rules):
            backups.append(f".pre-settings-{time.time_ns()}.json")
            data["settings"] = asdict(rules)
            data["warnings"].append("Settings changed; recorded neural entries, stops, costs and consumed candles remain fixed.")
        for name, cfg in assets.items():
            if name in data["assets"] and data["assets"][name]["symbol"] != cfg["symbol"]:
                raise DataError("Cannot change a neural asset's pair; use a new --state path")
            data["assets"].setdefault(name, record(cfg["symbol"]))
        for name, row in data["assets"].items():
            if name not in assets and any(t["status"] == "active" for t in row["trades"]):
                warning = f"{name} is disabled with an active neural trade; re-enable it to monitor exits."
                if warning not in data["warnings"]:
                    data["warnings"].append(warning)
        for event in data["outbox"]:
            if event["status"] == "running":
                event.update(status="uncertain", error="Restart interrupted delivery")
    validate(data)
    return data, backups


class NeuralStore(DocumentStore):
    def __init__(self, path, assets, rules, *, backend=None):
        self._bind(path, backend, validate, atomic_json)
        if backend is not None:
            backend.context = {"assets": assets, "rules": asdict(rules)}
        self._initialize(lambda source: prepare_neural(source, assets, rules), eager=True)


def open_trade(document, name, signal, quote, rules, now):
    row = document["assets"][name]
    if rules.nn_limitations and any(t["status"] == "active" for t in row["trades"]):
        raise DataError("Neural position already open")
    if any(t["signal_end"] == signal["signal_end"] for t in row["trades"]):
        raise DataError("Neural entry candle already traded")
    entry = quote["ask"]*(1+rules.slippage_rate)
    stop = entry*(1-rules.nn_stop_loss) if rules.nn_limitations else None
    stop_fill = stop*(1-rules.slippage_rate) if stop is not None else 0.
    unit_risk = entry-stop_fill+rules.fee_rate*(entry+stop_fill)
    book = portfolio(document)
    available_cash = max(0., book["cash"])
    quantity = available_cash/(entry*(1+rules.fee_rate))
    if rules.nn_limitations:
        risk = min(book["cost_equity"]*rules.risk_per_trade,
                   max(0., book["cost_equity"]*rules.max_total_risk-book["open_risk"]),
                   max(0., book["cost_equity"]-rules.paper_floor-book["open_risk"]))
        quantity = min(quantity, risk/unit_risk,
                       max(0., book["cost_equity"])*rules.max_allocation/(entry*(1+rules.fee_rate)))
    if (rules.nn_limitations and quantity*entry < rules.minimum_notional) or quantity <= 0 or available_cash <= 1e-9:
        raise DataError("Insufficient neural paper cash or risk budget")
    identity = hashlib.sha256(f"{name}:{signal['model_id']}:{signal['signal_end']}".encode()).hexdigest()[:24]
    trade = {"id": identity, "asset": name, "symbol": row["symbol"], "strategy": "neural_network", "side": "long",
             "status": "active", "entry": entry, "entry_quote": quote["ask"], "entry_quote_ms": quote["asof_ms"],
             "stop": stop, "limitations_enabled": rules.nn_limitations, "quantity": quantity, "collateral": quantity*entry,
             "entry_fee": quantity*entry*rules.fee_rate, "initial_risk_usd": quantity*unit_risk,
             "fee_rate": rules.fee_rate, "slippage_rate": rules.slippage_rate,
             "opened_ms": now, "last_bar_end": now, "tracking_gap": False, "realized_pnl": 0., **signal}
    row["trades"].append(trade)
    row["last_signal_end"] = max(row["last_signal_end"], signal["signal_end"])
    document["cash"] -= trade["collateral"]+trade["entry_fee"]
    if abs(document["cash"]) < 1e-9:
        document["cash"] = 0.
    stop_text = f"${stop:,.8f}" if stop is not None else "none (NN SELL only)"
    queue_event(document, identity+":buy", "telegram",
                f"{name} #{identity[:8]} · NEURAL PAPER BUY\nEntry ${entry:,.8f}; stop {stop_text}; quantity {quantity:.8g}\n"
                +( "Exit on SELL classification or stop. " if stop is not None else "Exit on SELL classification. ")
                +"No exchange order.", now, trade)
    return trade


def close_trade(document, trade, price, now, reason):
    if trade["status"] != "active":
        return
    fill = price*(1-trade["slippage_rate"])
    fee = fill*trade["quantity"]*trade["fee_rate"]
    pnl = trade["quantity"]*(fill-trade["entry"])-trade["entry_fee"]-fee
    document["cash"] += fill*trade["quantity"]-fee
    document["realized_pnl"] += pnl
    trade.update(status="stopped" if reason == "stop" else "sold", exit=fill, closed_ms=now,
                 exit_fee=fee, realized_pnl=pnl)
    queue_event(document, trade["id"]+":exit", "telegram",
                f"{trade['asset']} #{trade['id'][:8]} · NEURAL PAPER SELL · {reason.upper()}\n"
                f"Exit ${fill:,.8f}; quantity {trade['quantity']:.8g}; net P/L ${pnl:,.2f}.\nNo exchange order.", now, trade)
