"""Fit two offline, non-selectable baselines on the frozen candidate protocol.

The majority baseline emits training-only empirical class frequencies. The
linear baseline is a genuine per-asset 16-feature multinomial logistic fit,
using training-only standardization and validation-only early stopping. Both
predict the identical 4H held-out grid as the four neural candidates. No
dashboard settings, live state, or model registry files are touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from adaptive_crypto.candidate_features import (FEATURE_NAMES, FEATURE_SCHEMA, HISTORY_BARS,
                                                RAW_CONTEXT_BARS, feature_rows)
from adaptive_crypto.core import H4
from .candidate_train_pipeline import read_data, source_labels, sha256_path
from .evaluate_nn_candidates import read_experiment, read_predictions


BASELINE_SCHEMA = "nn-simple-baselines-v1"
RECIPE = {"logistic_learning_rate": 0.05, "logistic_l2": 0.001,
          "logistic_max_epochs": 400, "logistic_patience": 25,
          "logistic_min_scale": 1e-8, "class_weights": None,
          "calibration": None, "decision": "BUY,HOLD,SELL argmax; first-class tie"}
KIND_IDS = {"majority": "training_majority_v1", "logistic": "linear_logistic_v1"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _cross_entropy(x, y, weights, bias):
    logits = x @ weights + bias
    logits -= logits.max(axis=1, keepdims=True)
    probabilities = np.exp(logits)
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    return float(-np.log(np.clip(probabilities[np.arange(len(y)), y], 1e-15, 1)).mean())


def _probabilities(x, weights, bias):
    logits = x @ weights + bias
    logits -= logits.max(axis=1, keepdims=True)
    result = np.exp(logits)
    result /= result.sum(axis=1, keepdims=True)
    return result


def _asset_samples(segments, spec):
    samples = {split: {"x": [], "y": [], "ends": []} for split in ("train", "validation", "test")}
    for candles in segments:
        if len(candles) < HISTORY_BARS + 2:
            continue
        rows, row_ends = feature_rows(candles)
        truth = source_labels([candle.c for candle in candles])
        for index in range(HISTORY_BARS - 1, len(candles) - 2):
            stamp = candles[index].t
            target_open = candles[index + 2].t
            if stamp < spec["aligned_history_start_ms"]:
                continue
            if stamp < spec["validation_start_ms"] and target_open < spec["validation_start_ms"]:
                split = "train"
            elif spec["validation_start_ms"] <= stamp < spec["test_start_ms"] and target_open < spec["test_start_ms"]:
                split = "validation"
            elif spec["test_start_ms"] <= stamp < spec["test_end_ms"] and target_open < spec["test_end_ms"]:
                split = "test"
            else:
                continue
            feature_index = index - (RAW_CONTEXT_BARS - 1)
            if feature_index < 0 or row_ends[feature_index] != candles[index].end:
                raise ValueError("Feature row does not match the complete signal candle")
            label = int(truth[index])
            if label not in (0, 1, 2):
                raise ValueError("Unknown label entered a split")
            samples[split]["x"].append(rows[feature_index])
            samples[split]["y"].append(label)
            samples[split]["ends"].append(candles[index].end)
    result = {}
    for split, values in samples.items():
        if not values["x"]:
            raise ValueError(f"No {split} samples after chronological purge")
        result[split] = {
            "x": np.asarray(values["x"], dtype=np.float64),
            "y": np.asarray(values["y"], dtype=np.int64),
            "ends": np.asarray(values["ends"], dtype=np.int64),
        }
    return result


def _fit_logistic(train, validation):
    x_train, y_train = train
    x_validation, y_validation = validation
    weights = np.zeros((len(FEATURE_NAMES), 3), dtype=np.float64)
    priors = (np.bincount(y_train, minlength=3) + 1.) / (len(y_train) + 3.)
    bias = np.log(priors)
    best = (float("inf"), None, None, None)
    stale = 0
    history = []
    for epoch in range(1, RECIPE["logistic_max_epochs"] + 1):
        probabilities = _probabilities(x_train, weights, bias)
        probabilities[np.arange(len(y_train)), y_train] -= 1.
        grad_weights = x_train.T @ probabilities / len(y_train) + RECIPE["logistic_l2"] * weights
        grad_bias = probabilities.mean(axis=0)
        weights -= RECIPE["logistic_learning_rate"] * grad_weights
        bias -= RECIPE["logistic_learning_rate"] * grad_bias
        loss = _cross_entropy(x_validation, y_validation, weights, bias)
        history.append(loss)
        if loss < best[0] - 1e-8:
            best = (loss, epoch, weights.copy(), bias.copy())
            stale = 0
        else:
            stale += 1
            if stale >= RECIPE["logistic_patience"]:
                break
    if best[1] is None or not np.isfinite(best[2]).all() or not np.isfinite(best[3]).all():
        raise ValueError("Logistic baseline fit did not converge to finite weights")
    return best[2], best[3], {"epochs_run": len(history), "selected_epoch": best[1],
                              "best_validation_cross_entropy": best[0],
                              "weight_l2_norm": float(np.linalg.norm(best[2]))}


def fit_baselines(data_dir, experiment, reference):
    spec = read_experiment(experiment)
    if spec.get("feature_schema") != FEATURE_SCHEMA or spec.get("label_version") != "parente-source-5-2-v1":
        raise ValueError("Simple baselines require the frozen 16-feature/source-target protocol")
    segments, audit, source = read_data(data_dir, tuple(spec["asset_order"]))
    expected = read_predictions(reference)[1]
    model_data, prediction_rows = {}, {kind: [] for kind in KIND_IDS}
    for asset in spec["asset_order"]:
        split = _asset_samples(segments[asset], spec)
        train, validation, test = (split[key] for key in ("train", "validation", "test"))
        mean = train["x"].mean(axis=0)
        std = train["x"].std(axis=0)
        scale = np.where(std > RECIPE["logistic_min_scale"], std, 1.)
        x_train = (train["x"] - mean) / scale
        x_validation = (validation["x"] - mean) / scale
        x_test = (test["x"] - mean) / scale
        weights, bias, fit = _fit_logistic((x_train, train["y"]), (x_validation, validation["y"]))
        counts = np.bincount(train["y"], minlength=3)
        priors = (counts + 1.) / (len(train["y"]) + 3.)
        outputs = {"majority": np.repeat(priors[None, :], len(test["y"]), axis=0),
                   "logistic": _probabilities(x_test, weights, bias)}
        for kind, probabilities in outputs.items():
            for end, truth, probability in zip(test["ends"], test["y"], probabilities):
                key = (asset, int(end))
                if key not in expected or expected[key]["truth_label_index"] != int(truth):
                    raise ValueError(f"Baseline target/grid differs from frozen neural candidates at {key}")
                prediction_rows[kind].append({"asset": asset, "signal_end_ms": int(end),
                                               "probabilities": [float(value) for value in probability],
                                               "truth_label_index": int(truth)})
        model_data[asset] = {
            "source_dataset_sha256": audit[asset]["dataset_sha256"],
            "sample_counts": {key: len(split[key]["y"]) for key in split},
            "train_class_counts": {name: int(counts[index]) for index, name in enumerate(("BUY", "HOLD", "SELL"))},
            "train_only_mean": mean.tolist(), "train_only_scale": scale.tolist(),
            "majority_train_smoothed_priors": priors.tolist(),
            "logistic_weights": weights.tolist(), "logistic_bias": bias.tolist(),
            "logistic_fit": fit,
        }
    reference_keys = set(expected)
    for kind, rows in prediction_rows.items():
        if {(row["asset"], row["signal_end_ms"]) for row in rows} != reference_keys:
            raise ValueError(f"{kind} baseline does not cover the frozen candidate held-out grid")
        rows.sort(key=lambda row: (row["asset"], row["signal_end_ms"]))
    provenance = {
        "schema": BASELINE_SCHEMA, "scope": "research-only; not a selectable dashboard model",
        "source": "verified historical Kraken/USD 4H archive plus committed REST tail",
        "source_provenance_sha256": sha256_path(Path(data_dir) / "provenance.json"),
        "experiment_sha256": sha256_path(experiment), "reference_predictions_sha256": sha256_path(reference),
        "feature_schema": FEATURE_SCHEMA, "feature_names": list(FEATURE_NAMES),
        "history_bars": HISTORY_BARS, "feature_input": "final causal 16-feature row, no future labels",
        "target": "source-convention Parente 5/2", "split_policy": spec.get("split_policy"),
        "recipe": RECIPE, "asset_order": list(spec["asset_order"]),
        "assets": model_data,
        "baseline_descriptions": {
            "majority": "Constant Laplace-smoothed training class frequencies; argmax is the training majority class",
            "logistic": "Per-asset trained multinomial softmax linear classifier with L2 penalty and validation-only early stopping",
        },
    }
    return provenance, prediction_rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--reference-predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New offline experiment directory")
    args = parser.parse_args(argv)
    provenance, predictions = fit_baselines(args.data, args.experiment, args.reference_predictions)
    args.output.mkdir(parents=True, exist_ok=False)
    for kind, rows in predictions.items():
        identity = {"schema": BASELINE_SCHEMA, "kind": kind, "provenance": provenance}
        artifact_id = hashlib.sha256(_canonical(identity)).hexdigest()
        model_id = f"{KIND_IDS[kind]}@{artifact_id[:12]}"
        path = args.output / f"{kind}.predictions.jsonl"
        with path.open("x", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps({"model_id": model_id, "artifact_id": artifact_id, **row},
                                        sort_keys=True, allow_nan=False) + "\n")
        provenance.setdefault("outputs", {})[kind] = {"model_id": model_id, "artifact_id": artifact_id,
                                                      "predictions_sha256": sha256_path(path), "rows": len(rows)}
    (args.output / "provenance.json").write_text(json.dumps(
        provenance, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(provenance["outputs"], sort_keys=True))


if __name__ == "__main__":
    main()
