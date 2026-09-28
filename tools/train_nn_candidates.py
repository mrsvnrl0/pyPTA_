"""Offline Kraken/USD candidate-data audit and bounded model training.

Official Kraken archive: https://support.kraken.com/in/articles/360047124832-
downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data

The full archive is larger than typical workstation free space. ``archive-list``
and ``archive-fetch`` use HTTP byte ranges to read the ZIP directory and only
the selected 240-minute members. Extracted members receive ZIP CRC checks.
This never treats a recent 720-candle REST response as a full history.
"""
from __future__ import annotations

import argparse
import bisect
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import time
import zipfile

from adaptive_crypto.core import Candle, H4
from adaptive_crypto.candidate_features import (
    FEATURE_NAMES, FEATURE_SCHEMA, HISTORY_BARS, LOOKBACK_ROWS, feature_rows,
)

ARCHIVE_URL = "https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_Full_2026Q2.zip.part{:02d}"
ARCHIVE_SHA256 = "fc81b54cba6e12af3e9422dde9416179e6ef76af4831d48d839fbdb43018eaa4"
ARCHIVE_ASOF = "2026-06-30"
CLASSES = ("BUY", "HOLD", "SELL")
LABEL_VERSION = "parente-source-5-2-v1"
ASSETS = ("BTC", "ETH", "SOL", "SUI")


