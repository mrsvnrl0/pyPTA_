"""Freeze the earliest common Parente-comparable held-out test scope.

The boundary is determined only from verified 4H candle coverage and a fixed
720-bar live-equivalent Parente history requirement. It never sees model
predictions or performance. Candidate and Parente reports must then use this
same derived experiment, underlying CSV, costs and limitations mode.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .evaluate_nn_candidates import EvaluationError, H4, _sha256, read_bars, read_experiment

PARENTE_HISTORY_BARS = 720
PARENTE_ASSETS = ("BTC", "ETH", "SOL")


def derive(spec, bars):
    if not set(PARENTE_ASSETS).issubset(spec["asset_order"]):
        raise EvaluationError("Source experiment needs comparable BTC, ETH and SOL assets")
    runs = {}
    for asset in PARENTE_ASSETS:
        lengths, previous, consecutive = {}, None, 0
        for t in sorted(bars.get(asset, {})):
            consecutive = consecutive + 1 if previous is not None and t == previous + H4 else 1
            lengths[t] = consecutive
            previous = t
        runs[asset] = lengths
    latest_start = spec["test_end_ms"] - (spec["forward_horizon_bars"] + 1) * H4
    common_start = next((t for t in range(spec["test_start_ms"], latest_start + 1, H4)
                         if all(runs[asset].get(t, 0) >= PARENTE_HISTORY_BARS
                                for asset in PARENTE_ASSETS)), None)
    if common_start is None:
        raise EvaluationError("No common held-out interval follows 720 completed consecutive 4H bars")
    return {**spec, "asset_order": list(PARENTE_ASSETS), "test_start_ms": common_start,
            "comparison_scope": {
                "reason": "Earliest held-out candle with 720 consecutive completed Kraken/USD 4H bars for bundled Parente on BTC/ETH/SOL",
                "warmup_bars": PARENTE_HISTORY_BARS,
                "unsupported_parente_asset": "SUI",
                "rule_frozen_without_prediction_or_performance_inspection": True}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--candles-4h", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        spec = read_experiment(args.experiment)
        bars = read_bars(args.candles_4h, H4)
        comparable = derive(spec, bars)
        comparable["comparison_scope"]["source_experiment_sha256"] = _sha256(args.experiment)
        comparable["comparison_scope"]["candles_4h_sha256"] = _sha256(args.candles_4h)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(comparable, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"test_start_ms": comparable["test_start_ms"],
                          "test_end_ms": comparable["test_end_ms"],
                          "asset_order": comparable["asset_order"],
                          "output": str(args.output)}))
        return 0
    except (EvaluationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"comparison scope derivation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
