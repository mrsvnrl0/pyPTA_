"""Create a deterministic, allowlisted source archive without reading runtime data.

Uses only the standard library. The working tree is the source of truth: Git's
index is neither read nor changed. ZIP_STORED avoids compression-library drift.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FIXED_FILES = (
    ".gitignore", "README.md", "adaptive_crypto_dashboard.py",
    "adaptive_crypto_settings.example.json", "requirements.txt",
    "requirements-neural.txt", "requirements-runtime-lock.txt",
    "requirements-candidate-training.txt", "requirements-candidate-runtime.txt",
    "build_windows_installers.ps1", "start_dashboard.ps1",
    "start_dashboard_background.ps1",
    "adaptive_crypto/models/NOTICE.md",
    "adaptive_crypto/models/parente_5_2.json",
    "adaptive_crypto/models/parente_5_2.npz",
    "docs/HISTORICAL_DASHBOARD.md", "docs/NEURAL_STRATEGY.md",
    "docs/PERSISTENCE_SQLITE_PLAN.md", "docs/SQLITE_PERSISTENCE.md",
    "docs/RELEASE_BASELINE.md", "docs/NN_CANDIDATE_EVALUATION.md",
    "gex_target_audit/FORMULA.md",
    "docs/GPT6_SOLAR_NN_CANDIDATES_HANDOFF.md",
    "benchmarks/persistence_benchmark.py",
    "test_data/btc_spike_20260911.json", "test_data/neural_author_btc.json",
    "packaging/windows/dashboard.ico", "packaging/windows/desktop.py",
    "packaging/windows/installer.nsi", "packaging/windows/pyPTA.spec",
    "packaging/windows/requirements-lock.txt", "packaging/windows/self_test_btc.json",
    "packaging/windows/version.txt", "packaging/windows/windows_launcher.py",
    "tools/source_release.py", "tools/prepare_windows_build.py",
    "tools/audit_nn_candidate_activation.py",
    "tools/backup_live_nn_candidate_activation.py",
    "tools/benchmark_nn_candidate_runtime.py",
    "tools/build_nn_candidate_ensemble.py",
    "tools/candidate_train_pipeline.py",
    "tools/compare_nn_candidate_reports.py",
    "tools/derive_nn_comparison_experiment.py",
    "tools/evaluate_nn_candidates.py",
    "tools/package_nn_candidate_ensemble.py",
    "tools/predict_parente_candidate_baseline.py",
    "tools/preview_nn_model_selector.js",
    "tools/train_nn_candidates.py",
    "tools/train_nn_simple_baselines.py",
    "tools/verify_nn_ensemble_runtime.py",
)
# Deliberately narrow rules; new runtime files or research directories are not
# included just because they are tracked by Git or sit beside application code.
SOURCE_GLOBS = (
    "adaptive_crypto/*.py", "adaptive_crypto/templates/*.html",
    "adaptive_crypto/static/*.css", "adaptive_crypto/static/*.js",
    "test_*.py", "test_*.js", "packaging/windows/licenses/*.txt",
)
MANIFEST_NAME = "SOURCE_MANIFEST.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
CANDIDATE_FAMILIES = frozenset((
    "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
    "grouped_attention_lstm_v1", "probability_ensemble_v1",
))
CANDIDATE_COMPONENT_ORDER = (
    "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
    "grouped_attention_lstm_v1",
)
CANDIDATE_VERSION = re.compile(r"[0-9a-f]{12}\Z")
CANDIDATE_SCHEMA = "kraken-ohlcv-16-v1"
CANDIDATE_LABEL = "parente-source-5-2-v1"
CANDIDATE_MAX_MANIFEST = 250_000
CANDIDATE_MAX_MODEL = 20_000_000
CANDIDATE_MAX_REPORT = 5_000_000


def _checked_file(root: Path, path: Path) -> Path:
    """Reject links/junctions rather than inadvertently package their targets."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise ValueError(f"Source links/reparse points are not allowed: {relative.as_posix()}")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError(f"Source is not a regular file inside the root: {relative.as_posix()}")
    return path


