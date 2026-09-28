"""Configuration, candle validation, and deterministic indicator primitives."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone


VERSION = 9


ENGINE_VERSION = "kraken-closed-bar-v9.3"


PREVIOUS_ENGINE_VERSION = "kraken-closed-bar-v9.2"


LEGACY_ENGINE_VERSION = "kraken-closed-bar-v9.1"


H4 = 14_400_000


M15 = 900_000

M5 = 300_000
M30 = 1_800_000
SMC_ENGINE_VERSION = "smc-video-v1"


class DataError(ValueError):
    pass


def finite(value, label="number", minimum=None):
    if isinstance(value, bool):
        raise DataError(f"{label} must be numeric, not boolean")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DataError(f"Invalid {label}") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise DataError(f"Invalid {label}")
    return result


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ms is not None else "—"


def safe_error(exc):
    message = str(exc)
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DERIBIT_CLIENT_ID", "DERIBIT_CLIENT_SECRET"):
        secret = os.getenv(name)
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", message)
    return message[:350]


@dataclass(frozen=True)
class Rules:
    strategy_model: str = "legacy"
    nn_model_path: str = ""
    nn_model_id: str = "parente_mlp_v1"
    nn_model_bundle_path: str = ""
    nn_limitations: bool = True
    nn_stop_loss: float = 0.10
    nn_signal_max_age_seconds: int = 60
    market_mode: str = "spot"
    smc_setup_minutes: int = 30
    smc_entry_minutes: int = 5
    smc_entry_method: str = "both"
    smc_breakout_entry: bool = False
    smc_breakout_window_bars: int = 6
    smc_breakout_max_chase_bps: float = 20.0
    smc_pivot_strength: int = 2
    smc_reversal_bars: int = 2
    smc_stop_basis: str = "swing"
    smc_stop_buffer_bps: float = 1.0
    smc_tp_sweep_buffer_bps: float = 1.0
    smc_tp_alert_bps: float = 10.0
    smc_gex_targets: bool = False
    smc_gex_alignment_bps: float = 10.0
    smc_gex_max_basis_bps: float = 50.0
    atr_period: int = 14
    volume_period: int = 20
    pivot_strength: int = 2
    ob_lookback: int = 120
    ob_follow_bars: int = 3
    setup_lifetime_bars: int = 24
    bearish_body_atr: float = 0.8
    bearish_close_location: float = 0.3
    bearish_rvol_min: float = 0.0  # Optional. Selloff participation is context.
    reclaim_body_atr: float = 0.6
    reclaim_close_location: float = 0.7
    reclaim_rvol_min: float = 1.2
    sweep_atr: float = 0.05
    stop_buffer_atr: float = 0.2
    mss_lookback: int = 3
    ltf_retest_lifetime_bars: int = 16
    trigger_fresh_bars: int = 2
    reclaim_require_trend: bool = False
    momentum_roc_period: int = 12
    momentum_signal_period: int = 9
    momentum_cross_window: int = 3
    momentum_rvol_min: float = 1.5
    momentum_rsi_period: int = 14
    momentum_rsi_min: float = 50.0
    momentum_rsi_max: float = 80.0
    momentum_require_trend: bool = True
    momentum_stop_atr: float = 1.5
    max_chase_atr: float = 0.25
    max_spread_bps: float = 30.0
    target1_net_r: float = 2.0
    target2_net_r: float = 3.0
    require_clear_target1: bool = False
    fee_rate: float = 0.008  # Conservative public Tier 1 spot taker assumption.
    slippage_rate: float = 0.0005
    paper_equity: float = 1000.0
    paper_floor: float = 950.0
    risk_per_trade: float = 0.005
    max_total_risk: float = 0.02
    max_allocation: float = 0.25
    minimum_notional: float = 10.0

    @property
    def combined(self):
        return self.strategy_model in {"smc_nn", "legacy_nn"}

    @property
    def base_strategy(self):
        return {"smc_nn": "smc_video", "legacy_nn": "legacy"}.get(self.strategy_model, self.strategy_model)

    @property
    def paper_namespace(self):
        return {"smc_video": "smc", "legacy": "legacy", "neural_network": "neural"}[self.base_strategy]

    def validate(self):
        integer_bounds = {
            "nn_signal_max_age_seconds": (1, 300),
            "smc_setup_minutes": (15, 30), "smc_entry_minutes": (1, 5),
            "smc_pivot_strength": (1, 5), "smc_reversal_bars": (0, 5),
            "smc_breakout_window_bars": (1, 12),
            "atr_period": (2, 50), "volume_period": (2, 100),
            "pivot_strength": (1, 5), "ob_lookback": (20, 300),
            "ob_follow_bars": (1, 6), "setup_lifetime_bars": (1, 40),
            "mss_lookback": (2, 20), "ltf_retest_lifetime_bars": (1, 64),
            "trigger_fresh_bars": (1, 4), "momentum_roc_period": (2, 50),
            "momentum_signal_period": (2, 30), "momentum_cross_window": (1, 6),
            "momentum_rsi_period": (2, 50),
        }
        for key, value in asdict(self).items():
            if key in {"nn_model_path", "nn_model_bundle_path"}:
                if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
                    raise DataError(f"{key} must be a path string")
            elif key == "nn_model_id":
                candidate = r"(?:lstm_classifier_v1|gru_classifier_v1|cnn_lstm_classifier_v1|grouped_attention_lstm_v1|probability_ensemble_v1)@[0-9a-f]{12}"
                if type(value) is not str or value != "parente_mlp_v1" and not re.fullmatch(candidate, value):
                    raise DataError("Unknown NN model selection")
            elif key in {"strategy_model", "market_mode", "smc_entry_method", "smc_stop_basis"}:
                allowed = {"strategy_model": {"legacy", "smc_video", "neural_network", "smc_nn", "legacy_nn"},
                           "market_mode": {"spot", "margin"},
                           "smc_entry_method": {"both", "conservative", "aggressive"},
                           "smc_stop_basis": {"swing", "order_block", "atr"}}[key]
                if value not in allowed:
                    raise DataError(f"Invalid {key}")
            elif key in integer_bounds:
                low, high = integer_bounds[key]
                if type(value) is not int or not low <= value <= high:
                    raise DataError(f"{key} must be an integer from {low} to {high}")
            elif key in {"nn_limitations", "reclaim_require_trend", "momentum_require_trend", "require_clear_target1", "smc_gex_targets", "smc_breakout_entry"}:
                if type(value) is not bool:
                    raise DataError(f"{key} must be true or false")
            else:
                if type(value) not in {int, float}:
                    raise DataError(f"{key} must be a JSON number")
                finite(value, key, 0)
        for key in ("bearish_close_location", "reclaim_close_location", "risk_per_trade", "max_total_risk", "max_allocation"):
            if not 0 < getattr(self, key) <= 1:
                raise DataError(f"{key} must be in (0, 1]")
        if self.nn_model_id == "parente_mlp_v1" and self.nn_model_bundle_path:
            raise DataError("NN model bundle path applies only to candidate models")
        if self.nn_model_id != "parente_mlp_v1" and self.nn_model_path:
            raise DataError("Parente model path must be empty when selecting a candidate")
        if not 0 <= self.fee_rate < 0.05 or not 0 <= self.slippage_rate < 0.05:
            raise DataError("Fee and slippage rates must be fractions below 0.05")
        if not 0 < self.nn_stop_loss <= .10:
            raise DataError("nn_stop_loss must be in (0, 0.10]")
        if not 0 < self.smc_breakout_max_chase_bps <= 100:
            raise DataError("Breakout maximum chase must be greater than zero and at most 100 bps")
        if not 0 <= self.momentum_rsi_min < self.momentum_rsi_max <= 100:
            raise DataError("Invalid RSI interval")
        if not 1 <= self.target1_net_r < self.target2_net_r:
            raise DataError("Require 1 <= TP1 net R < TP2 net R")
        if not 0 <= self.paper_floor < self.paper_equity:
            raise DataError("Require 0 <= paper_floor < paper_equity")
        if self.risk_per_trade > self.max_total_risk:
            raise DataError("Per-trade risk exceeds portfolio risk")
        if self.stop_buffer_atr <= 0 or self.momentum_stop_atr <= 0:
            raise DataError("Stop distances must be positive")
        if (self.smc_setup_minutes, self.smc_entry_minutes) not in {(30, 5), (15, 1)}:
            raise DataError("Video timeframes must be 30M/5M or 15M/1M")
        if not 0 < self.smc_stop_buffer_bps <= 100:
            raise DataError("SMC stop buffer must be above 0 and at most 100 basis points")
        if not 0 < self.smc_tp_sweep_buffer_bps <= 100:
            raise DataError("SMC take-profit sweep buffer must be above 0 and at most 100 basis points")
        if not 0 <= self.smc_tp_alert_bps <= 100:
            raise DataError("SMC take-profit alert range must be from 0 to 100 basis points (0 disables alerts)")
        # GEX tolerances use the finite, non-negative JSON-number checks above;
        # unlike stop and sweep buffers, they have no percentage ceiling.
        return self


DEFAULT_ASSETS = [
    {"name": "BTC", "symbol": "BTC/USD", "enabled": True, "price_decimals": 2},
    {"name": "ETH", "symbol": "ETH/USD", "enabled": True, "price_decimals": 2},
    {"name": "SOL", "symbol": "SOL/USD", "enabled": True, "price_decimals": 3},
]


def normalise_pair(symbol):
    compact = re.sub(r"[\s/_-]", "", str(symbol).upper())
    # Kraken can return legacy internal keys (for example XXBTZUSD) even
    # when a display-name response was requested. Accept those read-only
    # aliases wherever a pair key is compared.
    for internal_quote, quote in (("ZUSD", "USD"), ("ZEUR", "EUR"), ("ZGBP", "GBP"),
                                  ("ZCAD", "CAD"), ("ZAUD", "AUD"), ("ZJPY", "JPY"), ("ZCHF", "CHF")):
        if compact.endswith(internal_quote) and len(compact) > len(internal_quote):
            base = compact[:-len(internal_quote)]
            if base.startswith("X") and len(base) > 1:
                base = base[1:]
            return f"{'BTC' if base == 'XBT' else base}/{quote}"
    for quote in ("USDT", "USDC", "USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF", "XBT", "BTC", "ETH"):
        if compact.endswith(quote) and len(compact) > len(quote):
            base = compact[:-len(quote)]
            return f"{'BTC' if base == 'XBT' else base}/{'BTC' if quote == 'XBT' else quote}"
    raise DataError("Invalid pair; use BTC/USD")


def load_settings(path):
    document = json.loads(Path(path).read_text(encoding="utf-8-sig")) if Path(path).exists() else {}
    if not isinstance(document, dict):
        raise DataError("Settings must be a JSON object")
    unknown = set(document) - {"assets", "strategy", "refresh_seconds"}
    if unknown:
        raise DataError(f"Unknown settings: {', '.join(sorted(unknown))}")
    raw_rules = document.get("strategy", {})
    if not isinstance(raw_rules, dict) or set(raw_rules) - set(Rules.__dataclass_fields__):
        raise DataError("Unknown or malformed strategy settings")
    rules = Rules(**raw_rules).validate()
    assets = {}
    pairs = set()
    entries = document.get("assets", DEFAULT_ASSETS)
    if not isinstance(entries, list):
        raise DataError("assets must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("enabled", True)) is not bool:
            raise DataError("Invalid asset entry")
        if not entry.get("enabled", True):
            continue
        name = str(entry.get("name", "")).strip().upper()
        pair = normalise_pair(entry.get("symbol", ""))
        digits = entry.get("price_decimals", 3)
        if not name.isalnum() or name in assets or pair in pairs:
            raise DataError("Asset names and pairs must be distinct")
        # Cash and portfolio risk are denominated in USD. Do not mix quote units.
        if not pair.endswith("/USD"):
            raise DataError(f"{pair}: USD quote required for the USD paper portfolio")
        if type(digits) is not int or not 0 <= digits <= 10:
            raise DataError("price_decimals must be an integer from 0 to 10")
        assets[name] = {"symbol": pair, "price_decimals": digits}
        pairs.add(pair)
    if not assets:
        raise DataError("Enable at least one asset")
    refresh = finite(document.get("refresh_seconds", 15), "refresh_seconds")
    if not 5 <= refresh <= 300:
        raise DataError("refresh_seconds must be between 5 and 300")
    return assets, rules, refresh



def load_application_settings(path):
    """The live application always simulates NN; old strategy ledgers stay readable."""
    assets, rules, refresh = load_settings(path)
    return assets, replace(rules, strategy_model="neural_network"), refresh


@dataclass(frozen=True)
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float
    interval: int

    @property
    def end(self):
        return self.t + self.interval - 1


def validate_candles(candles, interval, now, minimum=1, fresh=True):
    if len(candles) < minimum:
        raise DataError(f"Need {minimum} completed {interval // 60000}m candles; received {len(candles)}")
    previous = None
    for bar in candles:
        vals = [finite(x, "OHLC price", 1e-15) for x in (bar.o, bar.h, bar.l, bar.c)]
        finite(bar.v, "base volume", 0)
        if bar.h < max(bar.o, bar.c, bar.l) or bar.l > min(bar.o, bar.c):
            raise DataError("Inconsistent OHLC bounds")
        if type(bar.t) is not int or bar.t < 0 or bar.t % interval or bar.interval != interval:
            raise DataError("Wrong candle timestamp units, alignment, or interval")
        if bar.end >= now:
            raise DataError("An uncompleted candle reached the signal engine")
        if previous is not None and bar.t != previous + interval:
            raise DataError("Duplicate, unordered, or missing candles")
        previous = bar.t
    if fresh and now - candles[-1].end > interval + 5000:
        raise DataError("Completed candle feed is stale")
    return candles


def parse_kraken_rows(rows, interval, now):
    if not isinstance(rows, list) or len(rows) < 2:
        raise DataError("Kraken returned insufficient OHLC rows")
    candles = []
    # Kraken explicitly marks the final row as uncommitted, even at a boundary.
    for row in rows[:-1]:
        if not isinstance(row, list) or len(row) < 8:
            raise DataError("Malformed Kraken OHLC row")
        stamp = finite(row[0], "Kraken timestamp", 0)
        if not stamp.is_integer():
            raise DataError("Kraken timestamp must be integer seconds")
        bar = Candle(int(stamp) * 1000, *(finite(row[i], "OHLC", 1e-15) for i in (1, 2, 3, 4)), finite(row[6], "base volume", 0), interval)
        if bar.end < now:
            candles.append(bar)
    return validate_candles(candles, interval, now)


def atr_series(candles, period=14):
    result = [None] * len(candles)
    ranges = [max(b.h - b.l, abs(b.h - candles[i-1].c), abs(b.l - candles[i-1].c)) if i else b.h - b.l for i, b in enumerate(candles)]
    if len(ranges) >= period:
        result[period-1] = sum(ranges[:period]) / period
        for i in range(period, len(ranges)):
            result[i] = (result[i-1] * (period-1) + ranges[i]) / period
    return result


def ema(values, period):
    if not values:
        return []
    result = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(result[-1] + alpha * (value - result[-1]))
    return result


def rsi_series(closes, period=14):
    result = [None] * len(closes)
    if len(closes) <= period:
        return result
    changes = [b-a for a, b in zip(closes, closes[1:])]
    gain = sum(max(x, 0) for x in changes[:period]) / period
    loss = sum(max(-x, 0) for x in changes[:period]) / period
    def value():
        return (100 if gain else 50) if loss == 0 else 100 - 100 / (1 + gain / loss)
    result[period] = value()
    for i in range(period+1, len(closes)):
        change = changes[i-1]
        gain = (gain * (period-1) + max(change, 0)) / period
        loss = (loss * (period-1) + max(-change, 0)) / period
        result[i] = value()
    return result


def rvol(candles, index, period):
    if index < period:
        return None
    baseline = sum(b.v for b in candles[index-period:index]) / period
    return candles[index].v / baseline if baseline > 0 else None


def location(bar):
    return (bar.c-bar.l) / (bar.h-bar.l) if bar.h > bar.l else 0.5


def pivot(candles, i, strength, known_at, high=False):
    if i < strength or i + strength > known_at:
        return False
    value = candles[i].h if high else candles[i].l
    neighbours = candles[i-strength:i] + candles[i+1:i+strength+1]
    return all(value > b.h if high else value < b.l for b in neighbours)


def check(key, label, passed=None, measured=None, required=None, candle=None, context=False):
    return {"key": key, "label": label, "status": "context" if context else ("waiting" if passed is None else "pass" if passed else "wait"),
            "measured": measured, "required": required, "candle_ms": candle}


def passed(checks):
    return all(c["status"] in {"pass", "context"} for c in checks)


def trend_at(candles, i):
    if i < 249:
        return None
    closes = [b.c for b in candles[:i+1]]
    e50, e200 = ema(closes, 50)[-1], ema(closes, 200)[-1]
    return closes[-1] > e200 and e50 > e200
