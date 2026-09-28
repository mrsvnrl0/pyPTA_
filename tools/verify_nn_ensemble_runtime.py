"""Check frozen equal-weight ensemble predictions through the live CPU adapter.

This read-only offline check detects feature, scaler, class-order, component,
and probability-average drift after the four trained manifests are frozen.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from adaptive_crypto.core import Candle, H4
from adaptive_crypto.neural_models import EnsembleModel, HISTORY_BARS
from .evaluate_nn_candidates import EvaluationError, _sha256, read_predictions


CLASSES = ("BUY", "HOLD", "SELL")
TOLERANCE = 1e-5


def verify(bundle_path, prediction_path, candle_path):
    bundle_path, prediction_path, candle_path = map(Path, (bundle_path, prediction_path, candle_path))
    manifest = json.loads((bundle_path / "manifest.json").read_text(encoding="utf-8"))
    model_id = f"probability_ensemble_v1@{manifest['artifact_id'][:12]}"
    model = EnsembleModel(model_id, bundle_path)
    identity, rows = read_predictions(prediction_path)
    if identity != (model_id, model.artifact_id):
        raise EvaluationError("Frozen ensemble predictions do not identify the selected bundle")
    candles = {}
    with candle_path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            asset = row["asset"]
            bar = Candle(int(row["open_time_ms"]),
                         *(float(row[key]) for key in ("open", "high", "low", "close", "volume")), H4)
            by_asset = candles.setdefault(asset, {})
            if bar.t in by_asset:
                raise EvaluationError("Duplicate candle in ensemble runtime check")
            by_asset[bar.t] = bar
    max_difference, checked = 0.0, 0
    for asset in sorted(model.coverage):
        records = sorted((row for (name, _), row in rows.items() if name == asset),
                         key=lambda row: row["signal_end_ms"])
        if len(records) < 3:
            raise EvaluationError(f"Not enough frozen ensemble signals for {asset}")
        for item in (records[0], records[len(records) // 2], records[-1]):
            last_open = item["signal_end_ms"] - H4 + 1
            window = [candles[asset].get(last_open - H4 * back)
                      for back in reversed(range(HISTORY_BARS))]
            if any(bar is None for bar in window):
                raise EvaluationError(f"Ensemble runtime history is incomplete for {asset}")
            native = model.predict(window, asset)
            if native["signal_end"] != item["signal_end_ms"]:
                raise EvaluationError("Ensemble decision candle changed")
            delta = max(abs(native["probabilities"][label] - item["probabilities"][i])
                        for i, label in enumerate(CLASSES))
            if delta > TOLERANCE:
                raise EvaluationError(f"Live ensemble probability mismatch on {asset}: {delta}")
            max_difference = max(max_difference, delta)
            checked += 1
    return {"model_id": model_id, "artifact_id": model.artifact_id,
            "bundle_manifest_sha256": _sha256(bundle_path / "manifest.json"),
            "frozen_predictions_sha256": _sha256(prediction_path),
            "candles_4h_sha256": _sha256(candle_path),
            "checked": checked, "max_probability_difference": max_difference,
            "tolerance": TOLERANCE, "status": "pass"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--candles-4h", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = verify(args.bundle, args.predictions, args.candles_4h)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
