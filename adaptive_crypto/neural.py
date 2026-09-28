"""Parente et al. (2024) classifier: fixed historical scaling, causal inference.

See docs/NEURAL_STRATEGY.md for the paper/source differences and model provenance.
Numerical dependencies are optional until this strategy is selected.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .core import DataError, H4, validate_candles

FEATURE_VERSION = "parente-source-36-frozen-volume-v1"
MODEL_VERSION = "parente-mlp-v1"
DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "parente_5_2.npz"
PATTERNS = (
    "CDL2CROWS", "CDL3BLACKCROWS", "CDL3WHITESOLDIERS", "CDLABANDONEDBABY",
    "CDLBELTHOLD", "CDLCOUNTERATTACK", "CDLDARKCLOUDCOVER", "CDLDRAGONFLYDOJI",
    "CDLENGULFING", "CDLEVENINGDOJISTAR", "CDLEVENINGSTAR", "CDLGRAVESTONEDOJI",
    "CDLHANGINGMAN", "CDLHARAMICROSS", "CDLINVERTEDHAMMER", "CDLMARUBOZU",
    "CDLMORNINGDOJISTAR", "CDLMORNINGSTAR", "CDLPIERCING", "CDLRISEFALL3METHODS",
    "CDLSHOOTINGSTAR", "CDLSPINNINGTOP", "CDLUPSIDEGAP2CROWS",
)
FEATURES = ("Z_score", "RSI", "boll", "ULTOSC", "pct_change", "zsVol",
            "PR_MA_Ratio_short", "MA_Ratio_short", "MA_Ratio", "PR_MA_Ratio",
            *PATTERNS, "DayOfWeek", "Month", "Hourly")
CLASSES = ("BUY", "HOLD", "SELL")


def dependencies():
    try:
        import numpy as np
        import pandas as pd
        import talib
    except ImportError as exc:
        raise DataError("Neural strategy requires: python -m pip install -r requirements-neural.txt") from exc
    return np, pd, talib


def feature_frame(candles, volume_stats):
    """All features at t use candles through t and a frozen calibration pair."""
    np, pd, ta = dependencies()
    if not candles:
        raise DataError("Neural features require completed 4H candles")
    validate_candles(candles, H4, candles[-1].end+1, minimum=100, fresh=False)
    mean, std = volume_stats
    if not np.isfinite([mean, std]).all() or mean < 0 or std <= 0:
        raise DataError("Invalid frozen volume calibration")
    o, h, l, c, v = (np.array([getattr(b, key) for b in candles], dtype=float) for key in "ohlcv")
    close = pd.Series(c)
    returns = np.log(close).diff()
    upper, _, lower = ta.BBANDS(c, timeperiod=5, nbdevup=2, nbdevdn=2, matype=0)
    ma21, ma50, ma100 = (ta.SMA(c, timeperiod=n) for n in (21, 50, 100))
    with np.errstate(divide="ignore", invalid="ignore"):
        result = pd.DataFrame({
            "Z_score": (returns-returns.rolling(20).mean())/returns.rolling(20).std(ddof=1),
            "RSI": ta.RSI(c, timeperiod=14)/100,
            "boll": (c-lower)/(upper-lower),
            "ULTOSC": ta.ULTOSC(h, l, c, timeperiod1=7, timeperiod2=14, timeperiod3=28)/100,
            "pct_change": close.pct_change(fill_method=None), "zsVol": (v-mean)/std,
            "PR_MA_Ratio_short": (c-ma21)/ma21, "MA_Ratio_short": (ma21-ma50)/ma50,
            "MA_Ratio": (ma50-ma100)/ma100, "PR_MA_Ratio": (c-ma50)/ma50,
        })
    for name in PATTERNS:
        result[name] = getattr(ta, name)(o, h, l, c)/100
    dates = pd.to_datetime([b.t for b in candles], unit="ms", utc=True)
    result["DayOfWeek"], result["Month"], result["Hourly"] = dates.dayofweek, dates.month, dates.hour/4
    return result.loc[:, FEATURES].replace([np.inf, -np.inf], np.nan)


def labels(closes, backward=5, forward=2, alpha=.038, beta=.24, convention="source"):
    """Training labels only. Unknown future tails stay NaN, never fabricated HOLDs.

    Source code: adjusted EMA, no fee in labels, beta*(1+forward*.1).
    Paper prose: beta*(1+(forward-1)*.1). This option exposes the discrepancy.
    """
    np, pd, _ = dependencies()
    if any(type(n) is not int or not 1 <= n <= 5 for n in (backward, forward)):
        raise DataError("Label windows must be integers from 1 to 5")
    if convention not in {"source", "paper"} or not 0 < alpha < beta < 1:
        raise DataError("Invalid label thresholds or convention")
    values = pd.Series(closes, dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise DataError("Labels require finite positive prices")
    baseline = values.ewm(span=backward, adjust=True).mean()
    change = values.shift(-forward)/baseline-1
    upper = beta*(1+.1*(forward if convention == "source" else forward-1))
    selected = (change.abs() > alpha) & (change.abs() < upper)
    result = pd.Series(np.where(selected, np.where(change > 0, -1., 1.), 0.))
    result[change.isna()] = np.nan
    return result


class NeuralModel:
    """Data-only NPZ loading; neither pickle nor Keras object deserialization."""
    def __init__(self, path=None):
        np, _, _ = dependencies()
        self.path = Path(path) if path else DEFAULT_MODEL
        try:
            if self.path.stat().st_size > 5_000_000:
                raise DataError("Neural model file is unexpectedly large")
            self.identity = hashlib.sha256(self.path.read_bytes()).hexdigest()
            with np.load(self.path, allow_pickle=False) as data:
                self.metadata = json.loads(str(data["metadata"].item()))
                self.mean, self.scale = data["mean"].copy(), data["scale"].copy()
                self.weights = [(data[f"w{i}"].copy(), data[f"b{i}"].copy()) for i in range(4)]
            if (self.metadata["version"] != MODEL_VERSION or self.metadata["feature_version"] != FEATURE_VERSION
                    or self.metadata["features"] != list(FEATURES) or self.metadata["classes"] != list(CLASSES)):
                raise DataError("Incompatible neural feature schema or class order")
            if self.mean.shape != (36,) or self.scale.shape != (36,) or (self.scale <= 0).any():
                raise DataError("Invalid neural scaler")
            if not np.isfinite(self.mean).all() or not np.isfinite(self.scale).all():
                raise DataError("Non-finite neural scaler")
            for (w, b), (inside, outside) in zip(self.weights, ((36, 128), (128, 64), (64, 32), (32, 3))):
                if w.shape != (inside, outside) or b.shape != (outside,) or not np.isfinite(w).all() or not np.isfinite(b).all():
                    raise DataError("Invalid neural network weights")
            self.volume_stats = self.metadata["volume_stats"]
            self.trained_through_ms = self.metadata["trained_through_ms"]
            if (self.metadata.get("negative_slope") != .01 or type(self.trained_through_ms) is not int
                    or self.trained_through_ms < 0 or not isinstance(self.volume_stats, dict) or not self.volume_stats):
                raise DataError("Invalid neural model metadata")
            for asset, stats in self.volume_stats.items():
                if (not isinstance(asset, str) or not isinstance(stats, list) or len(stats) != 2
                        or not all(type(v) in {int, float} for v in stats)
                        or not np.isfinite(stats).all() or stats[0] < 0 or stats[1] <= 0):
                    raise DataError("Invalid frozen volume calibration")
        except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
            raise DataError(f"Cannot load neural model: {exc}") from exc

    def probabilities(self, features):
        np, _, _ = dependencies()
        x = np.asarray(features, dtype=float)
        if x.ndim != 2 or x.shape[1] != 36 or not np.isfinite(x).all():
            raise DataError("Neural prediction requires 36 finite features per row")
        x = ((x-self.mean)/self.scale).astype(np.float32)
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            try:
                for i, (w, b) in enumerate(self.weights):
                    x = x @ w + b
                    if i < 3:
                        x = np.where(x >= 0, x, x*np.float32(.01))
                x = np.exp(x-x.max(axis=1, keepdims=True))
                result = x/x.sum(axis=1, keepdims=True)
            except FloatingPointError as exc:
                raise DataError("Numerically invalid neural prediction") from exc
        if not np.isfinite(result).all():
            raise DataError("Non-finite neural probabilities")
        return result

    def features(self, candles, asset):
        # USD and USDT are deliberately mapped by base asset. Venue basis remains visible.
        stats = self.volume_stats.get(asset)
        if stats is None:
            raise DataError(f"Model has no frozen volume calibration for {asset}; use a calibrated model")
        return feature_frame(candles, stats)

    def predict(self, candles, asset):
        frame = self.features(candles, asset)
        values = self.probabilities(frame.iloc[[-1]].to_numpy())[0]
        return {"label": CLASSES[int(values.argmax())],
                "probabilities": dict(zip(CLASSES, map(float, values))),
                "features": dict(zip(FEATURES, map(float, frame.iloc[-1]))),
                "signal_end": candles[-1].end, "model_id": self.identity}
