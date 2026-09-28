"""Create one fixed, equal-weight four-network held-out prediction file.

Inputs must be four frozen, distinct, trained candidate artifacts on an
identical asset/candle/target grid. This command never fits weights or picks
members from test performance. The member set is the four predeclared model
families: LSTM, GRU, CNN-LSTM and grouped attention-LSTM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from .evaluate_nn_candidates import EvaluationError, _sha256, read_experiment, read_predictions

FAMILIES = ("lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
            "grouped_attention_lstm_v1")
SCHEMA = "fixed-probability-ensemble-v1"
WEIGHTS = (0.25, 0.25, 0.25, 0.25)
CLASSES = ("BUY", "HOLD", "SELL")
FEATURE_SCHEMA = "kraken-ohlcv-16-v1"
LABEL_VERSION = "parente-source-5-2-v1"
H4 = 14_400_000
HISTORY_BARS = 319


def combine(member_files, manifest_files, experiment):
    """Return provenance and ensemble rows from four fully paired files."""
    if len(member_files) != 4 or len(manifest_files) != 4:
        raise EvaluationError("Fixed ensemble requires exactly four candidate predictions and manifests")
    manifests = {}
    for path in manifest_files:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        family = manifest.get("id")
        if family not in FAMILIES or family in manifests:
            raise EvaluationError("Unknown or duplicate candidate manifest family")
        if (manifest.get("feature_schema") != FEATURE_SCHEMA
                or manifest.get("label_version") != LABEL_VERSION
                or manifest.get("classes") != list(CLASSES)
                or manifest.get("timeframe_ms") != H4
                or manifest.get("target_horizon_bars") != 2
                or manifest.get("history_bars") != HISTORY_BARS
                or not isinstance(manifest.get("members"), dict)
                or not manifest["members"]):
            raise EvaluationError(f"Incompatible candidate manifest contract: {family}")
        manifests[family] = {"manifest": manifest, "sha256": _sha256(path), "path": str(path)}
    if set(manifests) != set(FAMILIES):
        raise EvaluationError("Fixed ensemble is missing a predeclared candidate manifest")
    by_family = {}
    for path in member_files:
        identity, rows = read_predictions(path)
        model_id, artifact_id = identity
        if (not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_id)
                or not re.fullmatch(r"[a-z0-9_]+@[0-9a-f]{12}", model_id)):
            raise EvaluationError(f"Invalid versioned member identity: {model_id}")
        family, prefix = model_id.split("@")
        if family not in FAMILIES or prefix != artifact_id[:12] or family in by_family:
            raise EvaluationError(f"Unknown, duplicate or mismatched ensemble family: {model_id}")
        if manifests[family]["manifest"].get("artifact_id") != artifact_id:
            raise EvaluationError(f"Prediction artifact differs from its immutable manifest: {model_id}")
        by_family[family] = {"model_id": model_id, "artifact_id": artifact_id,
                             "predictions_sha256": _sha256(path), "predictions_file": str(path),
                             "rows": rows}
    if set(by_family) != set(FAMILIES):
        raise EvaluationError("Fixed ensemble is missing one or more predeclared architectures")
    reference = by_family[FAMILIES[0]]["rows"]
    keys = set(reference)
    for family in FAMILIES[1:]:
        rows = by_family[family]["rows"]
        if set(rows) != keys:
            raise EvaluationError(f"Ensemble prediction grid differs for {family}")
        for key in keys:
            if rows[key]["truth_label_index"] != reference[key]["truth_label_index"]:
                raise EvaluationError(f"Ensemble truth label differs for {family} at {key}")
    coverage = sorted(set.intersection(*(set(manifests[f]["manifest"]["members"]) for f in FAMILIES)))
    if not coverage:
        raise EvaluationError("Four candidate artifacts have no common supported asset")
    if any(asset not in coverage for asset, _ in keys):
        raise EvaluationError("Prediction includes an asset unsupported by an ensemble member")
    cutoffs = [manifests[f]["manifest"].get("latest_model_selection_through_ms") for f in FAMILIES]
    if any(type(value) is not int or value < 0 for value in cutoffs):
        raise EvaluationError("Candidate manifest lacks a valid model-selection cutoff")
    components = [{"model_id": by_family[f]["model_id"], "artifact_id": by_family[f]["artifact_id"],
                   "manifest_sha256": manifests[f]["sha256"]} for f in FAMILIES]
    identity_payload = {"schema": SCHEMA, "architecture_id": "probability_ensemble_v1",
                        "feature_schema": FEATURE_SCHEMA, "label_version": LABEL_VERSION,
                        "timeframe_ms": H4, "target_horizon_bars": 2,
                        "history_bars": HISTORY_BARS, "classes": list(CLASSES),
                        "weights": list(WEIGHTS), "components": components,
                        "coverage": coverage,
                        "latest_model_selection_through_ms": max(*cutoffs, experiment["test_end_ms"] - 1)}
    artifact_id = hashlib.sha256(json.dumps(identity_payload, sort_keys=True,
                                            separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    model_id = f"probability_ensemble_v1@{artifact_id[:12]}"
    output = []
    for asset, signal_end_ms in sorted(keys):
        member_rows = [by_family[family]["rows"][(asset, signal_end_ms)] for family in FAMILIES]
        probabilities = [sum(weight * row["probabilities"][i]
                             for weight, row in zip(WEIGHTS, member_rows)) for i in range(3)]
        # Preserve exact sum-to-one despite harmless source float rounding.
        norm = sum(probabilities)
        probabilities = [p / norm for p in probabilities]
        output.append({"model_id": model_id, "artifact_id": artifact_id, "asset": asset,
                       "signal_end_ms": signal_end_ms, "probabilities": probabilities,
                       "truth_label_index": member_rows[0]["truth_label_index"]})
    provenance = {"identity_payload": identity_payload, "model_id": model_id, "artifact_id": artifact_id,
                  "member_prediction_inputs": [
                      {key: by_family[f][key] for key in ("model_id", "artifact_id", "predictions_sha256", "predictions_file")}
                      for f in FAMILIES],
                  "member_manifest_inputs": [{"model_id": by_family[f]["model_id"],
                                              "manifest_sha256": manifests[f]["sha256"],
                                              "manifest_file": manifests[f]["path"]} for f in FAMILIES],
                  "prediction_count": len(output)}
    return provenance, output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--member", type=Path, action="append", required=True,
                        help="Repeat exactly four times, once per fixed model family")
    parser.add_argument("--manifest", type=Path, action="append", required=True,
                        help="Repeat exactly four times, once per immutable member bundle")
    parser.add_argument("--experiment", type=Path, required=True,
                        help="Frozen experiment used to set the conservative live eligibility cutoff")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path)
    args = parser.parse_args(argv)
    try:
        provenance, rows = combine(args.member, args.manifest, read_experiment(args.experiment))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        meta_path = args.provenance or args.output.with_suffix(".provenance.json")
        if meta_path == args.output or meta_path.exists() or args.output.exists():
            raise EvaluationError("Refusing to replace an ensemble prediction or provenance file")
        with args.output.open("x", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        provenance["ensemble_predictions_sha256"] = _sha256(args.output)
        with meta_path.open("x", encoding="utf-8") as stream:
            json.dump(provenance, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"model_id": provenance["model_id"],
                          "artifact_id": provenance["artifact_id"],
                          "rows": len(rows), "output": str(args.output),
                          "provenance": str(meta_path)}))
        return 0
    except (EvaluationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ensemble generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
