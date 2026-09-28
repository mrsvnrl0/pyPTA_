"""Reproducible, offline training/export for Kraken 4H NN research candidates.

Only data-only ONNX and JSON go into the deployable candidate bundle. Training
state, source candles, predictions and logs stay in the experiment directory.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import time

import numpy as np

from adaptive_crypto.candidate_features import (
    FEATURE_GROUPS, FEATURE_NAMES, FEATURE_SCHEMA, HISTORY_BARS, LOOKBACK_ROWS,
    RAW_CONTEXT_BARS, VOLUME_EPSILON, feature_rows,
)
from adaptive_crypto.core import Candle, H4

ARCHITECTURES = (
    "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
    "grouped_attention_lstm_v1",
)
DISPLAY_NAMES = {
    "lstm_classifier_v1": "LSTM",
    "gru_classifier_v1": "GRU",
    "cnn_lstm_classifier_v1": "CNN-LSTM",
    "grouped_attention_lstm_v1": "Attention-LSTM (OHLCV adaptation)",
}
ASSETS = ("BTC", "ETH", "SOL", "SUI")
CLASSES = ("BUY", "HOLD", "SELL")
LABEL_VERSION = "parente-source-5-2-v1"
ONNX_OPSET = 17


def sha256_path(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False,
                                    allow_nan=False) + "\n", encoding="utf-8")


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def source_labels(closes):
    """Exact adjusted-EMA Parente source 5/2 labels, mapped BUY/HOLD/SELL.

    Mirrors ``neural.labels(..., backward=5, forward=2, convention='source')``.
    The final two targets are -1 (unknown), never HOLD. This pure NumPy form
    keeps the isolated PyTorch environment independent of live TA-Lib/Pandas.
    Its parity with the repository function is checked by a regression test.
    """
    closes = np.asarray(closes, dtype=np.float64)
    if closes.ndim != 1 or not len(closes) or not np.isfinite(closes).all() or np.any(closes <= 0):
        raise ValueError("Labels require finite positive close prices")
    decay = 2.0 / 3.0
    numerator = denominator = 0.0
    baseline = np.empty(len(closes), dtype=np.float64)
    for index, close in enumerate(closes):
        numerator = float(close) + decay * numerator
        denominator = 1.0 + decay * denominator
        baseline[index] = numerator / denominator
    target = np.full(len(closes), -1, dtype=np.int8)
    if len(closes) > 2:
        change = closes[2:] / baseline[:-2] - 1.0
        chosen = (np.abs(change) > .038) & (np.abs(change) < .24 * 1.2)
        target[:-2] = np.where(chosen, np.where(change > 0, 0, 2), 1).astype(np.int8)
    return target


def read_data(data_dir, assets=ASSETS):
    root = Path(data_dir)
    source = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    if source.get("api") != "https://api.kraken.com/0/public/OHLC":
        raise ValueError("Expected official Kraken archive plus verified REST tail provenance")
    result, audit = {}, {}
    for asset in assets:
        info = source["assets"].get(asset)
        if not info or info.get("status") != "retrieved":
            raise ValueError(f"Missing provenance for {asset}")
        path = root / info["merged_csv"]
        digest = sha256_path(path)
        if digest != info["merged_sha256"]:
            raise ValueError(f"Changed Kraken source CSV for {asset}")
        with path.open("r", newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            expected = {"asset", "open_time_ms", "open", "high", "low", "close", "volume"}
            if not expected.issubset(reader.fieldnames or []):
                raise ValueError(f"Bad Kraken CSV header for {asset}")
            candles = []
            for line, row in enumerate(reader, 2):
                if row["asset"] != asset:
                    raise ValueError(f"Wrong asset in {path}:{line}")
                t = int(row["open_time_ms"])
                o, h, l, c, v = (float(row[key]) for key in ("open", "high", "low", "close", "volume"))
                if (t % H4 or not all(math.isfinite(x) for x in (o, h, l, c, v))
                        or min(o, h, l, c) <= 0 or v < 0 or l > min(o, c)
                        or h < max(o, c) or l > h or candles and t <= candles[-1].t):
                    raise ValueError(f"Invalid Kraken candle in {path}:{line}")
                candles.append(Candle(t, o, h, l, c, v, H4))
        if len(candles) < 2 * 365 * 6:
            raise ValueError(f"Fewer than two years of total 4H observations for {asset}")
        segments, start = [], 0
        for index in range(1, len(candles)):
            if candles[index].t != candles[index - 1].t + H4:
                segments.append(candles[start:index])
                start = index
        segments.append(candles[start:])
        result[asset] = segments
        audit[asset] = {
            "source_csv": info["merged_csv"], "dataset_sha256": digest,
            "total_bars": len(candles), "first_open_ms": candles[0].t,
            "last_open_ms": candles[-1].t, "gap_count": len(segments) - 1,
            "largest_contiguous_bars": max(map(len, segments)),
            "segment_lengths_at_least_319": [len(x) for x in segments if len(x) >= HISTORY_BARS],
            "missing_bar_policy": "No interpolation; windows and targets never cross a gap",
        }
    return result, audit, source


def frozen_experiment(segments, audit, source, assets=ASSETS, *, seeds=(11, 23, 37),
                      max_epochs=100, patience=10, batch_size=64,
                      smoke_batches=None):
    # Align all assets on the newest one's full historical span. The last 20%
    # is reserved exactly once as a final test, irrespective of gap placement.
    first = max(min(segment[0].t for segment in segments[a]) for a in assets)
    end = min(max(segment[-1].t for segment in segments[a]) for a in assets) + H4
    bars = (end - first) // H4
    if bars < 365 * 6 * 2:
        raise ValueError("Common Kraken/USD history is shorter than two calendar years")
    val_start = first + int(bars * .70) * H4
    test_start = first + int(bars * .80) * H4
    for asset in assets:
        available = {c.t for segment in segments[asset] for c in segment if test_start <= c.t < end}
        if len(available) != (end - test_start) // H4:
            raise ValueError(f"Final common test interval has missing {asset} candles")
    return {
        "schema": 1,
        "experiment_id": "kraken-usd-4h-ohlcv16-parente52-chronological-v1",
        "frozen_before_training": True,
        "source": "Kraken official historical OHLCVT 2026Q2 archive plus verified committed REST tail",
        "source_provenance_sha256": sha256_path(Path(source["_path"])),
        "dataset_sha256_by_asset": {a: audit[a]["dataset_sha256"] for a in assets},
        "asset_order": list(assets), "quote_currency": "USD", "timeframe_ms": H4,
        "feature_schema": FEATURE_SCHEMA, "label_version": LABEL_VERSION,
        "label_alpha": .038, "label_beta": .24, "label_backward": 5,
        "forward_horizon_bars": 2,
        "aligned_history_start_ms": first, "validation_start_ms": val_start,
        "development_cutoff_ms": test_start - 1,
        "test_start_ms": test_start, "test_end_ms": end,
        "split_policy": "Train sample target ends before validation_start; validation sample opens at/after validation_start and target ends before test_start; test opens at/after test_start, last two unknown targets omitted. Contiguous segments only.",
        "training_recipe": {"optimizer": "Adam", "learning_rate": .001,
                            "batch_size": batch_size, "max_epochs": max_epochs,
                            "gradient_clip_norm": 1.0, "early_stopping_patience": patience,
                            "seeds": list(seeds), "smoke_batches": smoke_batches,
                            "seed_selection": "lowest final-validation cross entropy only",
                            "class_weights": None, "calibration": None},
        "architecture_selection": "Validation only; final test confirms once. No automatic replacement; no demonstrated winner unless evaluator rule passes.",
        "initial_equity": 10000.0, "fee_rate": .004, "slippage_rate": .0005,
        "spread_bps": 10.0, "primary_limitations_enabled": False,
        "data_audit": audit,
    }


@dataclass
class Samples:
    x: np.ndarray
    y: np.ndarray
    signal_ends: np.ndarray
    label_target_open_ms: np.ndarray


def asset_samples(segments):
    tensors, targets, stamps, future = [], [], [], []
    for candles in segments:
        if len(candles) < HISTORY_BARS + 2:
            continue
        rows, row_ends = feature_rows(candles)
        labels = source_labels([bar.c for bar in candles])
        # First feature row maps to candle index 255. A 64-row window ends
        # no earlier than index 318. The target at i+2 must exist in segment.
        from numpy.lib.stride_tricks import sliding_window_view
        windows = sliding_window_view(rows, LOOKBACK_ROWS, axis=0)
        windows = np.transpose(windows, (0, 2, 1))
        starts = np.arange(len(windows))
        end_indices = starts + HISTORY_BARS - 1
        valid = end_indices + 2 < len(candles)
        if not np.any(valid):
            continue
        windows = windows[valid].astype(np.float32, copy=True)
        indices = end_indices[valid]
        tensors.append(windows)
        targets.append(labels[indices])
        stamps.append(np.asarray([candles[i].end for i in indices], dtype=np.int64))
        future.append(np.asarray([candles[i + 2].t for i in indices], dtype=np.int64))
    if not tensors:
        raise ValueError("No complete 319-bar feature and 2-bar target windows")
    return Samples(np.concatenate(tensors), np.concatenate(targets),
                   np.concatenate(stamps), np.concatenate(future))


def split_samples(samples, spec):
    open_times = samples.signal_ends - H4 + 1
    first = spec["aligned_history_start_ms"]
    validation, test = spec["validation_start_ms"], spec["test_start_ms"]
    end = spec["test_end_ms"]
    masks = {
        "train": (open_times >= first) & (open_times < validation)
                 & (samples.label_target_open_ms < validation),
        "validation": (open_times >= validation) & (open_times < test)
                      & (samples.label_target_open_ms < test),
        "test": (open_times >= test) & (open_times < end)
                & (samples.label_target_open_ms < end),
    }
    if any(np.count_nonzero(mask) < 100 for mask in masks.values()):
        raise ValueError("Chronological split has fewer than 100 complete windows")
    return masks


def frozen_scaler(train_x):
    rows = train_x.reshape(-1, train_x.shape[-1]).astype(np.float64)
    mean = rows.mean(axis=0)
    scale = rows.std(axis=0)
    scale = np.where(scale > 1e-8, scale, 1.0)
    return mean, scale


def scale_x(x, mean, scale):
    scaled = ((x.astype(np.float64) - mean) / scale).astype(np.float32)
    if not np.isfinite(scaled).all():
        raise ValueError("Non-finite scaled candidate features")
    return scaled


def model_class(name):
    import torch
    from torch import nn
    from torch.nn import functional as F

    class Recurrent(nn.Module):
        def __init__(self, kind):
            super().__init__()
            self.encoder = (nn.LSTM if kind == "LSTM" else nn.GRU)(16, 64, batch_first=True)
            self.dropout = nn.Dropout(.1)
            self.head = nn.Linear(64, 3)

        def forward(self, features):
            _, state = self.encoder(features)
            last = state[0][-1] if isinstance(state, tuple) else state[-1]
            return self.head(self.dropout(last))

    class CNNLSTM(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv = nn.Conv1d(16, 32, 3)
            self.encoder = nn.LSTM(32, 64, batch_first=True)
            self.dropout = nn.Dropout(.1)
            self.head = nn.Linear(64, 3)

        def forward(self, features):
            # Left-only padding means no future feature row can reach t.
            convolved = F.relu(self.conv(F.pad(features.transpose(1, 2), (2, 0))))
            _, (last, _) = self.encoder(convolved.transpose(1, 2))
            return self.head(self.dropout(last[-1]))

    class GroupedAttention(nn.Module):
        def __init__(self):
            super().__init__()
            self.encoders = nn.ModuleList(nn.LSTM(b - a, 32, batch_first=True)
                                          for a, b in FEATURE_GROUPS)
            self.attention = nn.ModuleList(nn.Sequential(nn.Linear(32, 32),
                                                          nn.Tanh(), nn.Linear(32, 1, bias=False))
                                           for _ in FEATURE_GROUPS)
            self.dense = nn.Linear(96, 32)
            self.dropout = nn.Dropout(.1)
            self.head = nn.Linear(32, 3)

        def forward(self, features):
            summaries = []
            for (a, b), encoder, attention in zip(FEATURE_GROUPS, self.encoders, self.attention):
                hidden, _ = encoder(features[:, :, a:b])
                weights = torch.softmax(attention(hidden), dim=1)
                summaries.append((weights * hidden).sum(dim=1))
            return self.head(self.dropout(F.relu(self.dense(torch.cat(summaries, dim=1)))))

    return {
        "lstm_classifier_v1": lambda: Recurrent("LSTM"),
        "gru_classifier_v1": lambda: Recurrent("GRU"),
        "cnn_lstm_classifier_v1": CNNLSTM,
        "grouped_attention_lstm_v1": GroupedAttention,
    }[name]


def set_seed(seed):
    import torch
    torch.manual_seed(seed)
    np.random.seed(seed)
    torch.set_num_threads(min(4, max(1, torch.get_num_threads())))


def fit_model(architecture, x_train, y_train, x_val, y_val, seed, max_epochs=100,
              patience=10, batch_size=64, smoke_batches=None):
    import torch
    from torch import nn
    set_seed(seed)
    model = model_class(architecture)()
    optimizer = torch.optim.Adam(model.parameters(), lr=.001)
    train_x = torch.from_numpy(x_train)
    train_y = torch.from_numpy(y_train.astype(np.int64))
    val_x = torch.from_numpy(x_val)
    val_y = torch.from_numpy(y_val.astype(np.int64))
    best_loss, best_state, bad = math.inf, None, 0
    history = []
    start = time.perf_counter()
    for epoch in range(max_epochs):
        model.train()
        order = torch.randperm(len(train_x), generator=torch.Generator().manual_seed(seed * 1000 + epoch))
        losses = []
        for batch_number, ids in enumerate(order.split(batch_size)):
            if smoke_batches is not None and batch_number >= smoke_batches:
                break
            optimizer.zero_grad(set_to_none=True)
            logits = model(train_x[ids])
            loss = nn.functional.cross_entropy(logits, train_y[ids])
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach()))
        model.eval()
        with torch.inference_mode():
            validation = [float(nn.functional.cross_entropy(model(val_x[ids]), val_y[ids]))
                          for ids in torch.arange(len(val_x)).split(batch_size)]
        val_loss = float(np.mean(validation))
        history.append({"epoch": epoch + 1, "train_loss": float(np.mean(losses)),
                        "validation_loss": val_loss})
        if val_loss < best_loss - 1e-5:
            best_loss = val_loss
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
            bad = 0
        else:
            bad += 1
        if bad >= patience:
            break
    model.load_state_dict(best_state)
    model.eval()
    return model, {"seed": seed, "epochs": len(history), "best_validation_loss": best_loss,
                   "seconds": time.perf_counter() - start, "history": history,
                   "parameters": sum(p.numel() for p in model.parameters())}


def predict_native(model, x, batch_size=256):
    import torch
    model.eval()
    outputs = []
    with torch.inference_mode():
        for offset in range(0, len(x), batch_size):
            outputs.append(model(torch.from_numpy(x[offset:offset + batch_size])).numpy())
    return np.concatenate(outputs)


def export_onnx(model, path, parity_inputs):
    import onnx
    import onnxruntime as ort
    import torch
    path = Path(path)
    with torch.inference_mode():
        torch.onnx.export(model, (torch.from_numpy(parity_inputs[:1]),), str(path),
                          input_names=["features"], output_names=["logits"],
                          opset_version=ONNX_OPSET, dynamo=False,
                          do_constant_folding=True)
    onnx.checker.check_model(str(path))
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(path.read_bytes(), sess_options=options,
                                   providers=["CPUExecutionProvider"])
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if (len(inputs) != 1 or inputs[0].name != "features"
            or list(inputs[0].shape) != [1, 64, 16]
            or len(outputs) != 1 or outputs[0].name != "logits"
            or list(outputs[0].shape) != [1, 3]):
        raise ValueError("Export tensor contract mismatch")
    native = predict_native(model, parity_inputs[:min(8, len(parity_inputs))], batch_size=1)
    exported = np.concatenate([session.run(["logits"], {"features": x[None]})[0]
                               for x in parity_inputs[:min(8, len(parity_inputs))]])
    max_abs = float(np.max(np.abs(native - exported)))
    if not np.isfinite(exported).all() or max_abs > 1e-4:
        raise ValueError(f"Native/ONNX logits parity failed: {max_abs}")
    trials = parity_inputs[:min(32, len(parity_inputs))]
    started = time.perf_counter()
    for row in trials:
        session.run(["logits"], {"features": row[None]})
    latency = (time.perf_counter() - started) / len(trials) * 1000
    return {"max_abs_logits_difference": max_abs, "tolerance": 1e-4,
            "inference_ms_per_asset_cpu_warm": latency,
            "onnx_bytes": path.stat().st_size, "onnx_sha256": sha256_path(path)}


def softmax(logits):
    centered = logits.astype(np.float64) - logits.max(axis=1, keepdims=True)
    exp = np.exp(centered)
    p = exp / exp.sum(axis=1, keepdims=True)
    if not np.isfinite(p).all() or not np.allclose(p.sum(axis=1), 1, atol=1e-6):
        raise ValueError("Invalid candidate probabilities")
    return p


def class_counts(y):
    return dict(zip(CLASSES, [int(np.count_nonzero(y == i)) for i in range(3)]))


def compact_evaluation(evaluation):
    """Keep decision-relevant evidence in the deployable JSON report.

    The full replay's individual equity marks and trades remain in the
    experiment directory, covered by their SHA256 below. Bundles intentionally
    omit thousands of raw marks and fill details from the public Settings API.
    """
    bulky = {"paired_equity_4h", "trades", "execution"}

    def summarize_replay(record):
        return {key: value for key, value in record.items() if key not in bulky}

    return {
        "status": evaluation["status"], "reason": evaluation["reason"],
        "label_verification": evaluation.get("label_verification"),
        "classification": evaluation.get("classification"),
        "replay": {name: summarize_replay(value)
                   for name, value in evaluation.get("replay", {}).items()},
        "per_asset_replay": {asset: {name: summarize_replay(value) for name, value in cases.items()}
                             for asset, cases in evaluation.get("per_asset_replay", {}).items()},
        "baselines": evaluation.get("baselines"),
        "coverage_4h": evaluation.get("coverage_4h"),
        "prediction_coverage": evaluation.get("prediction_coverage"),
        "mark_cadence_ms": evaluation.get("mark_cadence_ms"),
        "stop_timing_limitation": evaluation.get("stop_timing_limitation"),
        "quote_proxy": evaluation.get("quote_proxy"),
        "recommendation": evaluation.get("recommendation"),
    }


def train_architecture(architecture, segments, audit, spec, output, *, seeds,
                       max_epochs=100, patience=10, batch_size=64, smoke_batches=None):
    import onnx
    import onnxruntime as ort
    import torch
    output = Path(output)
    report = {"schema": 1, "architecture_id": architecture,
              "display_name": DISPLAY_NAMES[architecture],
              "technical_status": "training", "recommendation": "no demonstrated winner",
              "development_cutoff_ms": spec["development_cutoff_ms"],
              "promotion_evidence_through_ms": spec["test_end_ms"] - 1,
              "experiment_sha256": sha256_path(output / "experiment.json"),
              "data_audit": audit, "per_asset": {}, "validation_seed_dispersion": {},
              "fair_comparison_note": "All architectures share features, targets, chronological boundaries and evaluator; no hyperparameter search.",
              "attention_note": "OHLCV classifier adaptation; not the original on-chain price-regression SAM-LSTM" if architecture == "grouped_attention_lstm_v1" else None}
    bundle_work = output / "bundle_work" / architecture
    bundle_work.mkdir(parents=True, exist_ok=True)
    prediction_rows = []
    members = {}
    for asset in spec["asset_order"]:
        samples = asset_samples(segments[asset])
        masks = split_samples(samples, spec)
        mean, scale = frozen_scaler(samples.x[masks["train"]])
        x = {name: scale_x(samples.x[mask], mean, scale) for name, mask in masks.items()}
        y = {name: samples.y[mask] for name, mask in masks.items()}
        seed_runs = []
        chosen = None
        for seed in seeds:
            model, train_report = fit_model(architecture, x["train"], y["train"],
                                            x["validation"], y["validation"], seed,
                                            max_epochs=max_epochs, patience=patience,
                                            batch_size=batch_size, smoke_batches=smoke_batches)
            train_report["validation_class_counts"] = class_counts(y["validation"])
            seed_runs.append(train_report)
            print(f"{architecture} {asset} seed={seed} epochs={train_report['epochs']} "
                  f"val_loss={train_report['best_validation_loss']:.4f} "
                  f"seconds={train_report['seconds']:.1f}", flush=True)
            if chosen is None or train_report["best_validation_loss"] < chosen[0]:
                chosen = train_report["best_validation_loss"], seed, model
        selected_loss, selected_seed, model = chosen
        onnx_path = bundle_work / f"{asset}.onnx"
        parity = export_onnx(model, onnx_path, x["test"])
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        session = ort.InferenceSession(onnx_path.read_bytes(), sess_options=options,
                                       providers=["CPUExecutionProvider"])
        logits = np.concatenate([session.run(["logits"], {"features": row[None]})[0]
                                 for row in x["test"]])
        probabilities = softmax(logits)
        for signal_end, truth, probs in zip(samples.signal_ends[masks["test"]], y["test"], probabilities):
            prediction_rows.append({"model_id": architecture, "artifact_id": "pending",
                                    "asset": asset, "signal_end_ms": int(signal_end),
                                    "probabilities": [float(value) for value in probs],
                                    "truth_label_index": int(truth)})
        per_asset = {"sample_counts": {part: int(np.count_nonzero(mask)) for part, mask in masks.items()},
                     "class_counts": {part: class_counts(y[part]) for part in masks},
                     "selected_seed": selected_seed, "selected_validation_log_loss": selected_loss,
                     "seed_runs": seed_runs, "export": parity,
                     "scaler_fit": "Only train windows whose two-bar target ends before validation_start; overlapping raw feature rows counted by training-window frequency",
                     "history_bars": HISTORY_BARS,
                     "test_prediction_count": len(probabilities),
                     "test_open_start_ms": int(samples.signal_ends[masks["test"]][0] - H4 + 1),
                     "test_open_end_ms": int(samples.signal_ends[masks["test"]][-1] - H4 + 1)}
        report["per_asset"][asset] = per_asset
        members[asset] = {"onnx": onnx_path.name, "sha256": parity["onnx_sha256"],
                          "mean": mean.tolist(), "scale": scale.tolist(),
                          "trained_through_ms": spec["validation_start_ms"] - 1,
                          "dataset_sha256": audit[asset]["dataset_sha256"],
                          "training_seed": selected_seed, "training_samples": per_asset["sample_counts"]["train"]}
    report["technical_status"] = ("smoke_export_parity_passed" if smoke_batches is not None
                                   else "trained_export_parity_passed")
    report["software_versions"] = {"python": platform.python_version(),
                                   "torch": torch.__version__, "onnx": onnx.__version__,
                                   "onnxruntime": ort.__version__, "numpy": np.__version__}
    report["runtime_warm_inference_ms_all_assets"] = sum(
        report["per_asset"][a]["export"]["inference_ms_per_asset_cpu_warm"]
        for a in spec["asset_order"])
    report["limits"] = ["Training archive includes missing 4H bars; no windows cross gaps",
                        "OHLCV features only; no historical sentiment or on-chain observations",
                        "Technical export parity does not establish profitable trading"]
    report_path = bundle_work / "report.json"
    manifest = {
        "schema": 1, "id": architecture, "display_name": DISPLAY_NAMES[architecture],
        "feature_schema": FEATURE_SCHEMA, "feature_names": list(FEATURE_NAMES),
        "feature_groups": [{"start": a, "end": b} for a, b in FEATURE_GROUPS],
        "classes": list(CLASSES), "input_shape": [1, 64, 16],
        "output_shape": [1, 3], "timeframe_ms": H4,
        "history_bars": HISTORY_BARS, "raw_feature_context_bars": RAW_CONTEXT_BARS,
        "feature_sequence_rows": LOOKBACK_ROWS, "volume_units": "Kraken base asset units",
        "volume_epsilon": VOLUME_EPSILON, "missing_data_policy": "unavailable; no fill or interpolation",
        "target_horizon_bars": 2, "label_version": LABEL_VERSION,
        # The untouched test was used to validate promotion, so live
        # eligibility begins after the *entire* evaluated interval.
        "latest_model_selection_through_ms": spec["test_end_ms"] - 1,
        "development_cutoff_ms": spec["development_cutoff_ms"],
        "inference_runtime": {"name": "onnxruntime", "version_validated": ort.__version__,
                              "execution_provider": "CPUExecutionProvider", "opset": ONNX_OPSET},
        "training_dependencies": report["software_versions"],
        "training_hyperparameters": {**spec["training_recipe"], "seeds": list(seeds),
                                     "actual_max_epochs": max_epochs,
                                     "actual_patience": patience,
                                     "actual_batch_size": batch_size,
                                     "smoke_batches": smoke_batches},
        "dataset_provenance_sha256": spec["source_provenance_sha256"],
        "experiment_sha256": sha256_path(output / "experiment.json"),
        "splits": {k: spec[k] for k in ("aligned_history_start_ms", "validation_start_ms",
                                         "test_start_ms", "test_end_ms")},
        "members": members,
        "decision_policy": {"probability_mapping": "softmax logits, BUY/HOLD/SELL order",
                            "tie_break": "first class by argmax", "class_weights": None,
                            "probability_calibration": None},
    }
    # The immutable identity excludes the evaluation report and its hash so
    # the once-frozen held-out prediction file can name the final artifact.
    # Report fields do not affect preprocessing, weights or decision policy.
    manifest["artifact_id"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    identity = manifest["artifact_id"][:12]
    final = ((output / "smoke_artifacts") if smoke_batches is not None
             else Path("adaptive_crypto/models/candidates")) / architecture / identity
    if final.exists():
        raise FileExistsError(f"Refusing to replace immutable trained artifact {final}")
    frozen_predictions = output / f"{architecture}.{identity}.predictions.jsonl"
    with frozen_predictions.open("w", encoding="utf-8") as stream:
        for row in prediction_rows:
            row["model_id"] = f"{architecture}@{identity}"
            row["artifact_id"] = manifest["artifact_id"]
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    if smoke_batches is None:
        import subprocess
        import sys
        evaluation_path = output / f"{architecture}.{identity}.evaluation.json"
        command = [sys.executable, "-m", "tools.evaluate_nn_candidates",
                   "--predictions", str(frozen_predictions),
                   "--candles-4h", str(output / "combined_candles_4h.csv"),
                   "--experiment", str(output / "experiment.json"),
                   "--output", str(evaluation_path), "--verify-source-labels"]
        subprocess.run(command, check=True)
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        report["evaluation"] = compact_evaluation(evaluation)
        report["full_evaluation_report_sha256"] = sha256_path(evaluation_path)
        report["full_evaluation_report_file"] = evaluation_path.name
        report["validation_seed_dispersion"] = {
            asset: {"seed_count": len(info["seed_runs"]),
                    "validation_log_loss_min": min(run["best_validation_loss"]
                                                   for run in info["seed_runs"]),
                    "validation_log_loss_median": float(np.median(
                        [run["best_validation_loss"] for run in info["seed_runs"]])),
                    "validation_log_loss_max": max(run["best_validation_loss"]
                                                   for run in info["seed_runs"])}
            for asset, info in report["per_asset"].items()}
        if evaluation.get("status") != "complete":
            raise ValueError(f"Held-out evaluator incomplete for {architecture}: {evaluation.get('reason')}")
    write_json(report_path, report)
    manifest["report"] = "report.json"
    manifest["report_sha256"] = sha256_path(report_path)
    write_json(bundle_work / "manifest.json", manifest)
    final.parent.mkdir(parents=True, exist_ok=True)
    bundle_work.rename(final)
    print(json.dumps({"architecture": architecture, "artifact_id": manifest["artifact_id"],
                      "bundle": str(final), "predictions": str(frozen_predictions),
                      "per_asset": {a: report["per_asset"][a]["sample_counts"]
                                    for a in spec["asset_order"]}}, indent=2), flush=True)
    return final, frozen_predictions


def write_combined_candles(segments, spec, output):
    path = Path(output) / "combined_candles_4h.csv"
    def emit(stream):
        writer = csv.writer(stream)
        writer.writerow(("asset", "open_time_ms", "open", "high", "low", "close", "volume"))
        for asset in spec["asset_order"]:
            for segment in segments[asset]:
                for c in segment:
                    writer.writerow((asset, c.t, c.o, c.h, c.l, c.c, c.v))

    if path.exists():
        # The evaluator reads this derived file.  On resume, verifying only
        # the source CSV hashes is insufficient if the derived file changed.
        # Regenerate its exact CSV bytes into a digest, then reject drift.
        expected = hashlib.sha256()

        class HashSink:
            def write(self, value):
                encoded = value.encode("utf-8")
                expected.update(encoded)
                return len(value)

        emit(HashSink())
        if sha256_path(path) != expected.hexdigest():
            raise ValueError("Derived combined 4H candles differ from verified source CSVs")
        return path
    with path.open("w", newline="", encoding="utf-8") as stream:
        emit(stream)
    return path


def train_command(args):
    data = Path(args.data)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    segments, audit, source = read_data(data, args.assets)
    source["_path"] = str(data / "provenance.json")
    expected = frozen_experiment(segments, audit, source, args.assets,
                                 seeds=args.seeds, max_epochs=args.max_epochs,
                                 patience=args.patience, batch_size=args.batch_size,
                                 smoke_batches=args.smoke_batches)
    experiment_path = output / "experiment.json"
    if experiment_path.exists():
        saved = json.loads(experiment_path.read_text(encoding="utf-8"))
        if saved != expected:
            raise ValueError("Frozen experiment differs from current data/recipe; choose another output directory")
    else:
        write_json(experiment_path, expected)
    write_combined_candles(segments, expected, output)
    print(f"Frozen test: {utc(expected['test_start_ms'])} through "
          f"{utc(expected['test_end_ms'])}, 4 assets; source hashes verified", flush=True)
    for architecture in args.architectures:
        train_architecture(architecture, segments, audit, expected, output,
                           seeds=args.seeds, max_epochs=args.max_epochs,
                           patience=args.patience, batch_size=args.batch_size,
                           smoke_batches=args.smoke_batches)


def compact_existing_reports(output, architectures=ARCHITECTURES):
    """Reduce already-trained bundles while preserving their frozen identity.

    This is needed for runs started before bundled reports excluded raw replay
    curves. The full replay remains available by hash in the experiment area.
    It never modifies weights, scaler, target contract, or decision policy.
    """
    output = Path(output)
    for architecture in architectures:
        predictions = list(output.glob(f"{architecture}.*.predictions.jsonl"))
        if len(predictions) != 1:
            raise ValueError(f"Expected one frozen prediction file for {architecture}")
        prefix = predictions[0].name.split(".")[1]
        bundle = Path("adaptive_crypto/models/candidates") / architecture / prefix
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_identity = hashlib.sha256(canonical_bytes({
            k: v for k, v in manifest.items()
            if k not in {"artifact_id", "report", "report_sha256"}
        })).hexdigest()
        if manifest["artifact_id"] != expected_identity:
            raise ValueError(f"Identity contract differs for {architecture}")
        report_path = bundle / manifest["report"]
        if sha256_path(report_path) != manifest["report_sha256"]:
            raise ValueError(f"Cannot compact changed report for {architecture}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        original_evaluation_path = output / f"{architecture}.{prefix}.evaluation.json"
        corrected_path = output / f"{architecture}.{prefix}.v2.evaluation.json"
        evaluation_path = corrected_path if corrected_path.exists() else original_evaluation_path
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        already_compact = "model_id" not in report["evaluation"]
        if (not already_compact and (report["evaluation"]["model_id"] != evaluation["model_id"]
                                     or report["evaluation"]["artifact_id"] != evaluation["artifact_id"])
                or already_compact and report.get("full_evaluation_report_sha256") not in {
                    sha256_path(original_evaluation_path), sha256_path(evaluation_path)}
                or evaluation["artifact_id"] != manifest["artifact_id"]
                or evaluation["model_id"] != f"{architecture}@{prefix}"
                or evaluation["status"] != "complete"
                or evaluation.get("input_sha256") != {
                    "candles_4h": sha256_path(output / "combined_candles_4h.csv"),
                    "experiment": sha256_path(output / "experiment.json"),
                    "predictions": sha256_path(predictions[0]),
                }):
            raise ValueError(f"Cannot reconcile evaluator output for {architecture}")
        report["evaluation"] = compact_evaluation(evaluation)
        report["full_evaluation_report_sha256"] = sha256_path(evaluation_path)
        report["full_evaluation_report_file"] = evaluation_path.name
        if evaluation_path == corrected_path:
            report["evaluation_revision"] = "v2: entry-open bar stop proxy and spread-corrected bid stop fill"
        report["validation_seed_dispersion"] = {
            asset: {"seed_count": len(info["seed_runs"]),
                    "validation_log_loss_min": min(run["best_validation_loss"]
                                                   for run in info["seed_runs"]),
                    "validation_log_loss_median": float(np.median(
                        [run["best_validation_loss"] for run in info["seed_runs"]])),
                    "validation_log_loss_max": max(run["best_validation_loss"]
                                                   for run in info["seed_runs"])}
            for asset, info in report["per_asset"].items()}
        new_bytes = json.dumps(report, indent=2, ensure_ascii=False,
                               allow_nan=False).encode("utf-8") + b"\n"
        if len(new_bytes) > 5_000_000:
            raise ValueError(f"Compact report still exceeds registry limit for {architecture}")
        report_path.write_bytes(new_bytes)
        manifest["report_sha256"] = sha256_path(report_path)
        write_json(manifest_path, manifest)
        print(f"Compacted {architecture} report: {len(new_bytes):,} bytes; "
              f"artifact identity {prefix} unchanged", flush=True)


def fold_plan(spec, *, seed=11):
    """Two expanding outer folds with a disjoint inner early-stopping slice.

    The original draft fold plan used the outer validation slice for early
    stopping.  Its first four fits were discarded before a report was written;
    this amended plan records the correction rather than hiding that history.
    Final deployed weights and the held-out test report are unaffected.
    """
    first, test = spec["aligned_history_start_ms"], spec["test_end_ms"]
    bars = (test - first) // H4
    edges = {fraction: first + int(bars * fraction) * H4
             for fraction in (.50, .60, .70)}
    inner = {fraction: first + int(bars * fraction * .9) * H4
             for fraction in (.50, .60)}
    return {"schema": 2, "base_experiment_id": spec["experiment_id"],
            "base_experiment_sha256": None, "created_before_fold_training": True,
            "amendment": "Original schema-1 draft reused outer validation for early stopping. Four preliminary fits were discarded without writing a fold report. Schema 2 uses disjoint inner early-stopping ranges; finalized candidate weights and final-test evaluations are unchanged.",
            "seed": seed, "max_epochs": 100, "early_stopping_patience": 10,
            "batch_size": 64, "optimizer": "Adam", "learning_rate": .001,
            "selection_use": "Outer-fold descriptive validation dispersion only; no held-out test samples or model reselection",
            "folds": [
                {"name": "expanding_50_60", "train_start_ms": first,
                 "fit_target_before_ms": inner[.50],
                 "early_stopping_start_ms": inner[.50],
                 "early_stopping_end_ms": edges[.50],
                 "validation_start_ms": edges[.50], "validation_end_ms": edges[.60]},
                {"name": "expanding_60_70", "train_start_ms": first,
                 "fit_target_before_ms": inner[.60],
                 "early_stopping_start_ms": inner[.60],
                 "early_stopping_end_ms": edges[.60],
                 "validation_start_ms": edges[.60], "validation_end_ms": edges[.70]},
            ]}


def train_folds_command(output, data):
    """Run independent expanding-fold checks without touching final-test data."""
    from tools.evaluate_nn_candidates import classification_metrics

    output = Path(output)
    spec = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
    segments, audit, source = read_data(data, tuple(spec["asset_order"]))
    if ({a: audit[a]["dataset_sha256"] for a in spec["asset_order"]}
            != spec["dataset_sha256_by_asset"]):
        raise ValueError("Fold data differs from frozen final experiment")
    plan = fold_plan(spec)
    plan["base_experiment_sha256"] = sha256_path(output / "experiment.json")
    # Keep the aborted schema-1 draft on disk as an audit trail.
    plan_path = output / "fold_plan_v2.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise ValueError("Fold plan differs from existing preregistration")
    else:
        write_json(plan_path, plan)
    cached = {asset: asset_samples(segments[asset]) for asset in spec["asset_order"]}
    report = {"schema": 2, "fold_plan_sha256": sha256_path(plan_path),
              "note": "One fixed seed per expanding outer fold; an inner disjoint slice controls early stopping. Final model selection used a separate 70-80% validation with three seeds.",
              "architectures": {}}
    for architecture in ARCHITECTURES:
        architecture_result = {}
        for asset, samples in cached.items():
            fold_results = []
            opens = samples.signal_ends - H4 + 1
            for fold in plan["folds"]:
                train = (opens >= fold["train_start_ms"]) & (opens < fold["fit_target_before_ms"])
                train &= samples.label_target_open_ms < fold["fit_target_before_ms"]
                early_stopping = (opens >= fold["early_stopping_start_ms"]) & (opens < fold["early_stopping_end_ms"])
                early_stopping &= samples.label_target_open_ms < fold["early_stopping_end_ms"]
                validation = (opens >= fold["validation_start_ms"]) & (opens < fold["validation_end_ms"])
                validation &= samples.label_target_open_ms < fold["validation_end_ms"]
                if min(np.count_nonzero(train), np.count_nonzero(early_stopping),
                       np.count_nonzero(validation)) < 100:
                    raise ValueError(f"Insufficient purged fold windows: {architecture} {asset} {fold['name']}")
                mean, scale = frozen_scaler(samples.x[train])
                train_x = scale_x(samples.x[train], mean, scale)
                stopping_x = scale_x(samples.x[early_stopping], mean, scale)
                val_x = scale_x(samples.x[validation], mean, scale)
                model, history = fit_model(architecture, train_x, samples.y[train],
                                           stopping_x, samples.y[early_stopping], plan["seed"],
                                           max_epochs=plan["max_epochs"],
                                           patience=plan["early_stopping_patience"],
                                           batch_size=plan["batch_size"])
                p = softmax(predict_native(model, val_x))
                rows = [{"truth_label_index": int(truth),
                         "label_index": int(np.argmax(probs)),
                         "probabilities": tuple(map(float, probs))}
                        for truth, probs in zip(samples.y[validation], p)]
                fold_results.append({"name": fold["name"],
                                     "train_samples": int(np.count_nonzero(train)),
                                     "early_stopping_samples": int(np.count_nonzero(early_stopping)),
                                     "validation_samples": int(np.count_nonzero(validation)),
                                     "train_class_counts": class_counts(samples.y[train]),
                                     "early_stopping_class_counts": class_counts(samples.y[early_stopping]),
                                     "validation_class_counts": class_counts(samples.y[validation]),
                                     "training": history,
                                     "validation_classification": classification_metrics(rows)})
                print(f"fold {architecture} {asset} {fold['name']} "
                      f"n={len(rows)} loss={history['best_validation_loss']:.4f} "
                      f"seconds={history['seconds']:.1f}", flush=True)
            architecture_result[asset] = fold_results
        report["architectures"][architecture] = architecture_result
    report_path = output / "validation_folds.json"
    write_json(report_path, report)
    print(f"Frozen expanding-fold evidence: {report_path}", flush=True)
    return report_path


def attach_fold_summaries(output):
    """Attach compact fold dispersion to the already-trained report hashes."""
    output = Path(output)
    fold_path = output / "validation_folds.json"
    folds = json.loads(fold_path.read_text(encoding="utf-8"))
    if folds["schema"] != 2 or len(folds["architectures"]) != 4:
        raise ValueError("Incomplete fold report")
    for architecture in ARCHITECTURES:
        predictions = list(output.glob(f"{architecture}.*.predictions.jsonl"))
        if len(predictions) != 1:
            raise ValueError(f"Expected one frozen prediction file for {architecture}")
        prefix = predictions[0].name.split(".")[1]
        bundle = Path("adaptive_crypto/models/candidates") / architecture / prefix
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report_path = bundle / manifest["report"]
        if sha256_path(report_path) != manifest["report_sha256"]:
            raise ValueError(f"Changed candidate report: {architecture}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        fold_assets = folds["architectures"][architecture]
        if set(fold_assets) != set(manifest["members"]):
            raise ValueError("Fold coverage differs from member coverage")
        report["validation_folds"] = {
            asset: [{"name": fold["name"], "train_samples": fold["train_samples"],
                     "early_stopping_samples": fold["early_stopping_samples"],
                     "validation_samples": fold["validation_samples"],
                     "validation_classification": fold["validation_classification"],
                     "best_validation_loss": fold["training"]["best_validation_loss"]}
                    for fold in entries]
            for asset, entries in fold_assets.items()}
        report["full_fold_report_sha256"] = sha256_path(fold_path)
        report["full_fold_report_file"] = fold_path.name
        write_json(report_path, report)
        if report_path.stat().st_size > 5_000_000:
            raise ValueError("Bundled report exceeds registry size limit")
        manifest["report_sha256"] = sha256_path(report_path)
        write_json(manifest_path, manifest)
        print(f"Attached two expanding folds to {architecture}@{prefix}", flush=True)


def verify_runtime_predictions(output, architectures=ARCHITECTURES):
    """Compare frozen backtest ONNX probabilities with the live 319-bar adapter.

    This validates the exported CPU model, feature window, per-asset scaler,
    class order, and manifest-selected member together without paper execution.
    """
    from adaptive_crypto.neural_models import CandidateModel

    output = Path(output)
    by_asset = {}
    with (output / "combined_candles_4h.csv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            asset = row["asset"]
            candle = Candle(int(row["open_time_ms"]),
                            *(float(row[key]) for key in ("open", "high", "low", "close", "volume")), H4)
            by_asset.setdefault(asset, {})[candle.t] = candle
    results = {}
    for architecture in architectures:
        paths = list(output.glob(f"{architecture}.*.predictions.jsonl"))
        if len(paths) != 1:
            raise ValueError(f"Expected one frozen prediction file for {architecture}")
        prefix = paths[0].name.split(".")[1]
        model_id = f"{architecture}@{prefix}"
        model = CandidateModel(model_id,
                               Path("adaptive_crypto/models/candidates") / architecture / prefix)
        rows = {asset: [] for asset in model.members}
        for line in paths[0].read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            rows[item["asset"]].append(item)
        max_delta = 0.0
        count = 0
        for asset, records in rows.items():
            if len(records) < 3:
                raise ValueError("Insufficient frozen predictions")
            for item in (records[0], records[len(records) // 2], records[-1]):
                end_open = item["signal_end_ms"] - H4 + 1
                candles = [by_asset[asset].get(end_open - H4 * back)
                           for back in reversed(range(HISTORY_BARS))]
                if any(candle is None for candle in candles):
                    raise ValueError(f"Missing runtime verification history for {asset}")
                native = model.predict(candles, asset)
                if native["signal_end"] != item["signal_end_ms"]:
                    raise ValueError("Runtime signal timestamp differs from frozen evaluation")
                values = [native["probabilities"][name] for name in CLASSES]
                delta = float(np.max(np.abs(np.asarray(values) - item["probabilities"])))
                max_delta = max(max_delta, delta)
                if delta > 1e-5:
                    raise ValueError(f"Live/export prediction divergence for {model_id} {asset}: {delta}")
                count += 1
        results[model_id] = {"checked": count, "max_probability_difference": max_delta,
                             "tolerance": 1e-5}
        print(f"Verified {model_id}: {count} live/frozen signals, max difference {max_delta:.3g}", flush=True)
    write_json(output / "live_export_parity.json", results)
    return results

