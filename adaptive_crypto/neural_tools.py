"""Offline author-model conversion and honest next-open backtesting.

python -m adaptive_crypto.neural_tools --help
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

from .core import Candle, DataError, H4, validate_candles
from .ledger import atomic_json
from .neural import (CLASSES, DEFAULT_MODEL, FEATURES, FEATURE_VERSION, MODEL_VERSION,
                     NeuralModel, dependencies)

ARCHIVE_MD5 = "012f76ea14b5f95fff2b77f3c4e5441c"
AUTHOR_SOURCE = "https://figshare.com/articles/code/CryptoTrading_zip/22953377/2"


def import_author_model(archive, output=DEFAULT_MODEL):
    """Read data/weights only from the published archive; do not execute its scripts.

    Reconstruct StandardScaler on precisely the final-training pool documented in
    run_train_final.py/model_train_test_lib.py, before balancing and random split.
    """
    np, pd, _ = dependencies()
    import h5py
    archive, output = Path(archive), Path(output)
    if output.exists():
        raise DataError("Model output exists; choose a new output path")
    checksum = hashlib.md5()
    with archive.open("rb") as f:
        for block in iter(lambda: f.read(1024*1024), b""):
            checksum.update(block)
    digest = checksum.hexdigest()
    if digest != ARCHIVE_MD5:
        raise DataError("Archive checksum does not match the published v2 research archive")
    arrays, volumes = {}, {}
    with zipfile.ZipFile(archive) as z:
        raw_weights = z.read("CryptoTrading/model_final_5_2.h5")
        with h5py.File(io.BytesIO(raw_weights), "r") as h5:
            config = json.loads(h5.attrs["model_config"])
            layers = [x["config"] for x in config["config"]["layers"] if x["class_name"] == "Dense"]
            if [x["units"] for x in layers] != [128, 64, 32, 3]:
                raise DataError("Unexpected author network architecture")
            for i, layer in enumerate(layers):
                group = h5["model_weights"][layer["name"]][layer["name"]]
                arrays[f"w{i}"] = group["kernel:0"][:]
                arrays[f"b{i}"] = group["bias:0"][:]
        n, mean, m2, latest = 0, np.zeros(36), np.zeros(36), 0
        train_assets = set()
        with z.open("CryptoTrading/processed_data/raw_data_4_hour_train_test_data.csv") as stream:
            for frame in pd.read_csv(stream, chunksize=40000):
                actual = [c for c in frame.columns if c not in {"Date", "Open", "High", "Low", "Close", "Volume", "Asset_name"} and not c.startswith("lab_")]
                if actual != list(FEATURES):
                    raise DataError("Published feature order changed")
                frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
                frame = frame[~frame.Asset_name.isin(["BTCUSDT", "ETHUSDT", "ALGOUSDT"]) & (frame["pct_change"] < .24)]
                if frame.empty:
                    continue
                x = frame.loc[:, FEATURES].to_numpy(dtype=float)
                count = len(x)
                delta = x.mean(axis=0)-mean
                m2 += ((x-x.mean(axis=0))**2).sum(axis=0) + delta**2*n*count/(n+count)
                mean += delta*count/(n+count)
                n += count
                train_assets.update(frame.Asset_name.unique())
                latest = max(latest, int(pd.to_datetime(frame.Date, utc=True).max().value//1_000_000)+H4-1)
        scale = np.sqrt(m2/n)
        arrays.update(mean=mean, scale=np.where(scale == 0, 1., scale))
        for name in z.namelist():
            if name.startswith("CryptoTrading/raw_data_4_hour/") and name.endswith("USDT.csv"):
                with z.open(name) as stream:
                    raw = pd.read_csv(stream, usecols=["Volume", "Date"])
                std = float(raw.Volume.std(ddof=1))
                if std > 0 and np.isfinite(std):
                    volumes[Path(name).stem[:-4]] = [float(raw.Volume.mean()), std]
                    latest = max(latest, int(pd.to_datetime(raw.Date, utc=True).max().value//1_000_000)+H4-1)
    metadata = {"version": MODEL_VERSION, "feature_version": FEATURE_VERSION,
                "features": list(FEATURES), "classes": list(CLASSES), "negative_slope": .01,
                "backward": 5, "forward": 2, "alpha": .038, "beta_effective": .288,
                "label_convention": "source", "volume_stats": volumes,
                "trained_through_ms": latest, "scaler_rows": n,
                "training_assets": sorted(train_assets), "excluded_assets": ["BTC", "ETH", "ALGO"],
                "source": AUTHOR_SOURCE, "license": "GPL-3.0-or-later",
                "archive_md5": digest, "h5_sha256": hashlib.sha256(raw_weights).hexdigest(),
                "description": "Authors' published 5/2 weights; reconstructed final-training scaler; frozen historical Binance base-volume statistics. Live Kraken/USD is a venue and currency transfer."}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never silently replace an in-use model.
    with output.open("xb") as f:
        np.savez_compressed(f, metadata=np.array(json.dumps(metadata)), **arrays)
    model = NeuralModel(output)
    atomic_json(output.with_suffix(".json"), {**metadata, "model_sha256": model.identity})
    return {"path": str(output.resolve()), "model_id": model.identity, "scaler_rows": n,
            "trained_through_ms": latest, "training_assets": len(train_assets)}


def read_candles(path):
    """CSV Date (UTC), Open, High, Low, Close, Volume; one asset, exactly 4H."""
    _, pd, _ = dependencies()
    frame = pd.read_csv(path)
    required = ["Date", "Open", "High", "Low", "Close", "Volume"]
    if not set(required).issubset(frame):
        raise DataError("CSV requires Date, Open, High, Low, Close, Volume")
    if "Asset_name" in frame and frame.Asset_name.nunique() != 1:
        raise DataError("Backtest CSV must contain exactly one asset")
    stamps = pd.to_datetime(frame.Date, utc=True, errors="raise")
    candles = [Candle(int(t.value//1_000_000), *(float(row[k]) for k in required[1:]), H4)
               for t, (_, row) in zip(stamps, frame.iterrows())]
    if not candles:
        raise DataError("CSV has no candles")
    return validate_candles(candles, H4, candles[-1].end+1, minimum=101, fresh=False)


def simulate(candles, predictions, *, fee=.001, slippage=.0005, stop_loss=.10, initial=1000.):
    """Buy/Hold/Sell long-only state machine; t's signal executes at t+1 open.

    Stops gap at the adverse opening price, apply fees/slippage, and take precedence.
    Final liquidation occurs at the last close, with no entry on the final signal.
    """
    import math
    if (len(predictions) != len(candles) or any(x not in {*CLASSES, None} for x in predictions)
            or not all(math.isfinite(v) for v in (fee, slippage, stop_loss, initial))
            or not 0 <= fee < .05 or not 0 <= slippage < .05 or not 0 < stop_loss <= .10 or initial <= 0):
        raise DataError("Invalid backtest inputs")
    if not candles:
        raise DataError("Backtest requires candles")
    validate_candles(candles, H4, candles[-1].end+1, fresh=False)
    cash, position, trades, curve, peak, drawdown = initial, None, [], [], initial, 0.
    def close(price, bar, reason):
        nonlocal cash, position
        fill = price*(1-slippage)
        cash = position["quantity"]*fill*(1-fee)
        # A low proves an intrabar stop only by candle close; OHLC has no
        # exact fill timestamp. Keep known opening/terminal executions distinct.
        intrabar = reason == "stop"
        closed_ms = bar.end if intrabar or reason == "end" else bar.t
        basis = "candle_close_confirmation" if intrabar else "final_candle_close" if reason == "end" else "candle_open"
        trades.append({**position, "exit": fill, "closed_ms": closed_ms,
                       "exit_time_exact": not intrabar, "exit_time_basis": basis,
                       "reason": reason, "pnl": cash-position["capital"]})
        position = None
    for i, bar in enumerate(candles):
        signal = predictions[i-1] if i else None
        stopped = False
        if position and bar.o <= position["stop"]:
            close(bar.o, bar, "gap_stop")
            stopped = True
        if position and signal == "SELL":
            close(bar.o, bar, "sell")
        if not position and not stopped and signal == "BUY":
            fill = bar.o*(1+slippage)
            position = {"entry": fill, "quantity": cash/(fill*(1+fee)), "capital": cash,
                        "stop": fill*(1-stop_loss), "opened_ms": bar.t}
            cash = 0.
        if position and bar.l <= position["stop"]:
            close(position["stop"], bar, "stop")
        if i == len(candles)-1 and position:
            close(bar.c, bar, "end")
        equity = cash if not position else position["quantity"]*bar.c*(1-slippage)*(1-fee)
        peak = max(peak, equity)
        drawdown = max(drawdown, 1-equity/peak)
        curve.append({"time_ms": bar.end, "equity": equity})
    return {"initial": initial, "final": cash, "roi": cash/initial-1, "max_drawdown": drawdown,
            "trade_count": len(trades), "trades": trades, "equity": curve,
            "fee_rate": fee, "slippage_rate": slippage, "stop_loss": stop_loss,
            "execution": "Next-open long-only, stop precedence, all available capital per asset; final close liquidation"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import-author", help="Convert the checksum-verified Figshare archive (offline)")
    imp.add_argument("archive", type=Path)
    imp.add_argument("--output", type=Path, default=DEFAULT_MODEL)
    inspect = commands.add_parser("inspect", help="Validate and inspect a model")
    inspect.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    bt = commands.add_parser("backtest", help="Backtest a single-asset 4H CSV after the model calibration period")
    bt.add_argument("csv", type=Path)
    bt.add_argument("--asset", required=True)
    bt.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    bt.add_argument("--output", type=Path, required=True)
    bt.add_argument("--fee", type=float, default=.001)
    bt.add_argument("--slippage", type=float, default=.0005)
    bt.add_argument("--stop-loss", type=float, default=.10)
    args = parser.parse_args(argv)
    try:
        if args.command == "import-author":
            result = import_author_model(args.archive, args.output)
        else:
            model = NeuralModel(args.model)
            if args.command == "inspect":
                result = {**model.metadata, "model_id": model.identity}
            else:
                np, _, _ = dependencies()
                candles = read_candles(args.csv)
                frame = model.features(candles, args.asset.upper())
                eligible = np.isfinite(frame.to_numpy()).all(axis=1) & np.array([b.t > model.trained_through_ms for b in candles])
                predictions = [None]*len(candles)
                if sum(eligible) < 2:
                    raise DataError("Need at least two feature-ready candles after the model calibration period")
                for i, p in zip(np.flatnonzero(eligible), model.probabilities(frame[eligible].to_numpy())):
                    predictions[int(i)] = CLASSES[int(p.argmax())]
                first = int(np.flatnonzero(eligible)[0])
                run = dict(fee=args.fee, slippage=args.slippage, stop_loss=args.stop_loss)
                result = simulate(candles[first:], predictions[first:], **run)
                result.update(asset=args.asset.upper(), model_id=model.identity, start_ms=candles[first].t,
                              trained_through_ms=model.trained_through_ms)
                rng = np.random.default_rng(2987)
                dummy = rng.choice(CLASSES, len(candles)-first, p=[.15, .70, .15]).tolist()
                hold = ["BUY"]+["HOLD"]*(len(dummy)-1)
                result["baselines"] = {name: {k: v for k, v in simulate(candles[first:], lab, **run).items() if k not in {"equity", "trades"}}
                                       for name, lab in (("dummy", dummy), ("buy_hold_with_stop", hold))}
                atomic_json(args.output, result)
                result = {k: v for k, v in result.items() if k not in {"equity", "trades"}}
        print(json.dumps(result, indent=2, allow_nan=False))
    except (DataError, OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
