"""Immutable, data-only candidate model registry and CPU inference adapters.

The bundled Parente model is deliberately loaded by its original strict loader.
Candidate models have their own manifest, feature schema and ONNX validation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from functools import lru_cache
from pathlib import Path

from .core import DataError, H4
from .neural import CLASSES, NeuralModel


CANDIDATE_ROOT = Path(__file__).resolve().parent / "models" / "candidates"
FAMILIES = {
    "lstm_classifier_v1": "LSTM",
    "gru_classifier_v1": "GRU",
    "cnn_lstm_classifier_v1": "CNN-LSTM",
    "grouped_attention_lstm_v1": "Attention-LSTM (OHLCV adaptation)",
    "probability_ensemble_v1": "Probability ensemble",
}
FEATURE_SCHEMA = "kraken-ohlcv-16-v1"
LABEL_VERSION = "parente-source-5-2-v1"
HISTORY_BARS = 319
SEQUENCE_LENGTH = 64
FEATURE_COUNT = 16
MAX_MANIFEST_BYTES = 250_000
MAX_MODEL_BYTES = 20_000_000
MAX_REPORT_BYTES = 5_000_000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hex_sha(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _read_small(path: Path, maximum: int) -> bytes:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > maximum:
        raise DataError(f"Invalid NN candidate artifact: {path.name}")
    return path.read_bytes()


def _local_filename(value: object) -> str:
    if (type(value) is not str or not value or value in {".", ".."}
            or Path(value).name != value or any(ch in value for ch in ("/", "\\", "\x00", ":"))):
        raise DataError("Candidate manifest contains an invalid artifact filename")
    return value


def _vector(values: object, key: str, *, positive: bool = False) -> tuple[float, ...]:
    if (not isinstance(values, list) or len(values) != FEATURE_COUNT
            or any(type(value) not in {float, int} or not math.isfinite(value)
                   or positive and value <= 0 for value in values)):
        raise DataError(f"Invalid NN candidate {key}")
    return tuple(float(value) for value in values)


def _manifest(path: Path, expected_id: str, *, check_weights: bool = True) -> tuple[dict, bytes]:
    if not path.is_dir() or path.is_symlink():
        raise DataError("Candidate model bundle does not exist")
    raw = _read_small(path / "manifest.json", MAX_MANIFEST_BYTES)
    try:
        data = json.loads(raw)
        architecture, separator, prefix = expected_id.partition("@")
        if not separator or architecture not in FAMILIES or len(prefix) != 12:
            raise DataError("Invalid versioned NN candidate ID")
        if not isinstance(data, dict) or data.get("schema") != 1 or data.get("id") != architecture:
            raise DataError("Candidate architecture/manifest mismatch")
        artifact_id = data.get("artifact_id")
        if not _hex_sha(artifact_id) or not artifact_id.startswith(prefix):
            raise DataError("Candidate artifact version mismatch")
        if (data.get("feature_schema") != FEATURE_SCHEMA or data.get("classes") != list(CLASSES)
                or data.get("input_shape") != [1, SEQUENCE_LENGTH, FEATURE_COUNT]
                or data.get("output_shape") != [1, len(CLASSES)]
                or data.get("timeframe_ms") != H4 or data.get("history_bars") != HISTORY_BARS
                or data.get("target_horizon_bars") != 2 or data.get("label_version") != LABEL_VERSION):
            raise DataError("Incompatible candidate feature, target or output schema")
        from .candidate_features import (FEATURE_NAMES, FEATURE_GROUPS, RAW_CONTEXT_BARS,
                                         LOOKBACK_ROWS, VOLUME_EPSILON)
        if data.get("feature_names") != list(FEATURE_NAMES):
            raise DataError("Candidate feature order differs from the runtime")
        if (data.get("feature_groups") != [{"start": a, "end": b} for a, b in FEATURE_GROUPS]
                or data.get("raw_feature_context_bars") != RAW_CONTEXT_BARS
                or data.get("feature_sequence_rows") != LOOKBACK_ROWS
                or data.get("volume_units") != "Kraken base asset units"
                or data.get("volume_epsilon") != VOLUME_EPSILON
                or data.get("missing_data_policy") != "unavailable; no fill or interpolation"):
            raise DataError("Candidate preprocessing or missing-data policy is incompatible")
        runtime = data.get("inference_runtime")
        if (not isinstance(runtime, dict) or runtime.get("name") != "onnxruntime"
                or runtime.get("execution_provider") != "CPUExecutionProvider"
                or runtime.get("opset") != 17 or type(runtime.get("version_validated")) is not str
                or not runtime["version_validated"]):
            raise DataError("Candidate CPU runtime/export provenance is missing")
        splits = data.get("splits")
        if (not isinstance(splits, dict)
                or any(type(splits.get(key)) is not int for key in
                       ("aligned_history_start_ms", "validation_start_ms", "test_start_ms", "test_end_ms"))
                or not (0 <= splits["aligned_history_start_ms"] < splits["validation_start_ms"]
                        < splits["test_start_ms"] < splits["test_end_ms"])):
            raise DataError("Candidate chronological split provenance is missing")
        if (not _hex_sha(data.get("dataset_provenance_sha256"))
                or not _hex_sha(data.get("experiment_sha256"))):
            raise DataError("Candidate source data or experiment hash is missing")
        cutoff = data.get("latest_model_selection_through_ms")
        if type(cutoff) is not int or cutoff < splits["test_end_ms"] - 1:
            raise DataError("Candidate live cutoff must follow its final evaluation period")
        if architecture == "probability_ensemble_v1":
            components = data.get("components")
            weights = data.get("weights")
            coverage = data.get("coverage")
            family_order = tuple(family for family in FAMILIES if family != "probability_ensemble_v1")
            if (not isinstance(components, list) or len(components) != len(family_order)
                    or not isinstance(weights, list) or len(weights) != len(components)
                    or any(type(weight) not in {int, float} or weight != .25 for weight in weights)
                    or not isinstance(coverage, list) or not coverage
                    or coverage != sorted(set(coverage))
                    or any(type(asset) is not str or not asset.isalnum() or asset != asset.upper()
                           for asset in coverage)):
                raise DataError("Invalid fixed equal-weight ensemble contract")
            supported = None
            for family, component in zip(family_order, components):
                if not isinstance(component, dict) or set(component) != {"model_id", "artifact_id", "manifest_sha256"}:
                    raise DataError("Invalid ensemble component")
                component_id = component["model_id"]
                actual_family, _, _ = component_id.partition("@") if isinstance(component_id, str) else (None, None, None)
                if (actual_family != family or not _hex_sha(component["artifact_id"])
                        or not _hex_sha(component["manifest_sha256"])):
                    raise DataError("Ensemble components must be one trained artifact per candidate architecture")
                component_path = candidate_bundle_path(component_id)
                component_manifest, component_raw = _manifest(component_path, component_id, check_weights=check_weights)
                if _sha(component_raw) != component["manifest_sha256"]:
                    raise DataError("Ensemble component manifest hash changed")
                if component_manifest["artifact_id"] != component["artifact_id"]:
                    raise DataError("Ensemble component artifact ID changed")
                if component_manifest["latest_model_selection_through_ms"] > cutoff:
                    raise DataError("Ensemble cutoff precedes a component cutoff")
                asset_set = set(component_manifest["members"])
                supported = asset_set if supported is None else supported & asset_set
            if set(coverage) != supported:
                raise DataError("Ensemble coverage does not match its trained components")
            identity_payload = {
                "schema": "fixed-probability-ensemble-v1",
                "architecture_id": architecture,
                "feature_schema": FEATURE_SCHEMA,
                "label_version": LABEL_VERSION,
                "timeframe_ms": H4,
                "target_horizon_bars": 2,
                "history_bars": HISTORY_BARS,
                "classes": list(CLASSES),
                "weights": [.25] * len(family_order),
                "components": components,
                "coverage": coverage,
                "latest_model_selection_through_ms": cutoff,
            }
            if data.get("identity_payload") != identity_payload:
                raise DataError("Ensemble identity payload does not match its verified components and policy")
        else:
            members = data.get("members")
            if not isinstance(members, dict) or not members:
                raise DataError("Candidate has no trained assets")
            for asset, member in members.items():
                if type(asset) is not str or not asset.isalnum() or asset != asset.upper() or not isinstance(member, dict):
                    raise DataError("Invalid candidate asset")
                filename = _local_filename(member.get("onnx"))
                if not filename.endswith(".onnx") or not _hex_sha(member.get("sha256")):
                    raise DataError("Invalid candidate model hash")
                _vector(member.get("mean"), "feature mean")
                _vector(member.get("scale"), "feature scale", positive=True)
                trained = member.get("trained_through_ms")
                if type(trained) is not int or trained < 0 or trained > cutoff:
                    raise DataError("Invalid candidate asset cutoff")
                if not _hex_sha(member.get("dataset_sha256")):
                    raise DataError("Candidate data provenance is missing")
                if check_weights and _sha(_read_small(path / filename, MAX_MODEL_BYTES)) != member["sha256"]:
                    raise DataError(f"Candidate {asset} weights do not match the manifest")
        report_name = _local_filename(data.get("report"))
        if not report_name.endswith(".json") or not _hex_sha(data.get("report_sha256")):
            raise DataError("Candidate validation report is missing")
        if check_weights:
            report_bytes = _read_small(path / report_name, MAX_REPORT_BYTES)
            if _sha(report_bytes) != data["report_sha256"]:
                raise DataError("Candidate validation report hash mismatch")
            report = json.loads(report_bytes)
            if not isinstance(report, dict):
                raise DataError("Candidate validation report must be a JSON object")
            if architecture == "probability_ensemble_v1":
                if (report.get("schema") != 1
                        or report.get("architecture_id") != architecture
                        or report.get("technical_status") != "paired_member_predictions_and_report_validated"
                        or report.get("identity_payload") != data["identity_payload"]
                        or not _hex_sha(report.get("full_evaluation_sha256"))
                        or not _hex_sha(report.get("full_comparison_evaluation_sha256"))
                        or report.get("recommendation") not in {"no demonstrated winner", "recommended"}):
                    raise DataError("Ensemble report lacks frozen member and evaluation evidence")
                for evaluation_key in ("evaluation", "comparison_evaluation"):
                    evidence = report.get(evaluation_key)
                    if (not isinstance(evidence, dict)
                            or evidence.get("status") != "complete"
                            or evidence.get("model_id") != expected_id
                            or evidence.get("artifact_id") != artifact_id
                            or not isinstance(evidence.get("replay"), dict)
                            or not isinstance(evidence.get("classification"), dict)):
                        raise DataError(f"Ensemble {evaluation_key} is incomplete or belongs to another artifact")
            else:
                if (report.get("schema") != 1 or report.get("architecture_id") != architecture
                        or report.get("technical_status") != "trained_export_parity_passed"
                        or report.get("experiment_sha256") != data["experiment_sha256"]
                        or report.get("promotion_evidence_through_ms", -1) > cutoff
                        or not isinstance(report.get("evaluation"), dict)
                        or report["evaluation"].get("status") != "complete"
                        or not isinstance(report.get("per_asset"), dict)
                        or set(report["per_asset"]) != set(data["members"])):
                    raise DataError("Candidate report lacks complete trained/export/evaluation evidence")
                for asset, evidence in report["per_asset"].items():
                    if (not isinstance(evidence, dict)
                            or not isinstance(evidence.get("seed_runs"), list)
                            or len(evidence["seed_runs"]) < 3
                            or not isinstance(evidence.get("sample_counts"), dict)
                            or any(type(evidence["sample_counts"].get(split)) is not int
                                   or evidence["sample_counts"][split] < 1
                                   for split in ("train", "validation", "test"))
                            or not isinstance(evidence.get("export"), dict)
                            or type(evidence["export"].get("max_abs_logits_difference")) not in {int, float}
                            or evidence["export"]["max_abs_logits_difference"] > 1e-4
                            or evidence["export"].get("onnx_sha256") != data["members"][asset]["sha256"]):
                        raise DataError(f"Candidate {asset} has incomplete fit/export evidence")
        # Reports are produced from predictions after artifact identity is
        # assigned. Their hash is verified above, but is not part of the
        # operational model ID, avoiding a report/identity cycle.
        operational = data["identity_payload"] if architecture == "probability_ensemble_v1" else {
            key: value for key, value in data.items()
            if key not in {"artifact_id", "report", "report_sha256"}}
        canonical = json.dumps(operational,
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if _sha(canonical) != artifact_id:
            raise DataError("Candidate artifact ID does not cover its manifest and member hashes")
        return data, raw
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise DataError(f"Invalid NN candidate manifest: {exc}") from exc


def candidate_bundle_path(model_id: str, settings_path: Path | None = None, custom_path: str = "") -> Path:
    architecture, separator, prefix = model_id.partition("@")
    if not separator or architecture not in FAMILIES or len(prefix) != 12 or any(c not in "0123456789abcdef" for c in prefix):
        raise DataError("Invalid versioned NN candidate ID")
    if custom_path:
        path = Path(custom_path)
        if not path.is_absolute() and settings_path is not None:
            path = Path(settings_path).resolve().parent / path
        elif not path.is_absolute():
            raise DataError("Relative model bundle needs a settings file")
        return path.resolve()
    return CANDIDATE_ROOT / architecture / prefix


class CandidateModel:
    """One applied immutable candidate bundle, sharing pyPTA's signal contract."""

    def __init__(self, model_id: str, path: Path):
        self.path = Path(path)
        manifest, raw = _manifest(self.path, model_id)
        if manifest["id"] == "probability_ensemble_v1":
            raise DataError("Ensemble bundle requires its four component adapters")
        if importlib.util.find_spec("onnxruntime") is None:
            raise DataError("Selected NN candidate requires the pinned onnxruntime CPU dependency")
        import onnxruntime as ort
        if getattr(ort, "__version__", None) != manifest["inference_runtime"]["version_validated"]:
            raise DataError("Selected NN candidate requires its validated ONNX Runtime CPU version")

        self.identity = _sha(raw)
        self.model_id = model_id
        self.architecture_id = manifest["id"]
        self.artifact_id = manifest["artifact_id"]
        self.trained_through_ms = manifest["latest_model_selection_through_ms"]
        self.required_history_bars = HISTORY_BARS
        self.metadata = {
            "display_name": manifest.get("display_name", FAMILIES[self.architecture_id]),
            "architecture_id": self.architecture_id,
            "artifact_id": self.artifact_id,
            "feature_count": FEATURE_COUNT,
            "target_horizon": 2,
            "trained_through_ms": max(member["trained_through_ms"] for member in manifest["members"].values()),
            "live_eligibility_after_ms": self.trained_through_ms,
            "coverage": sorted(manifest["members"]),
            "report_url": f"/api/models/{model_id}/report",
        }
        self.members = manifest["members"]
        self.sessions = {}
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = 1
        session_options.inter_op_num_threads = 1
        for asset, member in self.members.items():
            # Passing verified bytes prevents ONNX external-file references from
            # escaping the vetted bundle and keeps weights fixed until Apply.
            model_bytes = _read_small(self.path / member["onnx"], MAX_MODEL_BYTES)
            if _sha(model_bytes) != member["sha256"]:
                raise DataError(f"Candidate {asset} weights changed while loading")
            session = ort.InferenceSession(model_bytes, sess_options=session_options,
                                           providers=["CPUExecutionProvider"])
            inputs, outputs = session.get_inputs(), session.get_outputs()
            if (len(inputs) != 1 or inputs[0].name != "features" or inputs[0].type != "tensor(float)"
                    or list(inputs[0].shape) != [1, SEQUENCE_LENGTH, FEATURE_COUNT]
                    or len(outputs) != 1 or outputs[0].name != "logits" or outputs[0].type != "tensor(float)"
                    or list(outputs[0].shape) != [1, len(CLASSES)]):
                raise DataError("Candidate ONNX tensor contract mismatch")
            self.sessions[asset] = session

    def trained_through_ms_for(self, asset: str) -> int:
        member = self.members.get(asset)
        if member is None:
            raise DataError(f"Selected NN model has no trained artifact for {asset}")
        return max(self.trained_through_ms, member["trained_through_ms"])

    def predict(self, candles, asset: str):
        import numpy as np
        from .candidate_features import feature_sequence

        member = self.members.get(asset)
        if member is None:
            raise DataError(f"Selected NN model has no trained artifact for {asset}")
        features = feature_sequence(candles)
        mean = np.asarray(member["mean"], dtype=np.float32)
        scale = np.asarray(member["scale"], dtype=np.float32)
        x = ((np.asarray(features, dtype=np.float32) - mean) / scale)[None, :, :]
        if x.shape != (1, SEQUENCE_LENGTH, FEATURE_COUNT) or not np.isfinite(x).all():
            raise DataError("Invalid candidate NN input features")
        logits = np.asarray(self.sessions[asset].run(["logits"], {"features": x})[0], dtype=np.float64)
        if logits.shape != (1, len(CLASSES)) or not np.isfinite(logits).all():
            raise DataError("Invalid candidate NN logits")
        centered = logits[0] - logits[0].max()
        weights = np.exp(centered)
        probabilities = weights / weights.sum()
        if not np.isfinite(probabilities).all() or not np.isclose(probabilities.sum(), 1.0, atol=1e-6):
            raise DataError("Invalid candidate NN probabilities")
        final = np.asarray(features)[-1]
        return {
            "label": CLASSES[int(probabilities.argmax())],
            "probabilities": dict(zip(CLASSES, map(float, probabilities))),
            "features": dict(zip(manifest_feature_names(), map(float, final))),
            "signal_end": candles[-1].end,
            "model_id": self.identity,
        }


