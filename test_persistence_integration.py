"""Domain parity and application integration on disposable SQLite state."""
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
from zipfile import ZipFile
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import requests
from adaptive_crypto import cli, runtime
from adaptive_crypto.core import Rules, DataError
from adaptive_crypto.engine import Engine
from adaptive_crypto.ledger import StateStore, atomic_json
from adaptive_crypto.smc_engine import SMCEngine
from adaptive_crypto.smc_ledger import SMCStore
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.persistence import SQLiteDatabase, wal_version_supported
from adaptive_crypto.state_paths import open_stores, StatePaths
from test_dashboard import ASSETS, reclaim_fixture
from test_smc_formulas import full_fixture


@unittest.skipUnless(wal_version_supported(sqlite3.sqlite_version_info), "Use a WAL-fixed SQLite runtime")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        offline = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Offline persistence tests"))
        offline.start()
        self.addCleanup(offline.stop)
        self.index = 0

    def make(self, name, rules, sqlite):
        self.index += 1
        base = self.root/f"case{self.index}.json"
        db = SQLiteDatabase(base.with_suffix(".sqlite3"), create=True) if sqlite else None
        if db:
            self.addCleanup(db.close)
        adapter = db.namespace(name) if db else None
        if name == "positions":
            store = PositionStore(base.with_suffix(".positions.json"), market_mode=rules.market_mode, backend=adapter)
        else:
            store = (SMCStore if name == "smc" else StateStore)(base, ASSETS, rules, backend=adapter)
        return store, db

    def test_legacy_real_formula_entry_and_stop_match_json(self):
        results = []
        for sqlite in (False, True):
            rules = Rules()
            store, _ = self.make("legacy", rules, sqlite)
            engine = Engine(ASSETS, rules, store)
            high, low, quote, now = reclaim_fixture()
            engine.evaluate("BTC", quote, high, low, now)
            trade = store.snapshot()["assets"]["BTC"]["trades"][0]
            engine.evaluate("BTC", {**quote, "bid": trade["stop"]-1, "ask": trade["stop"]-.9, "asof_ms": now+1000}, high, low, now+1000)
            results.append(store.snapshot())
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1]["assets"]["BTC"]["trades"][0]["status"], "stopped")

    def test_smc_real_formulas_limit_fill_target_both_sides_match_json(self):
        for side in ("long", "short"):
            results = []
            for sqlite in (False, True):
                rules = Rules(strategy_model="smc_video", market_mode="margin", smc_pivot_strength=1)
                store, _ = self.make("smc", rules, sqlite)
                engine = SMCEngine(ASSETS, rules, store)
                high, low = full_fixture(side)
                now = low[-1].end+1
                quote = {"bid": low[-1].c-.01, "ask": low[-1].c+.01, "asof_ms": now}
                with patch("uuid.uuid4", return_value=uuid.UUID(int=1)):
                    engine.evaluate("BTC", quote, high, low, now)
                order = store.snapshot()["assets"]["BTC"]["pending"]
                self.assertIsNotNone(order)
                midpoint = order["limit"]
                entry_quote = {"bid": midpoint if side == "short" else midpoint-.01,
                               "ask": midpoint if side == "long" else midpoint+.01, "asof_ms": now+1000}
                engine.evaluate("BTC", entry_quote, high, low, now+1000)
                trade = store.snapshot()["assets"]["BTC"]["trades"][0]
                self.assertEqual(trade["status"], "active")
                exit_quote = {"bid": trade["target"] if side == "long" else trade["target"]-.01,
                              "ask": trade["target"] if side == "short" else trade["target"]+.01, "asof_ms": now+2000}
                engine.evaluate("BTC", exit_quote, high, low, now+2000)
                results.append(store.snapshot())
            with self.subTest(side=side):
                self.assertEqual(results[0], results[1])
                self.assertEqual(results[1]["assets"]["BTC"]["trades"][0]["status"], "target")

    def test_holdings_targets_requests_and_buy_watches_match_json(self):
        results = []
        rules = Rules(strategy_model="smc_video", smc_pivot_strength=1)
        high, low = full_fixture("long")
        now = low[-1].end+1
        quote = {"bid": low[-1].c-.01, "ask": low[-1].c+.01, "asof_ms": now}
        for sqlite in (False, True):
            store, _ = self.make("positions", rules, sqlite)
            with patch("uuid.uuid4", side_effect=[uuid.UUID(int=i) for i in range(1, 30)]):
                first, created = store.open_position(ASSETS, "BTC", "long", 100, 2, "a"*32, now-1000)
                self.assertTrue(created)
                duplicate, created = store.open_position(ASSETS, "BTC", "long", 100, 2, "a"*32, now-1000)
                self.assertFalse(created)
                self.assertEqual(first, duplicate)
                store.monitor_take_profit("BTC", "BTC/USD", high, low, quote, rules, now)
                target = store.snapshot()["positions"][0]["take_profit"]["price"]
                store.open_buy_watch(ASSETS, "BTC", 100, 99, 1, "b"*32, now)
                watch = store.snapshot()["buy_watches"][0]
                store.cancel_buy_watch(watch["id"])
                store.close_position(first["id"], target, now+1000)
                results.append(store.snapshot())
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1]["positions"][0]["status"], "closed")

    def test_spot_correction_backs_up_current_database_not_stale_sidecar(self):
        store, db = self.make("positions", Rules(market_mode="margin"), True)
        position, _ = store.open_position(ASSETS, "BTC", "short", 100, 2, "a"*32, 100)
        before = store.snapshot()
        store.path.write_text("stale sidecar")
        store.market_mode = "spot"
        self.assertEqual(store.correct_spot_buys(200), [position["id"]])
        backup = Path(store.snapshot()["positions"][0]["spot_correction"]["backup"])
        with ZipFile(backup) as archive:
            self.assertEqual(json.loads(archive.read("metadata/documents.json"))["documents"]["positions"], before)
        self.assertEqual(store.path.read_text(), "stale sidecar")
        self.assertEqual(store.correct_spot_buys(300), [])

    def test_settings_backup_and_preferences_preserve_frozen_smc_order(self):
        rules = Rules(strategy_model="smc_video", market_mode="margin", smc_pivot_strength=1)
        store, db = self.make("smc", rules, True)
        high, low = full_fixture("long")
        now = low[-1].end+1
        SMCEngine(ASSETS, rules, store).evaluate("BTC", {"bid": low[-1].c-.01, "ask": low[-1].c+.01, "asof_ms": now}, high, low, now)
        order = store.snapshot()["assets"]["BTC"]["pending"]
        path, dbpath = store.path, db.path
        db.close()
        with SQLiteDatabase(dbpath) as db:
            prefs = replace(rules, smc_gex_alignment_bps=20)
            store = SMCStore(path, ASSETS, prefs, backend=db.namespace("smc"))
            self.assertEqual(store.snapshot()["assets"]["BTC"]["pending"], order)
            before = store.snapshot()
        with SQLiteDatabase(dbpath) as db:
            changed = SMCStore(path, ASSETS, replace(prefs, fee_rate=.002), backend=db.namespace("smc"))
            self.assertIsNone(changed.snapshot()["assets"]["BTC"]["pending"])
        with ZipFile(self.root/"backup"/"latest.zip") as backup:
            self.assertEqual(json.loads(backup.read("metadata/documents.json"))["documents"]["smc"], before)

    def test_runtime_requires_same_database_holdings(self):
        store, db = self.make("legacy", Rules(), True)
        with self.assertRaises(DataError):
            runtime.DashboardRuntime(ASSETS, Rules(), store)
        holdings, _ = self.make("positions", Rules(), False)
        with self.assertRaises(DataError):
            runtime.DashboardRuntime(ASSETS, Rules(), store, position_store=holdings)

    def test_sqlite_once_and_web_use_shared_state_without_workers(self):
        high, low, quote, now = reclaim_fixture()
        provider = Mock()
        provider.now.return_value = now
        provider.quotes.return_value = {"BTC/USD": quote}
        provider.candles.side_effect = lambda pair, interval, at: high if interval == 14400000 else low
        settings = self.root/"settings.json"
        atomic_json(settings, {"assets": [{"name": name, **cfg} for name, cfg in ASSETS.items()], "strategy": asdict(Rules())})
        base = self.root/"once.json"
        with patch.object(runtime, "Kraken", return_value=provider), patch("sys.argv", ["dashboard", "--once", "--settings", str(settings), "--state", str(base)]), patch("sys.stdout", new_callable=io.StringIO) as output, patch("threading.Thread.start", side_effect=AssertionError("No worker in once mode")):
            cli.main()
        self.assertEqual(json.loads(output.getvalue())["portfolio"]["active_positions"], 0)
        self.assertIn("neural",json.loads(output.getvalue())["assets"]["BTC"])
        with open_stores(base, ASSETS, Rules()) as (paper, holdings):
            view = runtime.DashboardRuntime(ASSETS, Rules(), paper, position_store=holdings, provider=provider)
            from adaptive_crypto.web import create_app
            client = create_app(view).test_client()
            self.assertEqual(client.get("/api/state").status_code, 200)
            self.assertEqual(client.get("/").status_code, 200)
        self.assertFalse(base.exists())

    def test_shutdown_joins_owned_threads_before_store_close(self):
        base = self.root/"shutdown.json"
        with open_stores(base, ASSETS, Rules()) as (paper, positions):
            fake = Mock()
            import threading
            fake.stop = threading.Event()
            fake.positions = positions
            fake.run.side_effect = lambda: fake.stop.wait(5)
            args = Mock(once=False, host="127.0.0.1", port=5000)
            with patch.object(cli, "DashboardRuntime", return_value=fake), patch.object(cli, "create_app"), patch.object(cli, "worker", side_effect=lambda store, kind, stop: stop.wait(5)), patch("werkzeug.serving.make_server", return_value=Mock(serve_forever=Mock(side_effect=RuntimeError("server stopped")))), self.assertRaises(RuntimeError):
                cli._run(args, ASSETS, Rules(), 15, paper, positions)
            self.assertTrue(fake.stop.is_set())
            self.assertFalse(any(t.name in {"market-scan", "telegram", "ai", "holding-alerts"} for t in threading.enumerate()))
            paper.transaction(lambda d: d["warnings"].append("still owned until exit"))
        with self.assertRaises(DataError):
            paper.snapshot()

    def test_fresh_initialization_failure_does_not_publish_partial_database(self):
        base = self.root/"fresh.json"
        original = SQLiteDatabase._write_rows
        def fail(db, name, *args):
            result = original(db, name, *args)
            if name == "positions":
                raise OSError("holdings initialization failed")
            return result
        with patch.object(SQLiteDatabase, "_write_rows", fail), self.assertRaises(OSError):
            with open_stores(base, ASSETS, Rules()):
                pass
        self.assertFalse(base.with_suffix(".sqlite3").exists())
        with open_stores(base, ASSETS, Rules()) as (paper, positions):
            self.assertEqual(positions.snapshot()["positions"], [])

    def test_smc_restart_accepts_outbox_without_trade_payload(self):
        from adaptive_crypto.ledger import queue_event
        rules = Rules(strategy_model="smc_video")
        store, db = self.make("smc", rules, True)
        store.transaction(lambda doc: queue_event(doc, "generic", "telegram", "stub", 1))
        path, dbpath = store.path, db.path
        db.close()
        with SQLiteDatabase(dbpath) as reopened:
            restored = SMCStore(path, ASSETS, rules, backend=reopened.namespace("smc"))
            self.assertIsNone(restored.snapshot()["outbox"][0]["payload"])
