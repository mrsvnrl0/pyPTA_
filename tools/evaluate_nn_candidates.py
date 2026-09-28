"""Offline, deterministic comparison of frozen NN predictions on Kraken/USD bars.

The command reads archived candles and prediction JSONL. It never opens the
dashboard state, contacts an exchange, trains a model, or sends notifications.
Only completed, aligned bars can be execution proxies. Prices at the next bar
open are midpoint proxies; bid/ask are constructed from a declared spread.

Example::

    python -m tools.evaluate_nn_candidates --predictions frozen.jsonl \
        --candles-4h kraken_240.csv --experiment experiment.json \
        --output report.json

The experiment JSON freezes test_start_ms, test_end_ms (exclusive),
development_cutoff_ms, asset_order, fee_rate, slippage_rate, spread_bps,
initial_equity, and the existing Rules position limits. Pass --candles-5m
only for a complete, verified common 5M archive; otherwise the replay
deliberately uses 4H marks and discloses its intrabar uncertainty.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys

H4 = 14_400_000
M5 = 300_000
CLASSES = ("BUY", "HOLD", "SELL")
SCHEMA = "nn-candidate-evaluation-v1"


class EvaluationError(ValueError):
    """An input cannot support a valid chronological comparison."""


def _number(value, name, *, lower=None, upper=None):
    if isinstance(value, bool):
        raise EvaluationError(f"{name} must be a finite number")
    try:
        out = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise EvaluationError(f"{name} must be a finite number") from exc
    if not math.isfinite(out) or lower is not None and out < lower or upper is not None and out > upper:
        raise EvaluationError(f"{name} is outside its valid range")
    return out


def _integer(value, name, *, lower=0):
    if type(value) is not int or value < lower:
        raise EvaluationError(f"{name} must be an integer of at least {lower}")
    return value


@dataclass(frozen=True)
class Bar:
    asset: str
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float
    interval: int

    @property
    def end(self):
        return self.t + self.interval - 1


def read_bars(path, interval):
    """Read normalized archived rows and reject silent edits/gaps/duplicates.

    Gaps are returned as coverage facts. The replay requires a complete common
    grid in its chosen test interval; it does not interpolate missing prices.
    """
    path = Path(path)
    by_asset = {}
    required = {"asset", "open_time_ms", "open", "high", "low", "close", "volume"}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise EvaluationError(f"{path} needs columns {', '.join(sorted(required))}")
        for line, row in enumerate(reader, 2):
            asset = row["asset"].strip().upper()
            if not asset or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for ch in asset):
                raise EvaluationError(f"Invalid asset in {path} line {line}")
            try:
                t = int(row["open_time_ms"])
            except (TypeError, ValueError) as exc:
                raise EvaluationError(f"Invalid candle timestamp in {path} line {line}") from exc
            if t < 0 or t % interval:
                raise EvaluationError(f"Unaligned candle in {path} line {line}")
            o, h, l, c = (_number(row[key], key, lower=1e-15) for key in ("open", "high", "low", "close"))
            v = _number(row["volume"], "volume", lower=0)
            if not l <= min(o, c) <= max(o, c) <= h:
                raise EvaluationError(f"Inconsistent OHLC in {path} line {line}")
            by_time = by_asset.setdefault(asset, {})
            if t in by_time:
                raise EvaluationError(f"Duplicate or revised {asset} candle at {t}; resolve revisions before evaluation")
            by_time[t] = Bar(asset, t, o, h, l, c, v, interval)
    if not by_asset:
        raise EvaluationError(f"{path} has no candles")
    return by_asset


def read_predictions(path):
    path = Path(path)
    rows, identity = {}, None
    with path.open("r", encoding="utf-8-sig") as stream:
        for line, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                model_id, artifact_id = row["model_id"], row["artifact_id"]
                asset = row["asset"].strip().upper()
                end = _integer(row["signal_end_ms"], "signal_end_ms")
                truth = row.get("truth_label_index")
                probabilities = tuple(_number(v, "probability", lower=0, upper=1) for v in row["probabilities"])
            except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
                raise EvaluationError(f"Malformed prediction at {path}:{line}") from exc
            if (not isinstance(model_id, str) or not model_id or not isinstance(artifact_id, str)
                    or not artifact_id or not asset or len(probabilities) != 3
                    or not math.isclose(sum(probabilities), 1., abs_tol=1e-6)):
                raise EvaluationError(f"Invalid prediction identity/probabilities at {path}:{line}")
            if truth is not None and (type(truth) is not int or truth not in (0, 1, 2)):
                raise EvaluationError(f"Invalid truth label at {path}:{line}")
            predicted = max(range(3), key=lambda i: probabilities[i])  # First class wins an exact tie.
            if "label_index" in row and (type(row["label_index"]) is not int or row["label_index"] != predicted):
                raise EvaluationError(f"Label/probability mismatch at {path}:{line}")
            if identity is None:
                identity = (model_id, artifact_id)
            elif identity != (model_id, artifact_id):
                raise EvaluationError("One prediction file must identify one immutable model artifact")
            key = (asset, end)
            if key in rows:
                raise EvaluationError(f"Duplicate prediction for {asset} candle {end}")
            rows[key] = {"asset": asset, "signal_end_ms": end, "probabilities": probabilities,
                         "label_index": predicted, "truth_label_index": truth}
    if not rows:
        raise EvaluationError(f"{path} has no predictions")
    return identity, rows


def read_experiment(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        spec = json.load(stream)
    required = ("test_start_ms", "test_end_ms", "development_cutoff_ms", "asset_order",
                "initial_equity", "fee_rate", "slippage_rate", "spread_bps")
    absent = [key for key in required if key not in spec]
    if absent:
        raise EvaluationError(f"Experiment is missing: {', '.join(absent)}")
    start = _integer(spec["test_start_ms"], "test_start_ms")
    end = _integer(spec["test_end_ms"], "test_end_ms")
    cutoff = _integer(spec["development_cutoff_ms"], "development_cutoff_ms")
    if start % H4 or end % H4 or end <= start + H4 or cutoff >= start:
        raise EvaluationError("Test boundaries must be aligned, chronological and after all development")
    order = spec["asset_order"]
    if (not isinstance(order, list) or not order or any(not isinstance(a, str) or not a for a in order)
            or len(set(order)) != len(order) or order != [a.upper() for a in order]):
        raise EvaluationError("asset_order must be a unique, explicit uppercase list")
    spec["initial_equity"] = _number(spec["initial_equity"], "initial_equity", lower=1e-15)
    for key in ("fee_rate", "slippage_rate"):
        spec[key] = _number(spec[key], key, lower=0, upper=.05)
        if spec[key] == .05:
            raise EvaluationError(f"{key} must be below 0.05")
    spec["spread_bps"] = _number(spec["spread_bps"], "spread_bps", lower=0, upper=1000)
    defaults = {"nn_stop_loss": .10, "risk_per_trade": .05, "max_total_risk": .10,
                "max_allocation": .75, "paper_floor": 0., "minimum_notional": 10.,
                "max_spread_bps": 30., "signal_window_seconds": 60,
                "forward_horizon_bars": 2, "stress_fee_multiplier": 2.,
                "stress_slippage_multiplier": 2., "stress_spread_multiplier": 2.}
    for key, value in defaults.items():
        spec.setdefault(key, value)
    for key in ("nn_stop_loss", "risk_per_trade", "max_total_risk", "max_allocation"):
        spec[key] = _number(spec[key], key, lower=1e-15, upper=1)
    for key in ("paper_floor", "minimum_notional", "max_spread_bps"):
        spec[key] = _number(spec[key], key, lower=0)
    if spec["paper_floor"] >= spec["initial_equity"] or spec["risk_per_trade"] > spec["max_total_risk"]:
        raise EvaluationError("Invalid paper floor or risk policy")
    for key in ("stress_fee_multiplier", "stress_slippage_multiplier", "stress_spread_multiplier"):
        spec[key] = _number(spec[key], key, lower=1)
    spec["signal_window_seconds"] = _integer(spec["signal_window_seconds"], "signal_window_seconds", lower=1)
    spec["forward_horizon_bars"] = _integer(spec["forward_horizon_bars"], "forward_horizon_bars", lower=1)
    if "primary_limitations_enabled" in spec and type(spec["primary_limitations_enabled"]) is not bool:
        raise EvaluationError("primary_limitations_enabled must be a frozen boolean")
    if "label_version" in spec and (not isinstance(spec["label_version"], str) or not spec["label_version"]):
        raise EvaluationError("label_version must be a nonempty target contract")
    return spec


def _coverage(bars, order, start, end, interval):
    expected = range(start, end, interval)
    missing = {asset: [t for t in expected if t not in bars.get(asset, {})] for asset in order}
    return {"cadence_ms": interval, "expected_bars_per_asset": len(expected),
            "missing_bars": {asset: ts[:20] for asset, ts in missing.items() if ts},
            "missing_bar_counts": {asset: len(ts) for asset, ts in missing.items()},
            "complete": all(not ts for ts in missing.values())}


def verify_source_labels(predictions, bars, spec):
    """Independently audit held-out source-convention Parente 5/2 targets.

    The adjusted EMA is initialized once at the start of each contiguous
    asset-history segment; it never restarts at the beginning of a sample.
    This plain-Python recurrence is independent of the training labeler and
    matches pandas ``ewm(span=5, adjust=True)``.
    """
    if spec.get("label_version") != "parente-source-5-2-v1" or spec["forward_horizon_bars"] != 2:
        raise EvaluationError("Source-label audit requires parente-source-5-2-v1 and two-bar horizon")
    checked = 0
    for asset in spec["asset_order"]:
        ordered = sorted(bars[asset])
        try:
            start_index = ordered.index(spec["test_start_ms"])
            last_index = ordered.index(spec["test_end_ms"] - H4)
        except ValueError as exc:
            raise EvaluationError(f"Missing held-out {asset} bars for source-label audit") from exc
        left = start_index
        while left and ordered[left] - ordered[left - 1] == H4:
            left -= 1
        if any(ordered[i] - ordered[i - 1] != H4 for i in range(start_index + 1, last_index + 1)):
            raise EvaluationError(f"Gap in held-out {asset} source-label interval")
        segment = [bars[asset][t] for t in ordered[left:last_index + 1]]
        decay = 2. / 3.  # 1 - 2/(span + 1), span=5.
        numerator = denominator = 0.
        for index, bar in enumerate(segment):
            numerator = bar.c + decay * numerator
            denominator = 1. + decay * denominator
            stamp = bar.t
            key = (asset, stamp + H4 - 1)
            if key not in predictions:
                continue
            if index + 2 >= len(segment):
                raise EvaluationError("Source label needs two future completed bars")
            change = segment[index + 2].c / (numerator / denominator) - 1.
            expected = 1
            if .038 < abs(change) < .24 * 1.2:
                expected = 0 if change > 0 else 2
            if predictions[key]["truth_label_index"] != expected:
                raise EvaluationError(f"Source-label mismatch for {asset} at {stamp}")
            checked += 1
    if checked != len(predictions):
        raise EvaluationError("Some held-out prediction labels were not independently audited")
    return {"status": "complete", "checked": checked,
            "method": "independent adjusted-EMA(5), 2-bar forward, alpha 0.038, source upper threshold 0.288"}


def classification_metrics(rows):
    rows = [r for r in rows if r["truth_label_index"] is not None]
    if not rows:
        return {"samples": 0, "status": "unavailable", "reason": "No held-out truth labels"}
    matrix = [[0] * 3 for _ in range(3)]
    log_loss, brier, bins = 0., 0., [{"count": 0, "correct": 0, "confidence_sum": 0.} for _ in range(10)]
    for row in rows:
        truth, predicted, p = row["truth_label_index"], row["label_index"], row["probabilities"]
        matrix[truth][predicted] += 1
        log_loss -= math.log(max(p[truth], 1e-15))
        brier += sum((p[i] - (i == truth)) ** 2 for i in range(3)) / 3
        confidence = p[predicted]
        slot = bins[min(9, int(confidence * 10))]
        slot["count"] += 1
        slot["correct"] += predicted == truth
        slot["confidence_sum"] += confidence
    n = len(rows)
    recalls, f1s = [], []
    supports = [sum(matrix[i]) for i in range(3)]
    for i in range(3):
        tp, actual, predicted_total = matrix[i][i], supports[i], sum(matrix[j][i] for j in range(3))
        if actual:
            recalls.append(tp / actual)
        precision = tp / predicted_total if predicted_total else 0.
        recall = tp / actual if actual else 0.
        f1s.append(2 * precision * recall / (precision + recall) if precision + recall else 0.)
    reliability = [{"lower": i / 10, "upper": (i + 1) / 10, "count": b["count"],
                    "accuracy": b["correct"] / b["count"] if b["count"] else None,
                    "mean_confidence": b["confidence_sum"] / b["count"] if b["count"] else None}
                   for i, b in enumerate(bins)]
    ece = sum(b["count"] / n * abs(b["accuracy"] - b["mean_confidence"])
              for b in reliability if b["count"])
    return {"status": "complete", "samples": n, "class_order": list(CLASSES),
            "class_counts": dict(zip(CLASSES, supports)), "confusion_matrix_truth_rows_predicted_columns": matrix,
            "accuracy": sum(matrix[i][i] for i in range(3)) / n,
            "balanced_accuracy_present_classes": sum(recalls) / len(recalls),
            "macro_f1_all_three_classes": sum(f1s) / 3, "log_loss": log_loss / n,
            "multiclass_brier_divided_by_three": brier / n, "expected_calibration_error": ece,
            "reliability_ten_bins": reliability}


def _mid_quote(mid, spread_bps):
    half = spread_bps / 20_000
    return mid * (1 - half), mid * (1 + half)


def replay(bars, predictions, spec, *, limitations, stress=False, interval=H4, mode_schedule=None):
    """Replay the live long-only ledger policy, then mark at exit liquidation value.

    Each observation's prior completed bar can confirm a stop. Current-bar
    signals fill at this bar's open, never at their own completed close. The
    entry bar's later low is therefore eligible at the next observation in
    this open-fill proxy; a real post-open quote has less certain ordering.
    """
    start, end, order = spec["test_start_ms"], spec["test_end_ms"], spec["asset_order"]
    fee = spec["fee_rate"] * (spec["stress_fee_multiplier"] if stress else 1)
    slip = spec["slippage_rate"] * (spec["stress_slippage_multiplier"] if stress else 1)
    spread = spec["spread_bps"] * (spec["stress_spread_multiplier"] if stress else 1)
    if fee >= .05 or slip >= .05 or spread >= 20000:
        raise EvaluationError("Stressed transaction costs exceed the supported bounds")
    cash = initial = spec["initial_equity"]
    active, closed, marks = [], [], []
    current_mid, previous = {}, {}
    paid_fees = slippage_cost = spread_cost = 0.
    attempted = {"buy": 0, "sell": 0, "hold": 0, "missed_window": 0,
                 "insufficient_budget": 0, "spread_rejected": 0, "duplicate_or_active": 0}
    stop_uncertainty = exposure_marks = 0
    if mode_schedule is not None:
        if (not isinstance(mode_schedule, list) or not mode_schedule
                or any(type(item.get("from_ms")) is not int or type(item.get("enabled")) is not bool
                       for item in mode_schedule)
                or [item["from_ms"] for item in mode_schedule] != sorted({item["from_ms"] for item in mode_schedule})
                or mode_schedule[0]["from_ms"] > start):
            raise EvaluationError("Mode schedule needs distinct ascending timestamps and an initial mode")

    def mode_at(t):
        if mode_schedule is None:
            return limitations
        return next(item["enabled"] for item in reversed(mode_schedule) if item["from_ms"] <= t)

    def equity():
        return cash + sum(t["quantity"] * _mid_quote(current_mid[t["asset"]], spread)[0]
                          * (1 - t["slippage_rate"]) * (1 - t["fee_rate"]) for t in active)

    def close(trade, quote_bid, at, reason, *, quote_mid=None, exact=True):
        nonlocal cash, paid_fees, slippage_cost, spread_cost
        fill = quote_bid * (1 - trade["slippage_rate"])
        exit_fee = trade["quantity"] * fill * trade["fee_rate"]
        pnl = trade["quantity"] * (fill - trade["entry"]) - trade["entry_fee"] - exit_fee
        cash += trade["quantity"] * fill - exit_fee
        paid_fees += exit_fee
        slippage_cost += trade["quantity"] * quote_bid * trade["slippage_rate"]
        if quote_mid is not None:
            spread_cost += trade["quantity"] * max(0., quote_mid - quote_bid)
        active.remove(trade)
        closed.append({**trade, "exit": fill, "closed_ms": at, "exit_fee": exit_fee,
                       "realized_pnl": pnl, "reason": reason, "exit_time_exact": exact,
                       "exit_time_basis": "bar_open_proxy" if exact else "completed_bar_range_confirmation"})

    for t in range(start, end, interval):
        for asset in order:
            bar = bars[asset][t]
            current_mid[asset] = bar.o
        for asset in order:  # The configured scan order controls shared-cash allocation.
            bar = bars[asset][t]
            stopped = False
            prior = previous.get(asset)
            if prior:
                for trade in list(active):
                    if trade["asset"] != asset or trade["stop"] is None or prior.t < trade["opened_ms"]:
                        continue
                    prior_low_bid, _ = _mid_quote(prior.l, spread)
                    if prior_low_bid <= trade["stop"]:
                        prior_open_bid, _ = _mid_quote(prior.o, spread)
                        fill_bid = min(prior_open_bid, trade["stop"])
                        # The stop is a bid threshold. Estimate only the
                        # declared half-spread, not the full price move.
                        implied_mid = fill_bid / (1 - spread / 20_000)
                        close(trade, fill_bid, t, "stop", quote_mid=implied_mid, exact=False)
                        stop_uncertainty += 1
                        stopped = True
            bid, ask = _mid_quote(bar.o, spread)
            for trade in list(active):
                if trade["asset"] == asset and trade["stop"] is not None and bid <= trade["stop"]:
                    close(trade, bid, t, "gap_stop", quote_mid=bar.o)
                    stopped = True
            if t % H4 == 0:
                signal = predictions.get((asset, t - 1))
                if signal is not None:
                    if t - signal["signal_end_ms"] > spec["signal_window_seconds"] * 1000:
                        attempted["missed_window"] += 1
                    else:
                        label = CLASSES[signal["label_index"]]
                        limited = mode_at(t)
                        attempted[label.lower()] += 1
                        own = [p for p in active if p["asset"] == asset]
                        if label == "SELL":
                            for trade in own:
                                close(trade, bid, t, "sell", quote_mid=bar.o)
                        elif label == "BUY":
                            if stopped or limited and own:
                                attempted["duplicate_or_active"] += 1
                            elif limited and spread > spec["max_spread_bps"]:
                                attempted["spread_rejected"] += 1
                            else:
                                entry = ask * (1 + slip)
                                stop = entry * (1 - spec["nn_stop_loss"]) if limited else None
                                stop_fill = stop * (1 - slip) if stop is not None else 0.
                                unit_risk = entry - stop_fill + fee * (entry + stop_fill)
                                cost_equity = cash + sum(p["collateral"] for p in active)
                                open_risk = sum(p["initial_risk_usd"] for p in active)
                                qty = max(0., cash) / (entry * (1 + fee))
                                if limited:
                                    risk_budget = min(cost_equity * spec["risk_per_trade"],
                                                      max(0., cost_equity * spec["max_total_risk"] - open_risk),
                                                      max(0., cost_equity - spec["paper_floor"] - open_risk))
                                    qty = min(qty, risk_budget / unit_risk,
                                              max(0., cost_equity) * spec["max_allocation"] / (entry * (1 + fee)))
                                if qty <= 0 or cash <= 1e-9 or limited and qty * entry < spec["minimum_notional"]:
                                    attempted["insufficient_budget"] += 1
                                else:
                                    collateral = qty * entry
                                    entry_fee = collateral * fee
                                    trade = {"asset": asset, "signal_end_ms": signal["signal_end_ms"],
                                             "opened_ms": t, "entry": entry, "quantity": qty,
                                             "collateral": collateral, "entry_fee": entry_fee,
                                             "initial_risk_usd": qty * unit_risk, "stop": stop,
                                             "fee_rate": fee, "slippage_rate": slip,
                                             "limitations_enabled": limited}
                                    cash -= collateral + entry_fee
                                    if abs(cash) < 1e-9:
                                        cash = 0.
                                    paid_fees += entry_fee
                                    slippage_cost += qty * ask * slip
                                    spread_cost += qty * (ask - bar.o)
                                    active.append(trade)
            previous[asset] = bar
        exposure_marks += bool(active)
        marks.append({"time_ms": t, "equity": equity()})
    # Confirm stops in the final completed bar, then use one common terminal
    # closing quote for every surviving position. This is evaluation only.
    for asset in order:
        prior = previous[asset]
        for trade in list(active):
            if trade["asset"] == asset and trade["stop"] is not None and prior.t >= trade["opened_ms"]:
                prior_low_bid, _ = _mid_quote(prior.l, spread)
                if prior_low_bid <= trade["stop"]:
                    prior_open_bid, _ = _mid_quote(prior.o, spread)
                    fill_bid = min(prior_open_bid, trade["stop"])
                    implied_mid = fill_bid / (1 - spread / 20_000)
                    close(trade, fill_bid, end, "stop", quote_mid=implied_mid, exact=False)
                    stop_uncertainty += 1
        current_mid[asset] = prior.c
    marks.append({"time_ms": end, "equity": equity()})
    for asset in order:
        bid, _ = _mid_quote(current_mid[asset], spread)
        for trade in list(active):
            if trade["asset"] == asset:
                close(trade, bid, end, "terminal_liquidation", quote_mid=current_mid[asset])
    if not math.isclose(cash, marks[-1]["equity"], abs_tol=1e-7, rel_tol=1e-10):
        raise EvaluationError("Terminal liquidation does not reconcile to net-liquidation equity")
    peak, max_drawdown = initial, 0.
    for mark in marks:
        peak = max(peak, mark["equity"])
        max_drawdown = max(max_drawdown, 1 - mark["equity"] / peak)
    natural = [t for t in closed if t["reason"] != "terminal_liquidation"]
    paired = [marks[i] for i in range(0, len(marks) - 1, H4 // interval)] + [marks[-1]]
    return {"status": "complete", "limitations_enabled": "scheduled" if mode_schedule else limitations,
            "cost_scenario": "stress" if stress else "base",
            "initial_equity": initial, "final_net_liquidation_equity": cash,
            "net_return": cash / initial - 1, "max_sampled_drawdown": max_drawdown,
            "mark_cadence_ms": interval, "mark_count": len(marks),
            "paired_equity_4h": paired, "fees_paid_usd": paid_fees,
            "slippage_cost_usd": slippage_cost, "synthetic_spread_cost_usd": spread_cost,
            "turnover_notional_usd": sum(t["collateral"] + t["exit"] * t["quantity"] for t in closed),
            "completed_natural_trades": len(natural), "terminal_liquidations": len(closed) - len(natural),
            "time_in_market_fraction": exposure_marks / max(1, len(marks) - 1),
            "stop_range_confirmations_with_unknown_intrabar_time": stop_uncertainty,
            "attempts": attempted, "trades": closed,
            "execution": "Predicted completed 4H signal fills on next observation open; synthetic bid/ask, recorded fee/slippage, entry-bar low eligible at next observation, stop confirmation after a full bar, terminal close liquidation"}


def _buy_and_hold(bars, spec, *, interval, stress=False):
    order, start, end = spec["asset_order"], spec["test_start_ms"], spec["test_end_ms"]
    fee = spec["fee_rate"] * (spec["stress_fee_multiplier"] if stress else 1)
    slip = spec["slippage_rate"] * (spec["stress_slippage_multiplier"] if stress else 1)
    spread = spec["spread_bps"] * (spec["stress_spread_multiplier"] if stress else 1)
    allocation = spec["initial_equity"] / len(order)
    quantities = {}
    for asset in order:
        _, ask = _mid_quote(bars[asset][start].o, spread)
        quantities[asset] = allocation / (ask * (1 + slip) * (1 + fee))
    equity = 0.
    for asset in order:
        bid, _ = _mid_quote(bars[asset][end - interval].c, spread)
        equity += quantities[asset] * bid * (1 - slip) * (1 - fee)
    return {"status": "complete", "description": "Equal initial cash per asset, next-observation open entry, terminal close exit, same synthetic spread/fees/slippage; no stops",
            "final_net_liquidation_equity": equity, "net_return": equity / spec["initial_equity"] - 1}


def evaluate(predictions, bars_4h, spec, *, bars_5m=None, identity=("unknown", "unknown"), hashes=None,
             verify_labels=False, scope_predictions=False):
    start, end, order = spec["test_start_ms"], spec["test_end_ms"], spec["asset_order"]
    excluded_assets = sorted({asset for asset, _ in predictions if asset not in order})
    predictions = {key: value for key, value in predictions.items() if key[0] in order}
    coverage_4h = _coverage(bars_4h, order, start, end, H4)
    report = {"schema": SCHEMA, "model_id": identity[0], "artifact_id": identity[1],
              "status": "unavailable", "reason": None, "experiment": spec,
              "input_sha256": hashes or {}, "coverage_4h": coverage_4h,
              "excluded_prediction_assets_outside_declared_scope": excluded_assets,
              "class_order": list(CLASSES), "tie_rule": "first class in BUY/HOLD/SELL order",
              "recommendation": "no demonstrated winner"}
    if not coverage_4h["complete"]:
        report["reason"] = "Missing 4H test bars; no interpolation or fills across gaps"
        return report
    expected = {(asset, t + H4 - 1) for asset in order
                for t in range(start, end - spec["forward_horizon_bars"] * H4, H4)}
    unknown = set(predictions) - expected
    if unknown and not scope_predictions:
        raise EvaluationError(f"Prediction is outside the held-out, label-complete test interval: {sorted(unknown)[:3]}")
    report["excluded_prediction_rows_outside_declared_time_scope"] = {
        "count": len(unknown), "first": sorted(unknown)[:20],
        "explicit_scoping_enabled": scope_predictions}
    if scope_predictions:
        predictions = {key: value for key, value in predictions.items() if key in expected}
    missing = expected - set(predictions)
    report["prediction_coverage"] = {"expected": len(expected), "available": len(predictions),
                                     "missing": len(missing), "first_missing": sorted(missing)[:20]}
    missing_truth = [key for key, row in predictions.items() if row["truth_label_index"] is None]
    if missing or missing_truth:
        report["reason"] = "Missing held-out predictions or truth labels; comparison incomplete"
        return report
    for asset, signal_end in predictions:
        if signal_end - H4 + 1 not in bars_4h[asset]:
            raise EvaluationError(f"Prediction has no matching completed 4H bar: {asset} {signal_end}")
        if signal_end <= spec["development_cutoff_ms"]:
            raise EvaluationError("Predictions overlap fitting/tuning/calibration/model-selection period")
    if verify_labels:
        report["label_verification"] = verify_source_labels(predictions, bars_4h, spec)
    else:
        report["label_verification"] = {"status": "not_run", "reason": "Pass --verify-source-labels to audit truth labels"}
    interval, bars = H4, bars_4h
    if bars_5m is not None:
        coverage_5m = _coverage(bars_5m, order, start, end, M5)
        report["coverage_5m"] = coverage_5m
        if coverage_5m["complete"]:
            interval, bars = M5, bars_5m
        else:
            report["five_minute_status"] = "Incomplete 5M coverage; common 4H valuation/stop proxy used"
    report["mark_cadence_ms"] = interval
    report["stop_timing_limitation"] = ("OHLC range confirms a stop only after the bar; intrabar crossing/fill time is unknown. "
                                         "Replay fills at the bar open, so its later low is eligible at the next observation; "
                                         "a real post-open quote may have entered after that low.")
    report["quote_proxy"] = "Candle open/close midpoint with declared symmetric spread; not historical Kraken bid/ask"
    report["classification"] = {
        "by_asset": {asset: classification_metrics([row for (name, _), row in predictions.items() if name == asset])
                     for asset in order},
        "combined": classification_metrics(predictions.values())}
    report["replay"] = {f"{'limited' if limited else 'unlimited'}_{'stress' if stress else 'base'}":
                        replay(bars, predictions, spec, limitations=limited, stress=stress, interval=interval)
                        for limited in (True, False) for stress in (False, True)}
    report["per_asset_replay"] = {
        asset: {"limited_base": replay(bars, {key: value for key, value in predictions.items() if key[0] == asset},
                                       {**spec, "asset_order": [asset]}, limitations=True, interval=interval),
                "unlimited_base": replay(bars, {key: value for key, value in predictions.items() if key[0] == asset},
                                         {**spec, "asset_order": [asset]}, limitations=False, interval=interval)}
        for asset in order}
    report["baselines"] = {"cash_no_trade": {"net_return": 0., "final_net_liquidation_equity": spec["initial_equity"]},
                           "equal_cash_buy_and_hold_base": _buy_and_hold(bars, spec, interval=interval),
                           "equal_cash_buy_and_hold_stress": _buy_and_hold(bars, spec, interval=interval, stress=True),
                           "majority_class": {"status": "requires frozen training-only class counts/predictions"},
                           "linear_logistic": {"status": "requires separately trained held-out predictions"}}
    report["status"] = "complete"
    report["reason"] = None
    return report


def _sha256(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--candles-4h", type=Path, required=True)
    parser.add_argument("--candles-5m", type=Path)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-source-labels", action="store_true",
                        help="Independently recompute source-convention 5/2 truth labels")
    parser.add_argument("--scope-predictions", action="store_true",
                        help="Explicitly exclude frozen predictions outside a declared comparison date range")
    args = parser.parse_args(argv)
    try:
        spec = read_experiment(args.experiment)
        identity, predictions = read_predictions(args.predictions)
        bars_4h = read_bars(args.candles_4h, H4)
        bars_5m = read_bars(args.candles_5m, M5) if args.candles_5m else None
        hashes = {key: _sha256(path) for key, path in (("predictions", args.predictions),
                  ("candles_4h", args.candles_4h), ("experiment", args.experiment))}
        if args.candles_5m:
            hashes["candles_5m"] = _sha256(args.candles_5m)
        report = evaluate(predictions, bars_4h, spec, bars_5m=bars_5m, identity=identity, hashes=hashes,
                          verify_labels=args.verify_source_labels,
                          scope_predictions=args.scope_predictions)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"status": report["status"], "reason": report["reason"],
                          "model_id": identity[0], "output": str(args.output)}))
        return 0 if report["status"] == "complete" else 2
    except (EvaluationError, OSError, json.JSONDecodeError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
