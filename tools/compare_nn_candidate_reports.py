"""Compare frozen held-out candidate reports with an identical-scope Parente run.

This is a conservative evidence gate, not an automatic model selector. It
never edits Settings or model artifacts. A report can describe a technically
valid selectable model while this comparison has no demonstrated winner.

    python -m tools.compare_nn_candidate_reports --parente parente_report.json \
        --candidate lstm_report.json --candidate gru_report.json \
        --output comparison.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import re
import sys

from .evaluate_nn_candidates import EvaluationError, SCHEMA, _sha256

COMPARISON_SCHEMA = "nn-candidate-comparison-v1"


def _paired_bootstrap(candidate, reference, *, block=42, repeats=500, seed=2987):
    """Seven-day blocks of paired 4H log-return differences, fixed seed."""
    left, right = candidate["paired_equity_4h"], reference["paired_equity_4h"]
    if len(left) != len(right) or len(left) < 3 or any(a["time_ms"] != b["time_ms"] for a, b in zip(left, right)):
        raise EvaluationError("Paired equity timestamps differ")
    delta = []
    for i in range(1, len(left)):
        a0, a1, b0, b1 = left[i - 1]["equity"], left[i]["equity"], right[i - 1]["equity"], right[i]["equity"]
        if min(a0, a1, b0, b1) <= 0:
            raise EvaluationError("Nonpositive equity prevents log-return resampling")
        delta.append(math.log(a1 / a0) - math.log(b1 / b0))
    n = len(delta)
    block = min(block, n)
    rng = random.Random(seed)
    sums = []
    for _ in range(repeats):
        sample = []
        while len(sample) < n:
            first = rng.randrange(n - block + 1)
            sample.extend(delta[first:first + block])
        sums.append(sum(sample[:n]))
    sums.sort()
    return {"method": "paired moving-block bootstrap of 4H log-return differences",
            "block_bars": block, "replicates": repeats, "seed": seed,
            "observed_log_return_difference": sum(delta),
            "confidence_95_log_return_difference": [sums[int(.025 * repeats)], sums[int(.975 * repeats) - 1]]}


def _scope(report):
    if report.get("schema") != SCHEMA:
        raise EvaluationError("Unknown evaluator report schema")
    spec = report.get("experiment", {})
    keys = ("test_start_ms", "test_end_ms", "development_cutoff_ms", "asset_order",
            "initial_equity", "fee_rate", "slippage_rate", "spread_bps",
            "nn_stop_loss", "risk_per_trade", "max_total_risk", "max_allocation",
            "paper_floor", "minimum_notional", "max_spread_bps",
            "signal_window_seconds", "forward_horizon_bars",
            "stress_fee_multiplier", "stress_slippage_multiplier", "stress_spread_multiplier",
            "primary_limitations_enabled", "label_version")
    return {key: spec.get(key) for key in keys}


def compare(parente, candidates, *, report_hashes=None, validated_artifacts=None):
    """Return a deterministic conservative verdict and every exclusion reason.

    Recommendation requires a predeclared primary mode and target label,
    identical market/split/cost scope, and comparable complete reports.
    The final test is used once for confirmation; this function does not
    choose hyperparameters, retrain, or change the running model.
    """
    reference_scope = _scope(parente)
    validated_artifacts = validated_artifacts or {}
    out = {"schema": COMPARISON_SCHEMA, "recommendation": "no demonstrated winner",
           "recommended_model_id": None, "primary_limitations_enabled": reference_scope["primary_limitations_enabled"],
           "benchmark_model_id": parente.get("model_id"), "comparison_scope": reference_scope,
           "report_sha256": report_hashes or {}, "candidates": {}}
    global_reasons = []
    if parente.get("status") != "complete":
        global_reasons.append("Parente held-out benchmark is incomplete")
    if type(reference_scope["primary_limitations_enabled"]) is not bool:
        global_reasons.append("Primary ON/OFF mode was not predeclared")
    if not reference_scope["label_version"]:
        global_reasons.append("Target label version was not declared")
    if not parente.get("input_sha256", {}).get("candles_4h"):
        global_reasons.append("Benchmark market-data checksum is absent")
    if parente.get("label_verification", {}).get("status") != "complete":
        global_reasons.append("Benchmark held-out truth labels were not independently audited")
    if (parente.get("model_id") != "parente_mlp_v1"
            or not re.fullmatch(r"[0-9a-f]{64}", str(parente.get("artifact_id", "")))):
        global_reasons.append("Bundled Parente benchmark identity is invalid")
    out["global_reasons"] = global_reasons
    mode = "limited" if reference_scope["primary_limitations_enabled"] else "unlimited"
    benchmark = parente.get("replay", {}).get(f"{mode}_base")
    qualifying = []
    for report in candidates:
        name = report.get("model_id") or "unnamed"
        if name in out["candidates"]:
            raise EvaluationError(f"Duplicate candidate model ID: {name}")
        reasons, uncertainty = [], None
        try:
            same_scope = _scope(report) == reference_scope
        except EvaluationError:
            same_scope = False
        if report.get("status") != "complete":
            reasons.append("Held-out replay or classification is incomplete")
        if validated_artifacts.get(name) != report.get("artifact_id"):
            reasons.append("A fully trained/export-validated immutable candidate bundle was not verified")
        if report.get("label_verification", {}).get("status") != "complete":
            reasons.append("Held-out truth labels were not independently audited")
        if (not re.fullmatch(r"(?:lstm_classifier_v1|gru_classifier_v1|cnn_lstm_classifier_v1|grouped_attention_lstm_v1|probability_ensemble_v1)@[0-9a-f]{12}", name)
                or not re.fullmatch(r"[0-9a-f]{64}", str(report.get("artifact_id", "")))
                or not name.endswith(str(report.get("artifact_id", ""))[:12])
                or report.get("artifact_id") == parente.get("artifact_id")):
            reasons.append("Candidate versioned artifact identity is invalid or duplicates Parente")
        if not same_scope:
            reasons.append("Experiment/test/asset/cost/target scope differs from Parente")
        ref_hashes, candidate_hashes = parente.get("input_sha256", {}), report.get("input_sha256", {})
        if (not candidate_hashes.get("candles_4h") or candidate_hashes.get("candles_4h") != ref_hashes.get("candles_4h")
                or candidate_hashes.get("candles_5m") != ref_hashes.get("candles_5m")
                or not candidate_hashes.get("predictions") or not ref_hashes.get("predictions")):
            reasons.append("Underlying market-data checksums differ or are missing")
        if (report.get("classification", {}).get("combined", {}).get("status") != "complete"
                or report.get("classification", {}).get("combined", {}).get("samples")
                   != report.get("prediction_coverage", {}).get("expected")):
            reasons.append("Candidate held-out classification coverage is incomplete")
        candidate = report.get("replay", {}).get(f"{mode}_base")
        stress = report.get("replay", {}).get(f"{mode}_stress")
        if candidate is None or benchmark is None or stress is None:
            reasons.append("Primary-mode base/stress replay is unavailable")
        else:
            if (candidate.get("status") != "complete" or stress.get("status") != "complete"
                    or benchmark.get("status") != "complete"
                    or candidate.get("mark_cadence_ms") != benchmark.get("mark_cadence_ms")):
                reasons.append("Comparable base/stress replay status or mark cadence differs")
            if candidate["completed_natural_trades"] < 30:
                reasons.append("Fewer than 30 natural held-out exits")
            if candidate["net_return"] <= 0:
                reasons.append("Nonpositive after-cost held-out return")
            if candidate["net_return"] <= benchmark["net_return"]:
                reasons.append("No paired net-return improvement over Parente")
            if candidate["max_sampled_drawdown"] > benchmark["max_sampled_drawdown"]:
                reasons.append("Drawdown exceeds Parente on the same mark cadence")
            if stress["net_return"] <= 0:
                reasons.append("Higher-cost stress return is nonpositive")
            if not reasons and not global_reasons:
                try:
                    uncertainty = _paired_bootstrap(candidate, benchmark)
                except EvaluationError as exc:
                    reasons.append(str(exc))
                else:
                    if uncertainty["confidence_95_log_return_difference"][0] <= 0:
                        reasons.append("Paired time-block uncertainty includes no improvement")
        if global_reasons:
            reasons = global_reasons + reasons
        out["candidates"][name] = {"qualified": not reasons, "reasons": reasons,
                                    "paired_uncertainty": uncertainty,
                                    "base_net_return": candidate.get("net_return") if candidate else None,
                                    "base_max_drawdown": candidate.get("max_sampled_drawdown") if candidate else None,
                                    "natural_trades": candidate.get("completed_natural_trades") if candidate else None}
        if not reasons:
            qualifying.append((candidate["net_return"], name))
    if qualifying:
        qualifying.sort(reverse=True)
        out["recommended_model_id"] = qualifying[0][1]
        out["recommendation"] = "candidate demonstrated under predeclared confirmation rule"
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parente", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument("--candidate-manifest", type=Path, action="append", required=True,
                        help="Repeat once per candidate report to verify its trained immutable bundle")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if len(args.candidate) != len(args.candidate_manifest):
            raise EvaluationError("Each candidate report needs its own trained bundle manifest")
        paths = [args.parente] + args.candidate
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        from adaptive_crypto.neural import NeuralModel
        from adaptive_crypto.neural_models import _manifest
        bundled_parente = NeuralModel()
        if reports[0].get("artifact_id") != bundled_parente.identity:
            raise EvaluationError("Benchmark report is not the bundled Parente model")
        validated = {}
        for candidate_report, manifest_path in zip(reports[1:], args.candidate_manifest):
            model_id = candidate_report.get("model_id")
            manifest, _ = _manifest(manifest_path.parent, model_id, check_weights=True)
            if manifest["artifact_id"] != candidate_report.get("artifact_id"):
                raise EvaluationError(f"Candidate report does not match trained bundle: {model_id}")
            validated[model_id] = manifest["artifact_id"]
        result = compare(reports[0], reports[1:], report_hashes={path.name: _sha256(path) for path in paths},
                         validated_artifacts=validated)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"recommendation": result["recommendation"],
                          "recommended_model_id": result["recommended_model_id"], "output": str(args.output)}))
        return 0
    except (EvaluationError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"comparison failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