class EnsembleModel:
    """Frozen equal-probability average of four validated candidate families."""

    def __init__(self, model_id: str, path: Path):
        self.path = Path(path)
        manifest, raw = _manifest(self.path, model_id)
        if manifest["id"] != "probability_ensemble_v1":
            raise DataError("Not an ensemble artifact")
        self.components = [CandidateModel(component["model_id"],
                                          candidate_bundle_path(component["model_id"]))
                           for component in manifest["components"]]
        self.identity = _sha(raw)
        self.model_id = model_id
        self.architecture_id = manifest["id"]
        self.artifact_id = manifest["artifact_id"]
        self.trained_through_ms = manifest["latest_model_selection_through_ms"]
        self.required_history_bars = HISTORY_BARS
        self.coverage = frozenset(manifest["coverage"])
        self.metadata = {
            "display_name": manifest.get("display_name", FAMILIES[self.architecture_id]),
            "architecture_id": self.architecture_id,
            "artifact_id": self.artifact_id,
            "feature_count": FEATURE_COUNT,
            "target_horizon": 2,
            "trained_through_ms": max(component.metadata["trained_through_ms"] for component in self.components),
            "live_eligibility_after_ms": self.trained_through_ms,
            "coverage": sorted(self.coverage),
            "members": [component.model_id for component in self.components],
            "report_url": f"/api/models/{model_id}/report",
        }

    def trained_through_ms_for(self, asset: str) -> int:
        if asset not in self.coverage:
            raise DataError(f"Selected NN model has no trained artifact for {asset}")
        return max(self.trained_through_ms,
                   *(component.trained_through_ms_for(asset) for component in self.components))

    def predict(self, candles, asset: str):
        if asset not in self.coverage:
            raise DataError(f"Selected NN model has no trained artifact for {asset}")
        signals = [component.predict(candles, asset) for component in self.components]
        probabilities = {name: sum(signal["probabilities"][name] for signal in signals) / len(signals)
                         for name in CLASSES}
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in probabilities.values()) \
                or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-6):
            raise DataError("Invalid ensemble probabilities")
        if any(signal["signal_end"] != signals[0]["signal_end"] for signal in signals[1:]):
            raise DataError("Ensemble members disagreed on the decision candle")
        return {"label": max(CLASSES, key=lambda name: probabilities[name]),
                "probabilities": probabilities, "features": signals[0]["features"],
                "signal_end": signals[0]["signal_end"], "model_id": self.identity}


