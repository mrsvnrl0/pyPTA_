"""Measure the installed CPU candidate adapter on prepared 4H histories.

Each architecture runs in a fresh Python process so its Windows working-set
measurement does not include another architecture's ONNX sessions.  Timings
include causal feature construction, scaling, ONNX inference and probability
mapping.  Data fetch and dashboard orchestration are outside this measurement.
"""
from __future__ import annotations

import argparse
from collections import deque
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

from adaptive_crypto.core import Candle, H4
from adaptive_crypto.neural_models import CANDIDATE_ROOT, CandidateModel


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _windows_memory():
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                "PrivateUsage")]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                     ctypes.byref(counters), counters.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return {"working_set_bytes": int(counters.WorkingSetSize),
            "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
            "private_bytes": int(counters.PrivateUsage)}


def _history(path):
    by_asset = {}
    with Path(path).open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            asset = row["asset"]
            candle = Candle(int(row["open_time_ms"]),
                            *(float(row[key]) for key in ("open", "high", "low", "close", "volume")), H4)
            by_asset.setdefault(asset, deque(maxlen=319)).append(candle)
    return {asset: list(candles) for asset, candles in by_asset.items()}


def _timing(values):
    ordered = sorted(values)
    return {"count": len(values), "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
            "max_ms": ordered[-1]}


def measure(model_id, candles_path, repetitions):
    family, _, prefix = model_id.partition("@")
    histories = _history(candles_path)
    before = _windows_memory()
    model = CandidateModel(model_id, CANDIDATE_ROOT / family / prefix)
    assets = sorted(model.members)
    if any(len(histories.get(asset, ())) != model.required_history_bars for asset in assets):
        raise ValueError("Prepared 319-bar histories are unavailable")
    for asset in assets:
        model.predict(histories[asset], asset)
    loaded = _windows_memory()
    per_asset = {asset: [] for asset in assets}
    total = []
    for _ in range(repetitions):
        cycle = time.perf_counter_ns()
        for asset in assets:
            start = time.perf_counter_ns()
            model.predict(histories[asset], asset)
            per_asset[asset].append((time.perf_counter_ns() - start) / 1_000_000)
        total.append((time.perf_counter_ns() - cycle) / 1_000_000)
    after = _windows_memory()
    return {"model_id": model_id, "artifact_id": model.artifact_id,
            "manifest_sha256": _sha(model.path / "manifest.json"),
            "candles_sha256": _sha(candles_path),
            "method": "Fresh Windows process per architecture; 319 prepared contiguous completed Kraken/USD 4H bars per asset; one warm call; then sequential BTC/ETH/SOL/SUI adapter predictions. Includes feature construction, scaler, ONNX CPU and softmax; excludes data fetch and dashboard orchestration.",
            "repetitions": repetitions, "asset_order": assets,
            "per_asset": {asset: _timing(values) for asset, values in per_asset.items()},
            "four_asset_total": _timing(total),
            "windows_process_memory": {
                "before_model_load": before, "after_model_load_and_warmup": loaded,
                "after_benchmark": after,
                "warm_working_set_delta_bytes": (loaded["working_set_bytes"] - before["working_set_bytes"])
                if before and loaded else None},
            "python": platform.python_version(), "platform": platform.platform(),
            "measured_utc": datetime.now(timezone.utc).isoformat()}


def all_models(output, repetitions):
    output = Path(output)
    candles = output / "combined_candles_4h.csv"
    model_ids = [f"{family.name}@{version.name}"
                 for family in sorted(CANDIDATE_ROOT.iterdir())
                 if family.is_dir() and family.name != "probability_ensemble_v1"
                 for version in sorted(family.iterdir()) if (version / "manifest.json").is_file()]
    if len(model_ids) != 4:
        raise ValueError(f"Expected four trained architecture bundles; found {len(model_ids)}")
    result = {"schema": 1, "model_results": {}}
    for model_id in model_ids:
        proc = subprocess.run([sys.executable, "-m", "tools.benchmark_nn_candidate_runtime",
                               "--single", model_id, "--candles", str(candles),
                               "--repetitions", str(repetitions)],
                              check=True, capture_output=True, text=True)
        info = json.loads(proc.stdout)
        result["model_results"][model_id] = info
        print(f"{model_id}: four-asset p50={info['four_asset_total']['p50_ms']:.3f} ms, "
              f"p95={info['four_asset_total']['p95_ms']:.3f} ms; "
              f"warm working set={info['windows_process_memory']['after_model_load_and_warmup']['working_set_bytes']:,} bytes",
              flush=True)
    path = output / "runtime_benchmark.json"
    path.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Wrote {path}")


def attach(output):
    output = Path(output)
    benchmark_path = output / "runtime_benchmark.json"
    evidence = json.loads(benchmark_path.read_text(encoding="utf-8"))
    if evidence.get("schema") != 1 or len(evidence.get("model_results", {})) != 4:
        raise ValueError("Incomplete runtime benchmark")
    for model_id, measurement in evidence["model_results"].items():
        family, _, prefix = model_id.partition("@")
        bundle = CANDIDATE_ROOT / family / prefix
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report_path = bundle / manifest["report"]
        if manifest["report_sha256"] != _sha(report_path):
            raise ValueError(f"Candidate report changed: {model_id}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        source_sha = _sha(benchmark_path)
        if report.get("runtime_benchmark_source_sha256") == source_sha:
            if report.get("runtime_benchmark") != measurement:
                raise ValueError(f"Attached runtime measurement differs: {model_id}")
            print(f"Runtime benchmark already attached to {model_id}")
            continue
        if measurement["artifact_id"] != manifest["artifact_id"] or measurement["manifest_sha256"] != _sha(manifest_path):
            raise ValueError(f"Runtime measurement manifest changed: {model_id}")
        report["runtime_benchmark"] = measurement
        report["runtime_benchmark_source_sha256"] = source_sha
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        manifest["report_sha256"] = _sha(report_path)
        manifest_path.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"Attached Windows CPU benchmark to {model_id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--single")
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--attach", action="store_true")
    parser.add_argument("--candles")
    parser.add_argument("--output")
    parser.add_argument("--repetitions", type=int, default=50)
    args = parser.parse_args()
    if not 10 <= args.repetitions <= 500:
        parser.error("Repetitions must be 10–500")
    if args.single:
        if not args.candles:
            parser.error("--single needs --candles")
        print(json.dumps(measure(args.single, args.candles, args.repetitions), allow_nan=False))
    elif args.all:
        if not args.output:
            parser.error("--all needs --output")
        all_models(args.output, args.repetitions)
    else:
        if not args.output:
            parser.error("--attach needs --output")
        attach(args.output)


if __name__ == "__main__":
    main()