def sha256_path(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class RangeZip(io.RawIOBase):
    """Seekable read-only view of Kraken's five split ZIP parts.

    A single member is fetched on demand and verified by ``zipfile`` CRC. The
    official whole-archive SHA256 cannot be checked without downloading 9 GB;
    that limitation is explicit in generated provenance.
    """

    def __init__(self, timeout=30):
        import requests
        self._session = requests.Session()
        self.urls = [ARCHIVE_URL.format(i) for i in range(5)]
        sizes = []
        for url in self.urls:
            response = self._session.head(url, timeout=timeout)
            response.raise_for_status()
            sizes.append(int(response.headers["Content-Length"]))
        self._edges = [0]
        for size in sizes:
            self._edges.append(self._edges[-1] + size)
        self.length = self._edges[-1]
        self._pos = 0
        self.timeout = timeout
        self.bytes_fetched = 0
        self.requests = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        value = offset + (self.length if whence == io.SEEK_END else self._pos if whence == io.SEEK_CUR else 0)
        if value < 0:
            raise ValueError("Negative archive seek")
        self._pos = value
        return value

    def read(self, size=-1):
        if self._pos >= self.length:
            return b""
        if size is None or size < 0:
            size = self.length - self._pos
        remaining = min(size, self.length - self._pos)
        pieces = []
        while remaining:
            index = bisect.bisect_right(self._edges, self._pos) - 1
            start = self._pos - self._edges[index]
            count = min(remaining, self._edges[index + 1] - self._pos)
            response = self._session.get(self.urls[index], headers={"Range": f"bytes={start}-{start+count-1}"}, timeout=self.timeout)
            if response.status_code != 206 or len(response.content) != count:
                raise OSError(f"Archive byte-range request failed: HTTP {response.status_code}, {len(response.content)} bytes")
            expected = f"bytes {start}-{start+count-1}/{self._edges[index+1]-self._edges[index]}"
            if response.headers.get("Content-Range") != expected:
                raise OSError("Archive server returned a different byte range")
            pieces.append(response.content)
            self._pos += count
            remaining -= count
            self.bytes_fetched += count
            self.requests += 1
        return b"".join(pieces)


def archive_matches(archive, assets=ASSETS):
    matches = {}
    for asset in assets:
        # Kraken's legacy spot BTC symbol is XBT. Require the exact base pair;
        # suffix matching would accidentally accept wrapped/staked derivatives.
        official_name = "XBTUSD_240.CSV" if asset == "BTC" else f"{asset}USD_240.CSV"
        names = [item for item in archive.infolist()
                 if Path(item.filename).name.upper() == official_name]
        matches[asset] = [{"name": item.filename, "uncompressed_bytes": item.file_size,
                           "compressed_bytes": item.compress_size,
                           "crc32": f"{item.CRC:08x}"} for item in names]
    return matches


def archive_list(args):
    reader = RangeZip()
    with zipfile.ZipFile(reader) as archive:
        result = {"archive_sha256_published": ARCHIVE_SHA256, "archive_asof": ARCHIVE_ASOF,
                  "archive_bytes": reader.length, "matches": archive_matches(archive, args.assets),
                  "range_bytes_fetched": reader.bytes_fetched, "range_requests": reader.requests}
    print(json.dumps(result, indent=2))


def _parse_official_csv(stream, asset, output):
    count, first, last, gaps = 0, None, None, []
    with output.open("w", newline="", encoding="utf-8") as target:
        writer = csv.writer(target)
        writer.writerow(("asset", "open_time_ms", "open", "high", "low", "close", "volume"))
        for row in csv.reader(io.TextIOWrapper(stream, encoding="utf-8")):
            if len(row) != 7:
                raise ValueError("Official Kraken OHLCVT row must have seven columns")
            timestamp = int(row[0])
            if timestamp < 1_000_000_000_000:
                timestamp *= 1000
            if timestamp % H4:
                raise ValueError("Kraken 240-minute row is not UTC 4H aligned")
            o, h, l, c, v = map(float, row[1:6])
            if (not all(math.isfinite(x) for x in (o, h, l, c, v)) or
                    min(o, h, l, c) <= 0 or v < 0 or l > min(o, c) or h < max(o, c)):
                raise ValueError("Invalid official Kraken OHLCV row")
            if last is not None and timestamp <= last:
                raise ValueError("Official Kraken OHLCVT file has duplicate or reversed bars")
            if last is not None and timestamp - last > H4:
                gaps.append([last + H4, timestamp - H4])
            writer.writerow((asset, timestamp, *row[1:6]))
            count += 1
            first = timestamp if first is None else first
            last = timestamp
    return {"rows": count, "first_open_ms": first, "last_open_ms": last,
            "gap_count": len(gaps), "gap_ranges_ms": gaps}


def archive_fetch(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    reader = RangeZip()
    provenance = {"source": "Kraken official historical OHLCVT 2026Q2 full archive",
                  "source_url": ARCHIVE_URL.format(0), "archive_asof": ARCHIVE_ASOF,
                  "archive_sha256_published": ARCHIVE_SHA256,
                  "whole_archive_hash_verified": False,
                  "verification": "ZIP member CRC32 verified by decompression; normalized CSV SHA256 recorded",
                  "retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "timeframe_minutes": 240, "quote_currency": "USD", "assets": {}}
    with zipfile.ZipFile(reader) as archive:
        matches = archive_matches(archive, args.assets)
        for asset in args.assets:
            if len(matches[asset]) != 1:
                provenance["assets"][asset] = {"status": "unavailable",
                                                "reason": f"Expected one official 240-minute member, found {len(matches[asset])}",
                                                "matches": matches[asset]}
                continue
            item = archive.getinfo(matches[asset][0]["name"])
            target = output / f"{asset}USD_240.csv"
            if target.exists():
                raise FileExistsError(f"Refusing to replace existing source data: {target}")
            with archive.open(item) as stream:
                summary = _parse_official_csv(stream, asset, target)
            provenance["assets"][asset] = {"status": "retrieved", "member": item.filename,
                                            "member_crc32": f"{item.CRC:08x}",
                                            "normalized_csv": target.name,
                                            "normalized_sha256": sha256_path(target), **summary}
            print(f"{asset}: {summary['rows']} bars, {summary['gap_count']} gaps, {reader.bytes_fetched:,} bytes fetched", flush=True)
    provenance["archive_bytes"] = reader.length
    provenance["range_bytes_fetched"] = reader.bytes_fetched
    provenance["range_requests"] = reader.requests
    destination = output / "provenance.json"
    if destination.exists():
        raise FileExistsError(f"Refusing to replace source provenance: {destination}")
    destination.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"provenance": str(destination), "assets": {a: v["status"] for a, v in provenance["assets"].items()}}, indent=2))


def _read_normalized(path, asset):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or any(row["asset"] != asset for row in rows):
        raise ValueError(f"No valid {asset} rows in {path}")
    return rows