def manifest_feature_names() -> tuple[str, ...]:
    from .candidate_features import FEATURE_NAMES
    return FEATURE_NAMES


def load_selected_model(rules, settings_path: Path | None = None):
    if rules.nn_model_id == "parente_mlp_v1":
        path = Path(rules.nn_model_path) if rules.nn_model_path else None
        if path is not None and not path.is_absolute() and settings_path is not None:
            path = Path(settings_path).resolve().parent / path
        return NeuralModel(path)
    bundle = candidate_bundle_path(rules.nn_model_id, settings_path, rules.nn_model_bundle_path)
    model_class = EnsembleModel if rules.nn_model_id.startswith("probability_ensemble_v1@") else CandidateModel
    return model_class(rules.nn_model_id, bundle)


@lru_cache(maxsize=64)
def _runtime_validated(model_id: str, bundle: str, manifest_hash: str) -> bool:
    # Catalog status reflects executable ONNX sessions, not just filenames.
    # The caller hashes all member files first, so a replaced artifact cannot
    # keep a stale available status under this cached manifest identity.
    model_class = EnsembleModel if model_id.startswith("probability_ensemble_v1@") else CandidateModel
    model_class(model_id, Path(bundle))
    return True


def _training_through_ms(manifest: dict) -> int:
    if manifest["id"] != "probability_ensemble_v1":
        return max(member["trained_through_ms"] for member in manifest["members"].values())
    return max(
        member["trained_through_ms"]
        for component in manifest["components"]
        for member in _manifest(candidate_bundle_path(component["model_id"]),
                                component["model_id"])[0]["members"].values()
    )


