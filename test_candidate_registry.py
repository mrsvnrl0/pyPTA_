"""Candidate registry and Settings transitions use disposable bundles and stores."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from adaptive_crypto.candidate_features import FEATURE_NAMES, FEATURE_GROUPS
from adaptive_crypto.core import DataError, H4, Rules, load_settings
from adaptive_crypto.ledger import atomic_json
from adaptive_crypto.neural import CLASSES
from adaptive_crypto.neural_ledger import open_trade
from adaptive_crypto import neural_models
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.settings_editor import read_editor
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.web import create_app


MODEL_BYTES = b"synthetic ONNX fixture; sessions are mocked"
REPORT_BYTES = json.dumps({
    "schema": 1, "architecture_id": "lstm_classifier_v1",
    "technical_status": "trained_export_parity_passed",
    "experiment_sha256": "d" * 64,
    "promotion_evidence_through_ms": 1_780_000_000_000 - 1,
    "evaluation": {"status": "complete"},
    "per_asset": {"BTC": {"seed_runs": [{"seed": seed} for seed in (11, 23, 37)],
                          "sample_counts": {"train": 300, "validation": 100, "test": 100},
                          "export": {"max_abs_logits_difference": 0.0,
                                     "onnx_sha256": hashlib.sha256(MODEL_BYTES).hexdigest()}}},
}).encode("utf-8")


def fixture_payload(assets=("BTC",)):
    members = {}
    for asset in assets:
        filename = f"{asset.lower()}.onnx"
        members[asset] = {
            "onnx": filename, "sha256": hashlib.sha256(MODEL_BYTES).hexdigest(),
            "mean": [0.0] * neural_models.FEATURE_COUNT,
            "scale": [1.0] * neural_models.FEATURE_COUNT,
            "trained_through_ms": 1_700_000_000_000,
            "dataset_sha256": "b" * 64,
        }
    return {
        "schema": 1, "id": "lstm_classifier_v1",
        "feature_schema": neural_models.FEATURE_SCHEMA,
        "feature_names": list(FEATURE_NAMES), "classes": list(CLASSES),
        "feature_groups": [{"start": a, "end": b} for a, b in FEATURE_GROUPS],
        "raw_feature_context_bars": 256, "feature_sequence_rows": 64,
        "volume_units": "Kraken base asset units", "volume_epsilon": 1e-12,
        "missing_data_policy": "unavailable; no fill or interpolation",
        "inference_runtime": {"name": "onnxruntime", "version_validated": "1.30.0",
                              "execution_provider": "CPUExecutionProvider", "opset": 17},
        "splits": {"aligned_history_start_ms": 1_600_000_000_000,
                   "validation_start_ms": 1_720_000_000_000,
                   "test_start_ms": 1_750_000_000_000,
                   "test_end_ms": 1_780_000_000_000},
        "dataset_provenance_sha256": "c" * 64,
        "experiment_sha256": "d" * 64,
        "input_shape": [1, neural_models.SEQUENCE_LENGTH, neural_models.FEATURE_COUNT],
        "output_shape": [1, len(CLASSES)], "timeframe_ms": H4,
        "history_bars": neural_models.HISTORY_BARS, "target_horizon_bars": 2,
        "label_version": neural_models.LABEL_VERSION,
        "latest_model_selection_through_ms": 1_800_000_000_000,
        "members": members, "report": "validation.json",
        "report_sha256": hashlib.sha256(REPORT_BYTES).hexdigest(),
    }


def canonical_id(payload):
    operational = {key: value for key, value in payload.items()
                   if key not in {"artifact_id", "report", "report_sha256"}}
    return hashlib.sha256(json.dumps(operational, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


ARTIFACT_ID = canonical_id(fixture_payload())
MODEL_ID = "lstm_classifier_v1@" + ARTIFACT_ID[:12]


def fixture_bundle(root: Path, *, assets=("BTC",)) -> Path:
    manifest = fixture_payload(assets)
    artifact_id = canonical_id(manifest)
    path = root / "lstm_classifier_v1" / artifact_id[:12]
    path.mkdir(parents=True, exist_ok=True)
    (path / "validation.json").write_bytes(REPORT_BYTES)
    for asset in assets:
        (path / f"{asset.lower()}.onnx").write_bytes(MODEL_BYTES)
    manifest["artifact_id"] = artifact_id
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def change_manifest(path: Path, change):
    target = path / "manifest.json"
    manifest = json.loads(target.read_text(encoding="utf-8"))
    change(manifest)
    target.write_text(json.dumps(manifest), encoding="utf-8")


class FakeONNXSession:
    def __init__(self, model_bytes, *, providers, sess_options):
        if model_bytes != MODEL_BYTES or providers != ["CPUExecutionProvider"]:
            raise RuntimeError("Invalid ONNX fixture")

    def get_inputs(self):
        return [types.SimpleNamespace(name="features", type="tensor(float)",
                                      shape=[1, neural_models.SEQUENCE_LENGTH, neural_models.FEATURE_COUNT])]

    def get_outputs(self):
        return [types.SimpleNamespace(name="logits", type="tensor(float)", shape=[1, len(CLASSES)])]


class CandidateRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = fixture_bundle(self.root)
        neural_models._runtime_validated.cache_clear()
        self.addCleanup(neural_models._runtime_validated.cache_clear)
        candidate_root = patch.object(neural_models, "CANDIDATE_ROOT", self.root)
        candidate_root.start()
        self.addCleanup(candidate_root.stop)

    def mock_onnx(self, session=FakeONNXSession):
        module = types.ModuleType("onnxruntime")
        module.__version__ = "1.30.0"
        module.InferenceSession = session
        module.SessionOptions = lambda: types.SimpleNamespace(intra_op_num_threads=0, inter_op_num_threads=0)
        original_find_spec = importlib.util.find_spec
        dependency = patch.object(neural_models.importlib.util, "find_spec",
                                  side_effect=lambda name: object() if name == "onnxruntime" else original_find_spec(name))
        modules = patch.dict(sys.modules, {"onnxruntime": module})
        dependency.start(); modules.start()
        self.addCleanup(modules.stop)
        self.addCleanup(dependency.stop)

    def test_valid_manifest_and_session_load_only_the_declared_asset(self):
        self.mock_onnx()
        model = neural_models.load_selected_model(Rules(nn_model_id=MODEL_ID))
        self.assertEqual(model.architecture_id, "lstm_classifier_v1")
        self.assertEqual(model.metadata["coverage"], ["BTC"])
        self.assertEqual(model.sessions.keys(), {"BTC"})
        with self.assertRaisesRegex(DataError, "no trained artifact for ETH"):
            model.trained_through_ms_for("ETH")
        entry = next(item for item in neural_models.model_catalog() if item["id"] == MODEL_ID)
        self.assertTrue(entry["available"])
        self.assertEqual(entry["artifact_id"], ARTIFACT_ID)

    def test_manifest_filenames_and_model_id_cannot_escape_bundle(self):
        for key, value in (("onnx", "../outside.onnx"), ("onnx", "C:\\outside.onnx"),
                           ("onnx", "..\\outside.onnx")):
            with self.subTest(key=key, value=value):
                candidate = fixture_bundle(self.root)
                change_manifest(candidate, lambda data: data["members"]["BTC"].__setitem__(key, value))
                with self.assertRaisesRegex(DataError, "invalid artifact filename"):
                    neural_models._manifest(candidate, MODEL_ID)
        candidate = fixture_bundle(self.root)
        change_manifest(candidate, lambda data: data.__setitem__("report", "../report.json"))
        with self.assertRaisesRegex(DataError, "invalid artifact filename"):
            neural_models._manifest(candidate, MODEL_ID)
        with self.assertRaisesRegex(DataError, "Invalid versioned NN candidate ID"):
            neural_models.candidate_bundle_path("lstm_classifier_v1@../../escape")

    def test_corrupt_model_or_report_hash_blocks_catalogue_availability(self):
        self.bundle.joinpath("btc.onnx").write_bytes(b"tampered model")
        with self.assertRaisesRegex(DataError, "weights do not match"):
            neural_models._manifest(self.bundle, MODEL_ID)
        item = next(item for item in neural_models.model_catalog() if item["id"] == MODEL_ID)
        self.assertFalse(item["available"])
        self.assertIn("weights", item["reason"])
        fixture_bundle(self.root)
        self.bundle.joinpath("validation.json").write_bytes(b"tampered report")
        with self.assertRaisesRegex(DataError, "report hash mismatch"):
            neural_models._manifest(self.bundle, MODEL_ID)

    def test_feature_class_and_label_contracts_reject_incompatible_manifest(self):
        changes = (
            lambda data: data.__setitem__("feature_names", list(reversed(FEATURE_NAMES))),
            lambda data: data.__setitem__("classes", list(reversed(CLASSES))),
            lambda data: data.__setitem__("label_version", "different-target"),
            lambda data: data["members"]["BTC"].__setitem__("scale", [0.0] * neural_models.FEATURE_COUNT),
        )
        for change in changes:
            with self.subTest(change=change):
                fixture_bundle(self.root)
                change_manifest(self.bundle, change)
                with self.assertRaises(DataError):
                    neural_models._manifest(self.bundle, MODEL_ID)

    def test_missing_onnx_dependency_or_bad_session_is_not_selectable(self):
        with patch.object(neural_models.importlib.util, "find_spec", return_value=None):
            with self.assertRaisesRegex(DataError, "onnxruntime CPU dependency"):
                neural_models.CandidateModel(MODEL_ID, self.bundle)
            item = next(item for item in neural_models.model_catalog() if item["id"] == MODEL_ID)
            self.assertFalse(item["available"])
            self.assertIn("not installed", item["reason"])

        def bad_session(*_args, **_kwargs):
            raise RuntimeError("Malformed ONNX bytes")
        self.mock_onnx(bad_session)
        item = next(item for item in neural_models.model_catalog() if item["id"] == MODEL_ID)
        self.assertFalse(item["available"])
        self.assertIn("Malformed ONNX bytes", item["reason"])


class CandidateSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = fixture_bundle(self.root / "candidates")
        neural_models._runtime_validated.cache_clear()
        self.addCleanup(neural_models._runtime_validated.cache_clear)
        candidate_root = patch.object(neural_models, "CANDIDATE_ROOT", self.root / "candidates")
        candidate_root.start(); self.addCleanup(candidate_root.stop)
        module = types.ModuleType("onnxruntime")
        module.__version__ = "1.30.0"
        module.InferenceSession = FakeONNXSession
        module.SessionOptions = lambda: types.SimpleNamespace(intra_op_num_threads=0, inter_op_num_threads=0)
        original_find_spec = importlib.util.find_spec
        dependency = patch.object(neural_models.importlib.util, "find_spec",
                                  side_effect=lambda name: object() if name == "onnxruntime" else original_find_spec(name))
        modules = patch.dict(sys.modules, {"onnxruntime": module})
        dependency.start(); modules.start()
        self.addCleanup(modules.stop); self.addCleanup(dependency.stop)

        self.path = self.root / "settings.json"
        self.document = {
            "assets": [{"name": "BTC", "symbol": "BTC/USD", "enabled": True, "price_decimals": 2},
                       {"name": "ETH", "symbol": "ETH/USD", "enabled": True, "price_decimals": 2}],
            "strategy": asdict(Rules(strategy_model="neural_network", nn_limitations=False)),
            "refresh_seconds": 15,
        }
        atomic_json(self.path, self.document)
        assets, rules, refresh = load_settings(self.path)
        owner = open_stores(self.root / "state.json", assets, rules, "json")
        store, positions = owner.__enter__()
        self.addCleanup(owner.__exit__, None, None, None)
        self.runtime = DashboardRuntime(assets, rules, store, refresh, position_store=positions, settings_path=self.path)
        self.runtime.state_base = self.root / "state.json"
        self.restart = Mock()
        self.client = create_app(self.runtime, restart_callback=self.restart).test_client()
        self.client.get("/settings")
        with self.client.session_transaction() as session:
            self.token = session["csrf_token"]

    def post(self, route, body=None):
        return self.client.post(route, json=body or {}, headers={"X-CSRF-Token": self.token})

    def save_candidate(self):
        document, revision = read_editor(self.runtime)
        document["strategy"]["nn_model_id"] = MODEL_ID
        result = self.post("/api/settings/save", {"settings": document, "revision": revision})
        self.assertEqual(result.status_code, 200, result.json)
        return result

    def test_save_does_not_apply_and_failed_apply_keeps_engine_ledger_and_checkbox(self):
        applied_engine = self.runtime.engine
        before = copy.deepcopy(self.runtime.store.snapshot())
        self.save_candidate()
        self.assertIs(self.runtime.engine, applied_engine)
        self.assertEqual(self.runtime.rules.nn_model_id, "parente_mlp_v1")
        self.assertFalse(self.runtime.rules.nn_limitations)
        self.assertEqual(self.runtime.store.snapshot(), before)
        self.assertEqual(read_editor(self.runtime)[0]["strategy"]["nn_model_id"], MODEL_ID)
        self.bundle.joinpath("btc.onnx").write_bytes(b"changed after save")
        failure = self.post("/api/settings/apply")
        self.assertEqual(failure.status_code, 400, failure.json)
        self.assertIs(self.runtime.engine, applied_engine)
        self.assertEqual(self.runtime.rules.nn_model_id, "parente_mlp_v1")
        self.assertFalse(self.runtime.rules.nn_limitations)
        self.assertEqual(self.runtime.store.snapshot(), before)

    def test_active_asset_requires_candidate_coverage_before_apply_or_restart(self):
        from adaptive_crypto.neural_ledger import validate
        signal_end = (1_900_000_000_000 // H4) * H4 - 1
        now = signal_end + 1001
        signal = {"label": "BUY", "probabilities": {"BUY": .8, "HOLD": .1, "SELL": .1},
                  "features": {}, "signal_end": signal_end, "model_id": "a" * 64}
        quote = {"ask": 100.01, "bid": 100.0, "asof_ms": now}
        self.runtime.store.transaction(lambda doc: open_trade(doc, "ETH", signal, quote, self.runtime.rules, now))
        validate(self.runtime.store.snapshot())
        # This candidate contains BTC only, so the open ETH lot needs another
        # classifier capable of its future SELL exits.
        before = copy.deepcopy(self.runtime.store.snapshot())
        applied_engine = self.runtime.engine
        self.save_candidate()
        failure = self.post("/api/settings/apply")
        self.assertEqual(failure.status_code, 400, failure.json)
        self.assertIn("ETH", failure.json["error"])
        self.assertIs(self.runtime.engine, applied_engine)
        self.assertEqual(self.runtime.store.snapshot(), before)
        restart = self.post("/api/dashboard/restart")
        self.assertEqual(restart.status_code, 400, restart.json)
        self.restart.assert_not_called()
        self.assertFalse(self.runtime.stop.is_set())

    def test_disabling_an_asset_with_an_active_trade_is_rejected_before_apply_or_restart(self):
        signal_end = (1_900_000_000_000 // H4) * H4 - 1
        now = signal_end + 1001
        signal = {"label": "BUY", "probabilities": {"BUY": .8, "HOLD": .1, "SELL": .1},
                  "features": {}, "signal_end": signal_end, "model_id": "a" * 64}
        quote = {"ask": 100.01, "bid": 100.0, "asof_ms": now}
        self.runtime.store.transaction(lambda doc: open_trade(doc, "ETH", signal, quote, self.runtime.rules, now))
        before = copy.deepcopy(self.runtime.store.snapshot())
        applied_engine = self.runtime.engine
        document, revision = read_editor(self.runtime)
        next(entry for entry in document["assets"] if entry["name"] == "ETH")["enabled"] = False
        saved = self.post("/api/settings/save", {"settings": document, "revision": revision})
        self.assertEqual(saved.status_code, 200, saved.json)
        applied = self.post("/api/settings/apply")
        self.assertEqual(applied.status_code, 400, applied.json)
        self.assertIn("Keep ETH enabled", applied.json["error"])
        restarted = self.post("/api/dashboard/restart")
        self.assertEqual(restarted.status_code, 400, restarted.json)
        self.assertIn("Keep ETH enabled", restarted.json["error"])
        self.assertIs(self.runtime.engine, applied_engine)
        self.assertEqual(self.runtime.store.snapshot(), before)
        self.restart.assert_not_called()


class BundledEnsembleReportTests(unittest.TestCase):
    def test_a_matching_report_hash_cannot_hide_failed_ensemble_evaluation(self):
        root = Path(neural_models.__file__).resolve().parent / "models" / "candidates" / "probability_ensemble_v1"
        bundles = sorted(root.glob("*/manifest.json"))
        if not bundles:
            self.skipTest("No bundled ensemble artifact")
        source = bundles[-1].parent
        model_id = f"probability_ensemble_v1@{source.name}"
        neural_models._manifest(source, model_id)
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder)
            manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
            report = json.loads((source / manifest["report"]).read_text(encoding="utf-8"))
            report["evaluation"]["status"] = "failed"
            raw = json.dumps(report, sort_keys=True).encode("utf-8")
            (bundle / manifest["report"]).write_bytes(raw)
            manifest["report_sha256"] = hashlib.sha256(raw).hexdigest()
            (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(DataError, "Ensemble evaluation is incomplete"):
                neural_models._manifest(bundle, model_id)


if __name__ == "__main__":
    unittest.main()