def _checked_directory(root: Path, path: Path) -> None:
    """Reject a reparse point before enumerating a candidate bundle directory."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise ValueError(f"Source links/reparse points are not allowed: {relative.as_posix()}")
    if not path.is_dir():
        raise ValueError(f"Candidate path is not a directory: {relative.as_posix()}")


def _candidate_data(root: Path, path: Path, maximum: int) -> bytes:
    file_path = _checked_file(root, path)
    if file_path.stat().st_size > maximum:
        raise ValueError(f"Candidate artifact exceeds size limit: {path.relative_to(root).as_posix()}")
    return file_path.read_bytes()


def _candidate_filename(value: object, extension: str) -> str:
    if (type(value) is not str or not value.endswith(extension) or value in {".", ".."}
            or Path(value).name != value or any(char in value for char in ("/", "\\", ":", "\x00"))):
        raise ValueError("Candidate manifest references an invalid filename")
    return value


def _candidate_sha(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def candidate_artifact_paths(root: Path) -> set[Path]:
    """Select only immutable, hash-checked ONNX bundles; never scan their extras.

    Source assembly deliberately uses the standard library. Runtime ONNX tensor
    validation is a separate release check, performed by the candidate loader.
    """
    root = Path(root).resolve(strict=True)
    candidate_root = root / "adaptive_crypto" / "models" / "candidates"
    if not candidate_root.exists() and not candidate_root.is_symlink():
        return set()
    _checked_directory(root, candidate_root)
    paths: set[Path] = set()
    validated: dict[tuple[str, str], tuple[dict, str]] = {}
    # The ensemble refers to the four previously validated single-network
    # bundles by immutable manifest hash; process it after those bundles.
    family_order = (*sorted(CANDIDATE_FAMILIES - {"probability_ensemble_v1"}),
                    "probability_ensemble_v1")
    for family in family_order:
        family_path = candidate_root / family
        if not family_path.exists() and not family_path.is_symlink():
            continue
        _checked_directory(root, family_path)
        for bundle in sorted(family_path.iterdir()):
            if not CANDIDATE_VERSION.fullmatch(bundle.name):
                continue
            _checked_directory(root, bundle)
            manifest_path = bundle / "manifest.json"
            raw = _candidate_data(root, manifest_path, CANDIDATE_MAX_MANIFEST)
            try:
                manifest = json.loads(raw)
                if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("id") != family:
                    raise ValueError("Candidate manifest architecture or schema mismatch")
                identity = manifest.get("artifact_id")
                # Validation reports contain the artifact ID, so the ID covers
                # the frozen model declaration while report bytes get their
                # own SHA256 in the final manifest.
                operational = (manifest.get("identity_payload") if family == "probability_ensemble_v1"
                               else {key: value for key, value in manifest.items()
                                     if key not in {"artifact_id", "report", "report_sha256"}})
                canonical = json.dumps(operational,
                                       sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                if (not _candidate_sha(identity) or not identity.startswith(bundle.name)
                        or hashlib.sha256(canonical).hexdigest() != identity):
                    raise ValueError("Candidate manifest artifact identity mismatch")
                if (manifest.get("feature_schema") != CANDIDATE_SCHEMA
                        or manifest.get("classes") != ["BUY", "HOLD", "SELL"]
                        or manifest.get("input_shape") != [1, 64, 16]
                        or manifest.get("output_shape") != [1, 3]
                        or manifest.get("timeframe_ms") != 14_400_000
                        or manifest.get("history_bars") != 319
                        or manifest.get("target_horizon_bars") != 2
                        or manifest.get("label_version") != CANDIDATE_LABEL):
                    raise ValueError("Candidate feature or target contract mismatch")
                names = manifest.get("feature_names")
                if (not isinstance(names, list) or len(names) != 16 or len(set(names)) != 16
                        or any(type(name) is not str or not name for name in names)):
                    raise ValueError("Candidate feature names are missing")
                cutoff = manifest.get("latest_model_selection_through_ms")
                if type(cutoff) is not int or cutoff < 0:
                    raise ValueError("Candidate cutoff is missing")
                if family == "probability_ensemble_v1":
                    components = manifest.get("components")
                    coverage = manifest.get("coverage")
                    weights = manifest.get("weights")
                    if (not isinstance(components, list) or len(components) != 4
                            or not isinstance(weights, list) or weights != [.25] * 4
                            or not isinstance(coverage, list) or not coverage
                            or coverage != sorted(set(coverage))
                            or any(type(asset) is not str or not asset.isalnum() or asset != asset.upper()
                                   for asset in coverage)):
                        raise ValueError("Candidate fixed ensemble contract is invalid")
                    supported = None
                    for component_family, component in zip(CANDIDATE_COMPONENT_ORDER, components):
                        if not isinstance(component, dict) or set(component) != {"model_id", "artifact_id", "manifest_sha256"}:
                            raise ValueError("Candidate ensemble component is invalid")
                        component_id = component.get("model_id")
                        if (type(component_id) is not str or not component_id.startswith(component_family + "@")
                                or not _candidate_sha(component.get("artifact_id"))
                                or not _candidate_sha(component.get("manifest_sha256"))):
                            raise ValueError("Candidate ensemble component identity is invalid")
                        prefix = component_id.split("@", 1)[1]
                        record = validated.get((component_family, prefix))
                        if (record is None or record[0]["artifact_id"] != component["artifact_id"]
                                or record[1] != component["manifest_sha256"]):
                            raise ValueError("Candidate ensemble references an unvalidated or changed component")
                        if record[0]["latest_model_selection_through_ms"] > cutoff:
                            raise ValueError("Candidate ensemble cutoff precedes a component")
                        asset_set = set(record[0]["members"])
                        supported = asset_set if supported is None else supported & asset_set
                    if set(coverage) != supported:
                        raise ValueError("Candidate ensemble coverage differs from its components")
                    expected_payload = {
                        "schema": "fixed-probability-ensemble-v1", "architecture_id": family,
                        "feature_schema": CANDIDATE_SCHEMA, "label_version": CANDIDATE_LABEL,
                        "timeframe_ms": 14_400_000, "target_horizon_bars": 2,
                        "history_bars": 319, "classes": ["BUY", "HOLD", "SELL"],
                        "weights": [.25] * 4, "components": components, "coverage": coverage,
                        "latest_model_selection_through_ms": cutoff,
                    }
                    if manifest.get("identity_payload") != expected_payload:
                        raise ValueError("Candidate ensemble identity payload is invalid")
                else:
                    members = manifest.get("members")
                    if not isinstance(members, dict) or not members:
                        raise ValueError("Candidate trained assets are missing")
                    filenames = set()
                    for asset, member in members.items():
                        if type(asset) is not str or not asset.isalnum() or asset != asset.upper() or not isinstance(member, dict):
                            raise ValueError("Candidate asset metadata is invalid")
                        filename = _candidate_filename(member.get("onnx"), ".onnx")
                        if filename in filenames or not _candidate_sha(member.get("sha256")):
                            raise ValueError("Candidate ONNX filename or hash is invalid")
                        filenames.add(filename)
                        for key, positive in (("mean", False), ("scale", True)):
                            values = member.get(key)
                            if (not isinstance(values, list) or len(values) != 16
                                    or any(type(value) not in (int, float) or not math.isfinite(value)
                                           or positive and value <= 0 for value in values)):
                                raise ValueError(f"Candidate {key} is invalid")
                        trained = member.get("trained_through_ms")
                        if (type(trained) is not int or trained < 0 or trained > cutoff
                                or not _candidate_sha(member.get("dataset_sha256"))):
                            raise ValueError("Candidate training provenance is invalid")
                        model_path = bundle / filename
                        if hashlib.sha256(_candidate_data(root, model_path, CANDIDATE_MAX_MODEL)).hexdigest() != member["sha256"]:
                            raise ValueError(f"Candidate {asset} ONNX hash mismatch")
                        paths.add(model_path)
                report_name = _candidate_filename(manifest.get("report"), ".json")
                if report_name == "manifest.json" or not _candidate_sha(manifest.get("report_sha256")):
                    raise ValueError("Candidate report metadata is invalid")
                report_path = bundle / report_name
                report_bytes = _candidate_data(root, report_path, CANDIDATE_MAX_REPORT)
                if hashlib.sha256(report_bytes).hexdigest() != manifest["report_sha256"]:
                    raise ValueError("Candidate report hash mismatch")
                if not isinstance(json.loads(report_bytes), dict):
                    raise ValueError("Candidate report must be a JSON object")
            except (TypeError, OverflowError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid candidate bundle {bundle.relative_to(root).as_posix()}: {exc}") from exc
            paths.update((manifest_path, report_path))
            validated[(family, bundle.name)] = manifest, hashlib.sha256(raw).hexdigest()
    return paths


def packaged_model_paths(root: Path) -> set[Path]:
    """The only model files permitted in source archives or frozen builds."""
    root = Path(root).resolve(strict=True)
    base = root / "adaptive_crypto" / "models"
    return {base / name for name in ("NOTICE.md", "parente_5_2.json", "parente_5_2.npz")} | candidate_artifact_paths(root)


def collect_sources(root: Path) -> dict[str, bytes]:
    """Read only explicitly permitted sources, failing on missing required files."""
    root = Path(root).resolve(strict=True)
    paths = {root / name for name in FIXED_FILES}
    for pattern in SOURCE_GLOBS:
        paths.update(root.glob(pattern))
    paths.update(packaged_model_paths(root))
    sources = {}
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        sources[name] = _checked_file(root, path).read_bytes()
    if not any(name.startswith("test_") and name.endswith(".py") for name in sources):
        raise ValueError("Source baseline requires its Python regression tests")
    return sources


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def release_bytes(sources: dict[str, bytes], label: str) -> tuple[bytes, bytes]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", label):
        raise ValueError("Label must be 1-80 filename-safe letters, numbers, dots, underscores or hyphens")
    files = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
             for name, data in sorted(sources.items())]
    manifest = _json_bytes({
        "schema": 1, "product": "pyPTA", "label": label,
        "scope": "allowlisted working-tree source; no runtime data or Git index",
        "content_sha256": hashlib.sha256(_json_bytes(files)).hexdigest(),
        "files": files,
    })
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted({**sources, MANIFEST_NAME: manifest}.items()):
            info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    return output.getvalue(), manifest


def _atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Output must be a regular file: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=".source-release-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_release(sources: dict[str, bytes], output_dir: Path, label: str = "baseline") -> dict[str, str | int]:
    archive, manifest = release_bytes(sources, label)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"pyPTA-Source-{label}"
    archive_path = output_dir / (stem + ".zip")
    manifest_path = output_dir / (stem + ".manifest.json")
    checksum_path = output_dir / (stem + ".sha256")
    checksum = hashlib.sha256(archive).hexdigest()
    hashes = (f"{checksum}  {archive_path.name}\n"
              f"{hashlib.sha256(manifest).hexdigest()}  {manifest_path.name}\n").encode("ascii")
    # The checksums file is written last and acts as the completed-output marker.
    for path, data in ((archive_path, archive), (manifest_path, manifest), (checksum_path, hashes)):
        _atomic_write(path, data)
    return {"archive": str(archive_path), "manifest": str(manifest_path),
            "checksums": str(checksum_path), "sha256": checksum,
            "source_files": len(sources), "bytes": len(archive)}


def build_release(root: Path, output_dir: Path, label: str = "baseline") -> dict[str, str | int]:
    return write_release(collect_sources(root), output_dir, label)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release" / "source")
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()
    print(json.dumps(build_release(args.root, args.output_dir, args.label), indent=2))


if __name__ == "__main__":
    main()