def model_catalog(settings_path: Path | None = None, selections=()) -> list[dict]:
    items = [{"id": "parente_mlp_v1", "label": "Parente 5/2 (current)",
              "available": True, "status": "Trained and validated", "reason": "Bundled model",
              "coverage": ["BTC", "ETH", "SOL"],
              "trained_through_ms": 1670169599999, "live_eligibility_after_ms": 1670169599999,
              "target_horizon": 2,
              "artifact_id": None, "report_url": None}]
    runtime_ready = importlib.util.find_spec("onnxruntime") is not None
    for family, label in FAMILIES.items():
        family_path = CANDIDATE_ROOT / family
        found = False
        if family_path.is_dir() and not family_path.is_symlink():
            for path in sorted(family_path.iterdir()):
                if not path.is_dir() or path.is_symlink() or len(path.name) != 12:
                    continue
                model_id = family + "@" + path.name
                try:
                    manifest, raw = _manifest(path, model_id)
                    available = runtime_ready
                    reason = "Trained and validated" if available else "ONNX Runtime CPU is not installed"
                    if available:
                        try:
                            _runtime_validated(model_id, str(path.resolve()), _sha(raw))
                        except Exception as exc:
                            available = False
                            reason = f"Candidate runtime validation failed: {type(exc).__name__}: {exc}"
                    items.append({"id": model_id, "label": label + " · " + path.name,
                                  "available": available,
                                  "status": "Trained and validated" if available else "Unavailable",
                                  "reason": reason,
                                  "coverage": (manifest["coverage"] if family == "probability_ensemble_v1"
                                               else sorted(manifest["members"])),
                                  "trained_through_ms": _training_through_ms(manifest),
                                  "live_eligibility_after_ms": manifest["latest_model_selection_through_ms"],
                                  "target_horizon": manifest["target_horizon_bars"],
                                  "artifact_id": manifest["artifact_id"],
                                  "report_url": f"/api/models/{model_id}/report"})
                    found = True
                except (DataError, OSError) as exc:
                    items.append({"id": model_id, "label": label + " · " + path.name,
                                  "available": False, "status": "Failed", "reason": str(exc), "coverage": [],
                                  "trained_through_ms": None, "live_eligibility_after_ms": None,
                                  "target_horizon": None,
                                  "artifact_id": None, "report_url": None})
                    found = True
        if not found:
            items.append({"id": family, "label": label, "available": False, "status": "Not trained",
                          "reason": "No validated trained artifact is installed", "coverage": [],
                          "trained_through_ms": None, "live_eligibility_after_ms": None,
                          "target_horizon": None,
                          "artifact_id": None, "report_url": None})
    for rules in selections:
        if rules.nn_model_id == "parente_mlp_v1" or not rules.nn_model_bundle_path:
            continue
        model_id = rules.nn_model_id
        family = model_id.partition("@")[0]
        try:
            path = candidate_bundle_path(model_id, settings_path, rules.nn_model_bundle_path)
            manifest, raw = _manifest(path, model_id)
            available = runtime_ready
            reason = "Trained and validated external bundle" if available else "ONNX Runtime CPU is not installed"
            if available:
                try:
                    _runtime_validated(model_id, str(path), _sha(raw))
                except Exception as exc:
                    available = False
                    reason = f"Candidate runtime validation failed: {type(exc).__name__}: {exc}"
            entry = {"id": model_id, "label": FAMILIES[family] + " · " + model_id.partition("@")[2],
                     "available": available,
                     "status": "Trained and validated" if available else "Unavailable",
                     "reason": reason,
                     "coverage": (manifest["coverage"] if family == "probability_ensemble_v1"
                                  else sorted(manifest["members"])),
                     "trained_through_ms": _training_through_ms(manifest),
                     "live_eligibility_after_ms": manifest["latest_model_selection_through_ms"],
                     "target_horizon": 2, "artifact_id": manifest["artifact_id"],
                     "report_url": f"/api/models/{model_id}/report"}
        except (DataError, OSError) as exc:
            entry = {"id": model_id, "label": model_id, "available": False, "status": "Failed",
                     "reason": str(exc), "coverage": [], "trained_through_ms": None,
                     "live_eligibility_after_ms": None,
                     "target_horizon": None, "artifact_id": None, "report_url": None}
        items = [item for item in items if item["id"] != model_id]
        items.append(entry)
    return items


def public_report(model_id: str, path: Path | None = None) -> dict:
    path = Path(path) if path is not None else candidate_bundle_path(model_id)
    manifest, _ = _manifest(path, model_id)
    raw = _read_small(path / manifest["report"], MAX_REPORT_BYTES)
    return json.loads(raw)
