"""Package a validated fixed ensemble without changing live Settings.

The candidate manifests, their hashes, fixed weights and common preprocessing
contract determine the operational artifact identity. The full replay stays in
the experiment directory; a bounded summary and its SHA256 are bundled for the
read-only Settings report endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from adaptive_crypto.neural_models import _manifest as validate_candidate_manifest
from .build_nn_candidate_ensemble import FAMILIES
from .evaluate_nn_candidates import EvaluationError, _sha256

SHARED = ("feature_schema", "feature_names", "feature_groups", "classes",
          "input_shape", "output_shape", "timeframe_ms", "history_bars",
          "raw_feature_context_bars", "feature_sequence_rows", "volume_units",
          "volume_epsilon", "missing_data_policy", "target_horizon_bars",
          "label_version", "inference_runtime", "dataset_provenance_sha256",
          "experiment_sha256", "splits")


def _compact_evaluation(report):
    result = {key: report[key] for key in ("schema", "model_id", "artifact_id", "status",
              "reason", "experiment", "input_sha256", "coverage_4h", "prediction_coverage",
              "excluded_prediction_assets_outside_declared_scope",
              "excluded_prediction_rows_outside_declared_time_scope", "label_verification",
              "classification", "mark_cadence_ms", "quote_proxy", "stop_timing_limitation",
              "baselines") if key in report}
    for name in ("replay", "per_asset_replay"):
        result[name] = {}
        for scope, value in report.get(name, {}).items():
            if name == "replay":
                result[name][scope] = {key: item for key, item in value.items()
                                       if key not in {"trades", "paired_equity_4h"}}
            else:
                result[name][scope] = {
                    mode: {key: item for key, item in run.items()
                           if key not in {"trades", "paired_equity_4h"}}
                    for mode, run in value.items()}
    return result


def package(provenance, evaluation, manifest_inputs, *, comparison=None):
    identity = provenance["identity_payload"]
    if (provenance.get("artifact_id") != hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            or provenance.get("model_id") != f"probability_ensemble_v1@{provenance['artifact_id'][:12]}"
            or identity.get("weights") != [.25] * 4
            or identity.get("classes") != ["BUY", "HOLD", "SELL"]):
        raise EvaluationError("Ensemble provenance identity is not the fixed four-member contract")
    if (evaluation.get("status") != "complete"
            or evaluation.get("model_id") != provenance["model_id"]
            or evaluation.get("artifact_id") != provenance["artifact_id"]
            or evaluation.get("label_verification", {}).get("status") != "complete"
            or evaluation.get("input_sha256", {}).get("predictions")
               != provenance.get("ensemble_predictions_sha256")):
        raise EvaluationError("Ensemble held-out evaluation does not match its frozen predictions")
    input_by_id = {item["model_id"]: item for item in manifest_inputs}
    if len(input_by_id) != 4:
        raise EvaluationError("Ensemble needs four distinct candidate manifest files")
    member_manifests = []
    for family, component in zip(FAMILIES, identity["components"]):
        if not component["model_id"].startswith(family + "@") or component["model_id"] not in input_by_id:
            raise EvaluationError("Ensemble component order or candidate manifest is invalid")
        source = input_by_id[component["model_id"]]
        path = Path(source["manifest_file"])
        if _sha256(path) != component["manifest_sha256"]:
            raise EvaluationError("Ensemble component manifest changed after prediction")
        # Only fully trained/export-validated, immutable single-network bundles
        # can be promoted into a selectable ensemble. Smoke artifacts fail here.
        validate_candidate_manifest(path.parent, component["model_id"], check_weights=True)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("artifact_id") != component["artifact_id"] or manifest.get("id") != family:
            raise EvaluationError("Ensemble component artifact identity changed")
        member_manifests.append(manifest)
    first = member_manifests[0]
    for other in member_manifests[1:]:
        if any(other.get(field) != first.get(field) for field in SHARED):
            raise EvaluationError("Ensemble candidate preprocessing, source or split provenance differs")
    common_assets = sorted(set.intersection(*(set(item["members"]) for item in member_manifests)))
    if common_assets != identity["coverage"]:
        raise EvaluationError("Ensemble common supported-asset coverage changed")
    if identity["latest_model_selection_through_ms"] < first["splits"]["test_end_ms"] - 1:
        raise EvaluationError("Ensemble live eligibility cutoff precedes final evaluation")
    if comparison is not None and (comparison.get("status") != "complete"
                                   or comparison.get("model_id") != provenance["model_id"]
                                   or comparison.get("artifact_id") != provenance["artifact_id"]
                                   or comparison.get("label_verification", {}).get("status") != "complete"):
        raise EvaluationError("Ensemble comparable Parente-scope evaluation is incomplete")
    report = {"schema": 1, "architecture_id": "probability_ensemble_v1",
              "display_name": "Fixed equal-weight probability ensemble",
              "technical_status": "paired_member_predictions_and_report_validated",
              "recommendation": "no demonstrated winner",
              "identity_payload": identity,
              "evaluation": _compact_evaluation(evaluation),
              "full_evaluation_sha256": provenance["full_evaluation_sha256"],
              "comparison_evaluation": _compact_evaluation(comparison) if comparison else None,
              "full_comparison_evaluation_sha256": provenance.get("full_comparison_evaluation_sha256")}
    manifest = {"schema": 1, "id": "probability_ensemble_v1",
                "display_name": report["display_name"],
                **{key: first[key] for key in SHARED},
                "latest_model_selection_through_ms": identity["latest_model_selection_through_ms"],
                "training_dependencies": {"components": [item.get("training_dependencies") for item in member_manifests]},
                "training_hyperparameters": {"members": list(FAMILIES), "probability_weights": [.25] * 4,
                                            "member_selection": "four predeclared candidate architectures; no test-set tuning"},
                "components": identity["components"], "weights": identity["weights"],
                "coverage": identity["coverage"], "identity_payload": identity,
                "artifact_id": provenance["artifact_id"], "report": "report.json"}
    report_bytes = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False,
                               ensure_ascii=False) + "\n").encode("utf-8")
    if len(report_bytes) > 5_000_000:
        raise EvaluationError("Ensemble summary exceeds the runtime report-size limit")
    manifest["report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    return manifest, report_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--comparison-evaluation", type=Path)
    parser.add_argument("--output", type=Path, required=True,
                        help="New empty immutable version directory")
    args = parser.parse_args(argv)
    try:
        provenance = json.loads(args.provenance.read_text(encoding="utf-8"))
        evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
        evaluation_hash = _sha256(args.evaluation)
        if provenance.get("full_evaluation_sha256", evaluation_hash) != evaluation_hash:
            raise EvaluationError("Full ensemble evaluation hash differs from provenance")
        provenance["full_evaluation_sha256"] = evaluation_hash
        comparison = None
        if args.comparison_evaluation:
            comparison = json.loads(args.comparison_evaluation.read_text(encoding="utf-8"))
            comparison_hash = _sha256(args.comparison_evaluation)
            if provenance.get("full_comparison_evaluation_sha256", comparison_hash) != comparison_hash:
                raise EvaluationError("Comparable evaluation hash differs from provenance")
            provenance["full_comparison_evaluation_sha256"] = comparison_hash
        manifest, report_bytes = package(provenance, evaluation,
                                         provenance["member_manifest_inputs"], comparison=comparison)
        if args.output.name != manifest["artifact_id"][:12] or args.output.parent.name != "probability_ensemble_v1":
            raise EvaluationError("Ensemble bundle output must end in probability_ensemble_v1/<artifact-prefix>")
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "report.json").write_bytes(report_bytes)
        (args.output / "manifest.json").write_text(json.dumps(
            manifest, indent=2, sort_keys=True, allow_nan=False, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"model_id": f"probability_ensemble_v1@{manifest['artifact_id'][:12]}",
                          "artifact_id": manifest["artifact_id"], "output": str(args.output)}))
        return 0
    except (EvaluationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ensemble packaging failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
