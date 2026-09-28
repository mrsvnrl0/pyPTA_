"""Portable-source reproducibility and private-data exclusion regressions."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from adaptive_crypto.core import DEFAULT_ASSETS, Rules, load_application_settings
from tools.prepare_windows_build import prepare
from tools.source_release import (
    FIXED_FILES, MANIFEST_NAME, ZIP_TIMESTAMP, build_release,
    candidate_artifact_paths, collect_sources, packaged_model_paths,
)


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        for name in FIXED_FILES:
            self.write(name, ("public fixture: " + name + "\n").encode())
        self.write("adaptive_crypto/core.py", b"VALUE = 1\n")
        self.write("test_regression.py", b"# offline test\n")
        self.write("test_browser_ui.js", b"// browser test\n")
        self.write("adaptive_crypto/templates/dashboard.html", b"<p>Dashboard</p>\n")
        self.write("adaptive_crypto/static/dashboard.js", b"'use strict';\n")
        self.write("packaging/windows/licenses/Runtime-notices.txt", b"License notice\n")

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def candidate_bundle(self, family="lstm_classifier_v1"):
        """Hash-consistent packaging fixture; inference is tested elsewhere."""
        model = b"small test-only ONNX bytes"
        report = b'{"classification":{"BTC":{"count":100}}}\n'
        manifest = {
            "schema": 1, "id": family, "display_name": family + " fixture",
            "feature_schema": "kraken-ohlcv-16-v1",
            "feature_names": [f"feature_{index}" for index in range(16)],
            "classes": ["BUY", "HOLD", "SELL"],
            "input_shape": [1, 64, 16], "output_shape": [1, 3],
            "timeframe_ms": 14_400_000, "history_bars": 319,
            "target_horizon_bars": 2, "label_version": "parente-source-5-2-v1",
            "latest_model_selection_through_ms": 1_800_000_000_000,
            "members": {"BTC": {"onnx": "BTC.onnx", "sha256": hashlib.sha256(model).hexdigest(),
                                "mean": [0.0] * 16, "scale": [1.0] * 16,
                                "trained_through_ms": 1_700_000_000_000,
                                "dataset_sha256": "a" * 64}},
            "report": "report.json", "report_sha256": hashlib.sha256(report).hexdigest(),
        }
        canonical = json.dumps({key: value for key, value in manifest.items()
                                if key not in {"artifact_id", "report", "report_sha256"}},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        manifest["artifact_id"] = hashlib.sha256(canonical).hexdigest()
        prefix = f"adaptive_crypto/models/candidates/{family}/{manifest['artifact_id'][:12]}"
        self.write(prefix + "/BTC.onnx", model)
        self.write(prefix + "/report.json", report)
        self.write(prefix + "/manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        return prefix

    def ensemble_bundle(self):
        families = ("lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
                    "grouped_attention_lstm_v1")
        component_paths = [self.candidate_bundle(family) for family in families]
        components = []
        for prefix in component_paths:
            raw = (self.root / prefix / "manifest.json").read_bytes()
            component = json.loads(raw)
            components.append({"model_id": component["id"] + "@" + component["artifact_id"][:12],
                               "artifact_id": component["artifact_id"],
                               "manifest_sha256": hashlib.sha256(raw).hexdigest()})
        payload = {
            "schema": "fixed-probability-ensemble-v1",
            "architecture_id": "probability_ensemble_v1", "feature_schema": "kraken-ohlcv-16-v1",
            "label_version": "parente-source-5-2-v1", "timeframe_ms": 14_400_000,
            "target_horizon_bars": 2, "history_bars": 319,
            "classes": ["BUY", "HOLD", "SELL"], "weights": [.25] * 4,
            "components": components, "coverage": ["BTC"],
            "latest_model_selection_through_ms": 1_800_000_000_000,
        }
        identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False).encode()).hexdigest()
        report = b'{"comparison":"fixture"}\n'
        manifest = {
            "schema": 1, "id": "probability_ensemble_v1", "artifact_id": identity,
            "display_name": "Fixed probability ensemble fixture", "feature_schema": "kraken-ohlcv-16-v1",
            "feature_names": [f"feature_{index}" for index in range(16)],
            "classes": ["BUY", "HOLD", "SELL"], "input_shape": [1, 64, 16],
            "output_shape": [1, 3], "timeframe_ms": 14_400_000, "history_bars": 319,
            "target_horizon_bars": 2, "label_version": "parente-source-5-2-v1",
            "latest_model_selection_through_ms": 1_800_000_000_000,
            "components": components, "weights": [.25] * 4, "coverage": ["BTC"],
            "identity_payload": payload, "report": "report.json",
            "report_sha256": hashlib.sha256(report).hexdigest(),
        }
        prefix = f"adaptive_crypto/models/candidates/probability_ensemble_v1/{identity[:12]}"
        self.write(prefix + "/manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        self.write(prefix + "/report.json", report)
        return component_paths, prefix

    def test_deterministic_bytes_hashes_and_zip_metadata(self):
        result = build_release(self.root, self.root / "release", "test-1")
        original = Path(result["archive"]).read_bytes()
        # Touch and relocate sources: neither mtime nor the source path belongs
        # in the archive identity. A changed Git index is irrelevant too.
        for path in self.root.rglob("*"):
            if path.is_file():
                os.utime(path, (1_700_000_000, 1_700_000_000))
        self.write(".git/index", b"unrelated staged content")
        second = build_release(self.root, self.root / "other-output", "test-1")
        self.assertEqual(original, Path(second["archive"]).read_bytes())
        self.assertEqual(hashlib.sha256(original).hexdigest(), result["sha256"])
        with zipfile.ZipFile(result["archive"]) as archive:
            manifest_bytes = archive.read(MANIFEST_NAME)
            self.assertEqual(manifest_bytes, Path(result["manifest"]).read_bytes())
            manifest = json.loads(manifest_bytes)
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
            self.assertEqual(len(manifest["files"]), result["source_files"])
            for entry in manifest["files"]:
                data = archive.read(entry["path"])
                self.assertEqual(entry["size"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            for info in archive.infolist():
                self.assertEqual(info.date_time, ZIP_TIMESTAMP)
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(stat.S_IMODE(info.external_attr >> 16), 0o644)
        for line in Path(result["checksums"]).read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((Path(result["checksums"]).parent / name).read_bytes()).hexdigest())
        self.write("adaptive_crypto/core.py", b"VALUE = 2\n")
        changed = build_release(self.root, self.root / "changed-output", "test-1")
        self.assertNotEqual(result["sha256"], changed["sha256"])

    def test_personal_runtime_and_generated_files_never_enter_archive(self):
        private_names = (
            "adaptive_crypto_settings.json", ".env", ".git/index",
            "adaptive_crypto_reclaim_state.sqlite3", "state-backup.sqlite3-wal",
            "connections.dat", "deribit-credentials.dat", "deribit-credentials.dat.pending",
            "ai-credentials.dat", "telegram-credentials.dat", ".connection-private.tmp",
            "market-brief-timing.json", "market-brief-timing.json.private.tmp",
            "desktop-session.json", "dashboard.log",
            "backup/latest.zip", "release/old-source.zip",
            ".venv/Lib/site-packages/private.py", ".qa/report.html",
            "recovered_settings/recovered.json", "qualification_audit/positions.py",
            "sweep_audit/btc_buy_watch_investigation_state.json",
            "nn-homepage-backup-20260913/module.py", "docs/private-notes.md",
            "adaptive_crypto/models/personal.npz", "adaptive_crypto/positions.json",
            "adaptive_crypto/__pycache__/core.pyc", "test_data/private-trades.json",
            "packaging/windows/personal-settings.json",
        )
        marker = b"PRIVATE-RECORD-MUST-NEVER-BE-ARCHIVED"
        for name in private_names:
            self.write(name, marker)
        result = build_release(self.root, self.root / "release", "private-check")
        with zipfile.ZipFile(result["archive"]) as archive:
            for name in private_names:
                self.assertNotIn(name, archive.namelist())
            self.assertIn("test_browser_ui.js", archive.namelist())
            self.assertIn("test_data/neural_author_btc.json", archive.namelist())
            self.assertNotIn(marker, Path(result["archive"]).read_bytes())

    def test_missing_required_model_fails_without_emitting_release(self):
        (self.root / "adaptive_crypto/models/parente_5_2.npz").unlink()
        output = self.root / "release"
        with self.assertRaises(FileNotFoundError):
            build_release(self.root, output)
        self.assertFalse(output.exists())

    def test_candidate_bundle_is_explicitly_selected_and_training_outputs_excluded(self):
        prefix = self.candidate_bundle()
        marker = b"NEVER-SHIP-TRAINING-OR-PRIVATE-DATA"
        for name in ("raw.csv", "checkpoint.pt", "secret.env", "training.log"):
            self.write(prefix + "/" + name, marker)
        self.write(".qa/candidate_data/BTCUSD_240.csv", marker)
        self.write("adaptive_crypto/models/candidates/unknown_model_v1/secret.onnx", marker)
        expected = {prefix + "/manifest.json", prefix + "/report.json", prefix + "/BTC.onnx"}
        paths = {path.relative_to(self.root).as_posix() for path in candidate_artifact_paths(self.root)}
        self.assertEqual(paths, expected)
        frozen_paths = {path.relative_to(self.root).as_posix() for path in packaged_model_paths(self.root)}
        self.assertEqual(frozen_paths, expected | {
            "adaptive_crypto/models/NOTICE.md", "adaptive_crypto/models/parente_5_2.json",
            "adaptive_crypto/models/parente_5_2.npz",
        })
        result = build_release(self.root, self.root / "release", "candidate")
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertTrue(expected.issubset(archive.namelist()))
            self.assertNotIn(marker, Path(result["archive"]).read_bytes())
            self.assertFalse(any(name.startswith(".qa/") for name in archive.namelist()))

    def test_ensemble_packages_only_verified_reference_bundle_files(self):
        components, ensemble = self.ensemble_bundle()
        marker = b"ENSEMBLE-EXTRA-MUST-NOT-SHIP"
        self.write(ensemble + "/predictions.csv", marker)
        self.write(ensemble + "/checkpoint.pt", marker)
        expected = {ensemble + "/manifest.json", ensemble + "/report.json"}
        for prefix in components:
            expected.update({prefix + "/manifest.json", prefix + "/report.json",
                             prefix + "/BTC.onnx"})
        selected = {path.relative_to(self.root).as_posix() for path in candidate_artifact_paths(self.root)}
        self.assertEqual(selected, expected)
        result = build_release(self.root, self.root / "release", "ensemble")
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertTrue(expected.issubset(archive.namelist()))
            self.assertNotIn(marker, Path(result["archive"]).read_bytes())
        payload = json.loads((self.root / ensemble / "manifest.json").read_text())
        payload["components"][0]["manifest_sha256"] = "0" * 64
        payload["identity_payload"]["components"] = payload["components"]
        new_id = hashlib.sha256(json.dumps(payload["identity_payload"], sort_keys=True,
                                           separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        payload["artifact_id"] = new_id
        invalid = f"adaptive_crypto/models/candidates/probability_ensemble_v1/{new_id[:12]}"
        self.write(invalid + "/manifest.json", (json.dumps(payload) + "\n").encode())
        self.write(invalid + "/report.json", (self.root / ensemble / "report.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "unvalidated or changed component"):
            candidate_artifact_paths(self.root)

    def test_corrupt_candidate_identity_or_payload_blocks_release(self):
        prefix = self.candidate_bundle()
        output = self.root / "release"
        self.write(prefix + "/BTC.onnx", b"modified weights")
        with self.assertRaisesRegex(ValueError, "ONNX hash mismatch"):
            build_release(self.root, output)
        self.assertFalse(output.exists())
        self.candidate_bundle()
        self.write(prefix + "/report.json", b"modified report")
        with self.assertRaisesRegex(ValueError, "report hash mismatch"):
            build_release(self.root, output)
        self.assertFalse(output.exists())
        self.candidate_bundle()
        manifest = json.loads((self.root / prefix / "manifest.json").read_text())
        manifest["feature_schema"] = "wrong-schema"
        self.write(prefix + "/manifest.json", json.dumps(manifest).encode())
        with self.assertRaisesRegex(ValueError, "artifact identity mismatch"):
            build_release(self.root, output)
        self.assertFalse(output.exists())

    def test_candidate_manifest_cannot_reference_files_outside_bundle(self):
        prefix = self.candidate_bundle()
        manifest = json.loads((self.root / prefix / "manifest.json").read_text())
        manifest["members"]["BTC"]["onnx"] = "../secret.onnx"
        canonical = json.dumps({key: value for key, value in manifest.items()
                                if key not in {"artifact_id", "report", "report_sha256"}},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        # Identity remains tied to the original directory: changing the model
        # declaration must fail before a path could be read.
        self.assertNotEqual(manifest["artifact_id"], hashlib.sha256(canonical).hexdigest())
        self.write(prefix + "/manifest.json", json.dumps(manifest).encode())
        with self.assertRaises(ValueError):
            candidate_artifact_paths(self.root)

    def test_invalid_label_cannot_escape_output_directory(self):
        output = self.root / "release"
        with self.assertRaises(ValueError):
            build_release(self.root, output, "../../escape")
        self.assertFalse(output.exists())

    def test_windows_preparation_uses_example_not_live_settings(self):
        prefix = self.candidate_bundle()
        self.write(prefix + "/checkpoint.pt", b"PRIVATE-CHECKPOINT")
        self.write("adaptive_crypto_settings.json", b"PRIVATE-LIVE-CONFIG")
        destination = Path(self.temporary.name) / "build-stage"
        prepare(self.root, destination)
        self.assertEqual((destination / "adaptive_crypto_settings.json").read_bytes(),
                         (self.root / "adaptive_crypto_settings.example.json").read_bytes())
        self.assertEqual((destination / "adaptive_crypto/desktop.py").read_bytes(),
                         (self.root / "packaging/windows/desktop.py").read_bytes())
        self.assertTrue((destination / "packaging/pyPTA.spec").is_file())
        self.assertTrue((destination / "adaptive_crypto/models/self_test_btc.json").is_file())
        self.assertTrue((destination / prefix / "BTC.onnx").is_file())
        self.assertTrue((destination / prefix / "manifest.json").is_file())
        self.assertFalse((destination / prefix / "checkpoint.pt").exists())
        self.assertTrue((destination / "release/pyPTA-Source-1.0.1.zip").is_file())
        self.assertTrue((destination / "packaging/README-Open.txt").is_file())
        with self.assertRaises(FileExistsError):
            prepare(self.root, destination)

    def test_source_symlink_is_rejected(self):
        path = self.root / "adaptive_crypto/core.py"
        path.unlink()
        external = Path(self.temporary.name) / "private.py"
        external.write_bytes(b"PRIVATE")
        try:
            path.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("This platform/account cannot create symbolic links")
        with self.assertRaisesRegex(ValueError, "links/reparse"):
            collect_sources(self.root)


class ExampleSettingsTests(unittest.TestCase):
    def test_portable_example_matches_public_application_defaults(self):
        path = Path(__file__).with_name("adaptive_crypto_settings.example.json")
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document, {"assets": DEFAULT_ASSETS, "refresh_seconds": 15,
                                  "strategy": asdict(Rules(strategy_model="neural_network"))})
        assets, rules, refresh = load_application_settings(path)
        self.assertEqual(set(assets), {item["name"] for item in DEFAULT_ASSETS})
        self.assertEqual(rules.strategy_model, "neural_network")
        self.assertEqual(refresh, 15)


if __name__ == "__main__":
    unittest.main()
