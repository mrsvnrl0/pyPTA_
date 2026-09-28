"""Freeze comparable Parente predictions on a held-out Kraken/USD test interval.

This read-only command uses the bundled, unchanged Parente NPZ, its existing
rolling live inference path, and the source-convention 5/2 target function.
Each prediction uses a fixed 720-bar completed history, matching the maximum
Kraken 4H OHLC response available to the live dashboard. The labeler sees a
whole contiguous asset segment so its adjusted EMA is not restarted per sample.

The output is an immutable JSONL input to ``tools.evaluate_nn_candidates``.
It never writes settings, ledger state, or model files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

from adaptive_crypto.core import DataError, H4, validate_candles
from adaptive_crypto.neural import NeuralModel, labels
from .evaluate_nn_candidates import EvaluationError, read_bars, read_experiment

HISTORY_BARS = 720
HORIZON_BARS = 2


def _contiguous_test_segment(asset_bars, start, end):
    stamps = sorted(asset_bars)
    try:
        first_test = stamps.index(start)
        last_test = stamps.index(end - H4)
    except ValueError as exc:
        raise EvaluationError("Held-out Parente interval has missing 4H candles") from exc
    left = first_test
    while left and stamps[left] - stamps[left - 1] == H4:
        left -= 1
    right = last_test
    while right + 1 < len(stamps) and stamps[right + 1] - stamps[right] == H4:
        right += 1
    if right - left + 1 < end // H4 - start // H4 + HISTORY_BARS - 1:
        raise EvaluationError("Parente requires 720 contiguous completed 4H history bars before this test")
    if any(stamps[i] - stamps[i - 1] != H4 for i in range(first_test + 1, last_test + 1)):
        raise EvaluationError("Held-out Parente interval contains a 4H history gap")
    return [asset_bars[t] for t in stamps[left:right + 1]], first_test - left


def freeze_predictions(bars, spec, model):
    """Yield one real model result per label-complete test candle and asset."""
    if spec["forward_horizon_bars"] != HORIZON_BARS:
        raise EvaluationError("Parente source 5/2 baseline requires a two-bar target horizon")
    for asset in spec["asset_order"]:
        if asset not in model.volume_stats:
            raise EvaluationError(f"Parente has no bundled frozen calibration for {asset}")
        if asset not in bars:
            raise EvaluationError(f"Missing {asset} candles for Parente baseline")
        segment, first_test = _contiguous_test_segment(
            bars[asset], spec["test_start_ms"], spec["test_end_ms"])
        targets = labels([bar.c for bar in segment], backward=5, forward=2, convention="source")
        last_signal = spec["test_end_ms"] - (HORIZON_BARS + 1) * H4
        for stamp in range(spec["test_start_ms"], last_signal + 1, H4):
            index = first_test + (stamp - spec["test_start_ms"]) // H4
            if index < HISTORY_BARS - 1:
                raise EvaluationError(f"Insufficient Parente warm-up at {asset} {stamp}")
            window = segment[index - HISTORY_BARS + 1:index + 1]
            validate_candles(window, H4, stamp + H4, minimum=HISTORY_BARS, fresh=False)
            if stamp <= model.trained_through_ms:
                raise EvaluationError("Parente held-out prediction overlaps its training period")
            truth = targets.iloc[index]
            if not math.isfinite(truth) or truth not in (-1., 0., 1.):
                raise EvaluationError(f"Unknown Parente source label at {asset} {stamp}")
            result = model.predict(window, asset)
            if result["signal_end"] != stamp + H4 - 1 or result["model_id"] != model.identity:
                raise EvaluationError("Parente prediction identity or signal candle mismatch")
            probabilities = [result["probabilities"][name] for name in ("BUY", "HOLD", "SELL")]
            if (any(not math.isfinite(p) or p < 0 or p > 1 for p in probabilities)
                    or not math.isclose(sum(probabilities), 1., abs_tol=1e-6)):
                raise EvaluationError("Invalid Parente output probabilities")
            yield {"model_id": "parente_mlp_v1", "artifact_id": model.identity,
                   "asset": asset, "signal_end_ms": stamp + H4 - 1,
                   "probabilities": probabilities, "truth_label_index": int(truth) + 1}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles-4h", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, help="Optional existing Parente NPZ")
    args = parser.parse_args(argv)
    try:
        spec = read_experiment(args.experiment)
        bars = read_bars(args.candles_4h, H4)
        model = NeuralModel(args.model)
        rows = list(freeze_predictions(bars, spec, model))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        print(json.dumps({"model_id": "parente_mlp_v1", "artifact_id": model.identity,
                          "predictions": len(rows), "output": str(args.output),
                          "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))
        return 0
    except (DataError, EvaluationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Parente baseline failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