def rest_merge(args):
    """Append available committed 4H Kraken API bars to verified archive rows."""
    import requests
    source, output = Path(args.source), Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    provenance = json.loads((source / "provenance.json").read_text(encoding="utf-8"))
    if provenance.get("source") != "Kraken official historical OHLCVT 2026Q2 full archive":
        raise ValueError("Expected verified official archive provenance")
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    summary = {"archive_provenance_sha256": sha256_path(source / "provenance.json"),
               "api": "https://api.kraken.com/0/public/OHLC", "retrieved_utc": datetime.now(timezone.utc).isoformat(),
               "api_limit": "last 720 bars, final uncommitted candle excluded",
               "assets": {}}
    for asset in args.assets:
        info = provenance["assets"].get(asset, {})
        if info.get("status") != "retrieved":
            summary["assets"][asset] = {"status": "unavailable", "reason": "Missing official archive member"}
            continue
        original = source / info["normalized_csv"]
        if sha256_path(original) != info["normalized_sha256"]:
            raise ValueError(f"Archive CSV changed since provenance: {asset}")
        rows = _read_normalized(original, asset)
        last = int(rows[-1]["open_time_ms"])
        pair = "XBTUSD" if asset == "BTC" else asset + "USD"
        request_params = {"pair": pair, "interval": 240, "since": last // 1000 - 1}
        response = requests.get(summary["api"], params=request_params, timeout=30)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise ValueError(f"Kraken public OHLC error for {asset}: {body['error']}")
        payload = [v for v in body["result"].values() if isinstance(v, list)]
        if len(payload) != 1 or len(payload[0]) < 2:
            raise ValueError(f"Unexpected Kraken public OHLC response for {asset}")
        # The last API entry is uncommitted by contract, even if its time looks
        # aligned and it happens to have nonzero volume.
        committed = payload[0][:-1]
        overlap = [v for v in committed if int(v[0]) * 1000 == last]
        if len(overlap) != 1:
            raise ValueError(f"Kraken REST tail does not overlap {asset} archive end")
        reference = rows[-1]
        for column, value in zip(("open", "high", "low", "close", "volume"), (overlap[0][1], overlap[0][2], overlap[0][3], overlap[0][4], overlap[0][6])):
            x, y = float(reference[column]), float(value)
            if not math.isclose(x, y, rel_tol=1e-8, abs_tol=1e-8):
                raise ValueError(f"Kraken {asset} archive/API overlap differs at {column}: {x} vs {y}")
        tail = []
        previous = last
        for bar in committed:
            stamp = int(bar[0]) * 1000
            if stamp <= last:
                continue
            if stamp != previous + H4 or stamp + H4 > now:
                raise ValueError(f"Kraken {asset} REST tail has a gap or unfinished bar")
            record = {"asset": asset, "open_time_ms": str(stamp), "open": bar[1], "high": bar[2],
                      "low": bar[3], "close": bar[4], "volume": bar[6]}
            o, h, l, c, v = (float(record[n]) for n in ("open", "high", "low", "close", "volume"))
            if (not all(math.isfinite(x) for x in (o, h, l, c, v)) or min(o, h, l, c) <= 0
                    or v < 0 or l > min(o, c) or h < max(o, c)):
                raise ValueError(f"Invalid Kraken {asset} REST OHLCV")
            tail.append(record)
            previous = stamp
        target = output / f"{asset}USD_240.csv"
        if target.exists():
            raise FileExistsError(f"Refusing to replace existing merged source: {target}")
        with target.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=("asset", "open_time_ms", "open", "high", "low", "close", "volume"))
            writer.writeheader()
            writer.writerows(rows)
            writer.writerows(tail)
        summary["assets"][asset] = {"status": "retrieved", "archive_rows": len(rows),
                                     "api_rows_added": len(tail), "first_open_ms": int(rows[0]["open_time_ms"]),
                                     "last_open_ms": previous, "merged_csv": target.name,
                                     "merged_sha256": sha256_path(target),
                                     "archive_csv_sha256": info["normalized_sha256"],
                                     "api_response_sha256": hashlib.sha256(response.content).hexdigest(),
                                     "api_request_pair": pair, "overlap_checked_ms": last,
                                     "excluded_uncommitted_open_ms": int(payload[0][-1][0]) * 1000,
                                     "historical_gap_count": info["gap_count"]}
        print(f"{asset}: appended {len(tail)} committed bars through {datetime.fromtimestamp((previous+H4)/1000, timezone.utc).isoformat()}", flush=True)
    destination = output / "provenance.json"
    if destination.exists():
        raise FileExistsError(f"Refusing to replace merged provenance: {destination}")
    destination.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"provenance": str(destination), "assets": {a: v["status"] for a, v in summary["assets"].items()}}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ls = commands.add_parser("archive-list", help="Inspect official full ZIP directory through HTTP ranges")
    ls.add_argument("--assets", nargs="+", default=ASSETS)
    fetch = commands.add_parser("archive-fetch", help="Fetch only selected official 240-minute members")
    fetch.add_argument("--assets", nargs="+", default=ASSETS)
    fetch.add_argument("--output", required=True)
    merge = commands.add_parser("rest-merge", help="Append committed Kraken REST tail to verified archive members")
    merge.add_argument("--source", required=True)
    merge.add_argument("--output", required=True)
    merge.add_argument("--assets", nargs="+", default=ASSETS)
    train = commands.add_parser("train", help="Train, export and evaluate frozen 4H candidates offline")
    train.add_argument("--data", required=True,
                       help="Verified Kraken archive-plus-REST directory with provenance.json")
    train.add_argument("--output", required=True,
                       help="Experiment directory outside the deployable model bundle")
    train.add_argument("--assets", nargs="+", choices=ASSETS, default=ASSETS)
    train.add_argument("--architectures", nargs="+", choices=(
        "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
        "grouped_attention_lstm_v1"), default=(
        "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
        "grouped_attention_lstm_v1"))
    train.add_argument("--seeds", nargs="+", type=int, default=(11, 23, 37))
    train.add_argument("--max-epochs", type=int, default=100)
    train.add_argument("--patience", type=int, default=10)
    train.add_argument("--batch-size", type=int, default=64)
    train.add_argument("--smoke-batches", type=int,
                       help="Bounded functional smoke run; writes only to output/smoke_artifacts")
    compact = commands.add_parser("compact-reports",
                                  help="Compact a completed pre-compression run without changing model identity")
    compact.add_argument("--output", required=True)
    compact.add_argument("--architectures", nargs="+", choices=(
        "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
        "grouped_attention_lstm_v1"), default=(
        "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
        "grouped_attention_lstm_v1"))
    folds = commands.add_parser("train-folds",
                                help="Run two preregistered expanding-window validation folds")
    folds.add_argument("--data", required=True)
    folds.add_argument("--output", required=True)
    attach = commands.add_parser("attach-folds",
                                 help="Attach complete fold metrics to immutable bundle reports")
    attach.add_argument("--output", required=True)
    verify = commands.add_parser("verify-runtime",
                                 help="Compare frozen predictions with the applied CPU candidate adapter")
    verify.add_argument("--output", required=True)
    verify.add_argument("--architectures", nargs="+", choices=(
        "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
        "grouped_attention_lstm_v1"), default=(
        "lstm_classifier_v1", "gru_classifier_v1", "cnn_lstm_classifier_v1",
        "grouped_attention_lstm_v1"))
    args = parser.parse_args(argv)
    if args.command == "archive-list":
        archive_list(args)
    elif args.command == "archive-fetch":
        archive_fetch(args)
    elif args.command == "rest-merge":
        rest_merge(args)
    elif args.command == "train":
        if (not 1 <= len(args.seeds) <= 3 or len(set(args.seeds)) != len(args.seeds)
                or any(seed < 0 for seed in args.seeds)
                or not 1 <= args.max_epochs <= 100 or not 1 <= args.patience <= 10
                or not 1 <= args.batch_size <= 512
                or args.smoke_batches is not None and not 1 <= args.smoke_batches <= 10):
            parser.error("Invalid bounded training configuration")
        from tools.candidate_train_pipeline import train_command
        train_command(args)
    elif args.command == "compact-reports":
        from tools.candidate_train_pipeline import compact_existing_reports
        compact_existing_reports(args.output, args.architectures)
    elif args.command == "train-folds":
        from tools.candidate_train_pipeline import train_folds_command
        train_folds_command(args.output, args.data)
    elif args.command == "attach-folds":
        from tools.candidate_train_pipeline import attach_fold_summaries
        attach_fold_summaries(args.output)
    elif args.command == "verify-runtime":
        from tools.candidate_train_pipeline import verify_runtime_predictions
        verify_runtime_predictions(args.output, args.architectures)


if __name__ == "__main__":
    main()
