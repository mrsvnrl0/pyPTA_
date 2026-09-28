"""Offline end-to-end persistence benchmark. Every ledger is disposable."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adaptive_crypto.core import Rules
from adaptive_crypto.ledger import StateStore, atomic_json, validate_state
from adaptive_crypto.engine import Engine
from adaptive_crypto.notifications import dispatch_once
from adaptive_crypto.persistence import SQLiteDatabase, check_sqlite_runtime
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.runtime import DashboardRuntime
from test_dashboard import reclaim_fixture

ASSETS = {name: {"symbol": name+"/USD", "price_decimals": 2} for name in ("BTC", "ETH", "SOL")}


class OriginalJsonStore(StateStore):
    """Exact pre-change transaction algorithm; constructor uses current defaults."""
    def transaction(self, operation):
        with self.lock:
            proposed = copy.deepcopy(self.data)
            result = operation(proposed)
            validate_state(proposed, self.assets, self.rules)
            atomic_json(self.path, proposed)
            self.data = proposed
            return result


def run_case(backend, history, repetitions, baseline_cls):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        path = root/"study.json"
        rules = Rules()
        db = SQLiteDatabase(root/"study.sqlite3", create=True) if backend == "sqlite" else None
        try:
            started = time.perf_counter()
            cls = baseline_cls if backend == "original-json" else StateStore
            store = cls(path, ASSETS, rules, **({"backend": db.namespace("legacy")} if db else {}))
            holdings = PositionStore(root/"study.positions.json", backend=db.namespace("positions") if db else None)
            startup_ms = (time.perf_counter()-started)*1000
            high, low, quote, now = reclaim_fixture()
            engine = Engine(ASSETS, rules, store)
            for name in ASSETS:
                engine.evaluate(name, quote, high, low, now)
            def seed(document):
                document["outbox"] = [{"id": str(i), "kind": "telegram", "status": "sent", "text": "Archived signal "+str(i),
                    "created_ms": i, "attempts": 1, "retry_ms": 0, "error": None,
                    "payload": {"measurements": [{"price": 100.12345, "source": "completed candle", "passed": True} for _ in range(3)]}}
                    for i in range(history)]
            store.transaction(seed)
            provider = Mock()
            provider.now.return_value = now
            provider.quotes.return_value = {cfg["symbol"]: quote for cfg in ASSETS.values()}
            provider.candles.side_effect = lambda pair, interval, at: high if interval == 14400000 else low
            runtime = DashboardRuntime(ASSETS, rules, store, provider=provider, position_store=holdings)
            counter = [0]
            def event_change():
                counter[0] += 1
                store.transaction(lambda d: d["outbox"][0].update(error=str(counter[0])))
            def trade_change():
                store.transaction(lambda d: d["assets"]["BTC"]["trades"][0].update(tracking_gap=not d["assets"]["BTC"]["trades"][0]["tracking_gap"]))
            def prepare_claim():
                store.transaction(lambda d: d["outbox"][-1].update(status="queued", retry_ms=0))
            cases = {"snapshot": (store.snapshot, None), "no_op": (lambda: store.transaction(lambda d: None), None),
                     "single_event": (event_change, None), "single_trade": (trade_change, None),
                     "claim_finish": (lambda: dispatch_once(store, "telegram", lambda event: {"status": "sent"}, now), prepare_claim),
                     "three_asset_scan": (runtime.scan_once, None)}
            results = {}
            for name, (operation, prepare) in cases.items():
                if prepare:
                    prepare()
                operation()  # Warm validation, JSON and filesystem caches.
                if db:
                    db.checkpoint()
                before_size = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
                durations, writes, payload_bytes = [], [], []
                total_start = time.perf_counter()
                for _ in range(repetitions):
                    if prepare:
                        prepare()
                    measured_bytes, commits = [0], [0]
                    if not db:
                        writer_globals = (store.transaction.__func__.__globals__ if backend == "original-json" else store._backend.writer.__globals__)
                        original_write = writer_globals["atomic_json"]
                        def count_json(path, document):
                            original_write(path, document)
                            commits[0] += 1
                            measured_bytes[0] += Path(path).stat().st_size
                    if db:
                        original_rows = db._write_rows
                        def count_rows(namespace, old, new):
                            measured_bytes[0] += sum(len(value[1].encode()) for key, value in new.items() if old.get(key) != value)
                            return original_rows(namespace, old, new)
                    changes = db._connection.total_changes if db else 0
                    with (patch.dict(writer_globals, {"atomic_json": count_json}) if not db else patch.object(db, "_write_rows", side_effect=count_rows)):
                        start = time.perf_counter()
                        operation()
                        durations.append((time.perf_counter()-start)*1000)
                    if db:
                        payload_bytes.append(measured_bytes[0])
                        writes.append(db._connection.total_changes-changes)
                    else:
                        payload_bytes.append(measured_bytes[0])
                        writes.append(commits[0])
                checkpoint_start = time.perf_counter()
                if db:
                    db.checkpoint()
                checkpoint_ms = (time.perf_counter()-checkpoint_start)*1000
                results[name] = {"median_ms": round(statistics.median(durations), 3),
                    "p95_ms": round(sorted(durations)[min(len(durations)-1, int(len(durations)*.95))], 3),
                    "mean_rows_or_json_commits": round(statistics.mean(writes), 2),
                    "mean_changed_payload_or_json_bytes": round(statistics.mean(payload_bytes)),
                    "workload_including_preparation_checkpoint_ms": round((time.perf_counter()-total_start)*1000, 3),
                    "final_checkpoint_ms": round(checkpoint_ms, 3),
                    "file_size_growth_after_checkpoint": sum(p.stat().st_size for p in root.iterdir() if p.is_file())-before_size}
            return {"backend": backend, "history_events": history, "startup_ms": round(startup_ms, 3), "measurements": results}, store.snapshot()
        finally:
            if db:
                db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=7)
    parser.add_argument("--histories", nargs="+", type=int, default=[10, 1000, 10000])
    parser.add_argument("--baseline-ledger", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repetitions < 2 or any(n < 1 for n in args.histories):
        parser.error("Use at least two repetitions and positive history sizes")
    check_sqlite_runtime()
    baseline_cls = OriginalJsonStore
    if args.baseline_ledger:
        spec = importlib.util.spec_from_file_location("adaptive_crypto._benchmark_original_ledger", args.baseline_ledger.resolve())
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        baseline_cls = module.StateStore
    import sqlite3
    import requests
    report = {"python": sys.version, "sqlite": sqlite3.sqlite_version, "repetitions": args.repetitions,
              "durability": "JSON fsync+replace; SQLite WAL synchronous=FULL", "physical_disk_writes_measured": False,
              "byte_metric": "SQLite changed record payloads (excludes namespace metadata, pages and checkpoint amplification); JSON complete committed file bytes. Instrumentation included in timings.",
              "baseline": str(args.baseline_ledger) if args.baseline_ledger else "pre-change transaction algorithm reproduced in OriginalJsonStore",
              "cases": []}
    with patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Benchmark must stay offline")):
        for history in args.histories:
            snapshots = []
            for backend in ("original-json", "json", "sqlite"):
                result, document = run_case(backend, history, args.repetitions, baseline_cls)
                report["cases"].append(result)
                snapshots.append(document)
                print(f"{backend} history={history}: event median={result['measurements']['single_event']['median_ms']} ms", flush=True)
            if any(doc != snapshots[0] for doc in snapshots[1:]):
                raise AssertionError("Benchmark backends produced different final states")
    atomic_json(args.output, report)
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
