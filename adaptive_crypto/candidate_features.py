"""Causal, fixed-context Kraken/USD features for sequence NN candidates.

The last 64 feature rows need 319 contiguous completed 4H candles. Each row
restarts RSI from its own trailing 256 raw candles; batch and live inference
therefore use exactly the same initialization at any history length.
"""
from __future__ import annotations

import math

import numpy as np

from .core import DataError, H4

FEATURE_SCHEMA = "kraken-ohlcv-16-v1"
FEATURE_NAMES = (
    "log_close_return_1", "log_open_prev_close", "log_high_prev_close",
    "log_low_prev_close", "log_close_open", "range_over_close",
    "log_close_return_3", "log_close_return_6", "log_close_return_12",
    "log_close_return_24", "log_close_sma24", "rsi14_centered",
    "log_return_std24", "log1p_volume", "log_volume_sma24_ratio",
    "log1p_volume_change",
)
FEATURE_GROUPS = ((0, 6), (6, 13), (13, 16))
RAW_CONTEXT_BARS = 256
LOOKBACK_ROWS = 64
HISTORY_BARS = RAW_CONTEXT_BARS + LOOKBACK_ROWS - 1
VOLUME_EPSILON = 1e-12


def _arrays(candles):
    """Validate ordered, contiguous raw candles and return float64 columns."""
    if len(candles) < HISTORY_BARS:
        raise DataError(f"Candidate model requires {HISTORY_BARS} contiguous 4H candles")
    values = np.asarray([[b.t, b.o, b.h, b.l, b.c, b.v] for b in candles], dtype=np.float64)
    stamps, o, h, l, c, v = values.T
    if (not np.isfinite(values).all() or np.any(np.diff(stamps) != H4)
            or np.any(stamps % H4 != 0) or np.any(o <= 0) or np.any(h <= 0)
            or np.any(l <= 0) or np.any(c <= 0) or np.any(v < 0)
            or np.any(h < np.maximum(o, c)) or np.any(l > np.minimum(o, c))
            or np.any(l > h)):
        raise DataError("Candidate candles have a gap, duplicate, bad 4H boundary or invalid OHLCV")
    return stamps.astype(np.int64), o, h, l, c, v


def _rsi_last(close):
    """Wilder RSI14, seeded from the first 14 changes of this 256-bar context."""
    diffs = np.diff(close)
    gains = np.maximum(diffs, 0.0)
    losses = np.maximum(-diffs, 0.0)
    avg_gain = float(np.mean(gains[:14]))
    avg_loss = float(np.mean(losses[:14]))
    for gain, loss in zip(gains[14:], losses[14:]):
        avg_gain = (avg_gain * 13.0 + float(gain)) / 14.0
        avg_loss = (avg_loss * 13.0 + float(loss)) / 14.0
    if avg_loss == 0:
        return 0.5 if avg_gain == 0 else 1.0
    return 1.0 - 1.0 / (1.0 + avg_gain / avg_loss)


def _row(o, h, l, c, v, end):
    first = end - RAW_CONTEXT_BARS + 1
    close_window = c[first:end + 1]
    vol_window = v[first:end + 1]
    p = c[end - 1]
    with np.errstate(divide="raise", invalid="raise"):
        row = np.array((
            math.log(c[end] / p), math.log(o[end] / p),
            math.log(h[end] / p), math.log(l[end] / p),
            math.log(c[end] / o[end]), (h[end] - l[end]) / c[end],
            *(math.log(c[end] / c[end - n]) for n in (3, 6, 12, 24)),
            math.log(c[end] / np.mean(c[end - 23:end + 1])),
            _rsi_last(close_window) - 0.5,
            np.std(np.diff(np.log(c[end - 24:end + 1])), ddof=1),
            math.log1p(v[end]),
            math.log((v[end] + VOLUME_EPSILON) /
                     (np.mean(v[end - 23:end + 1]) + VOLUME_EPSILON)),
            math.log1p(v[end]) - math.log1p(v[end - 1]),
        ), dtype=np.float64)
    if row.shape != (16,) or not np.isfinite(row).all():
        raise DataError("Non-finite candidate feature")
    return row


def feature_rows(candles):
    """Return rows [256th bar..last bar] and their signal-end timestamps.

    Call separately for each contiguous history segment. The same function is
    used in offline training and by the deployed candidate adapter.
    """
    stamps, o, h, l, c, v = _arrays(candles)
    rows = np.stack([_row(o, h, l, c, v, end)
                     for end in range(RAW_CONTEXT_BARS - 1, len(candles))])
    return rows, stamps[RAW_CONTEXT_BARS - 1:] + H4 - 1


def sequence(candles, mean=None, scale=None):
    """Latest raw or frozen train-scaled [64,16] tensor, float32."""
    rows, _ = feature_rows(candles[-HISTORY_BARS:])
    rows = rows[-LOOKBACK_ROWS:]
    if mean is not None or scale is not None:
        mean, scale = np.asarray(mean, dtype=np.float64), np.asarray(scale, dtype=np.float64)
        if (mean.shape != (16,) or scale.shape != (16,)
                or not np.isfinite(mean).all() or not np.isfinite(scale).all()
                or np.any(scale <= 0)):
            raise DataError("Invalid candidate frozen scaler")
        rows = (rows - mean) / scale
    result = rows.astype(np.float32)
    if result.shape != (LOOKBACK_ROWS, 16) or not np.isfinite(result).all():
        raise DataError("Invalid candidate sequence")
    return result


def feature_sequence(candles):
    """Unscaled latest [64,16] tensor for frozen-model adapters."""
    return sequence(candles)
