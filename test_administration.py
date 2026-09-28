"""Settings reload and purge on disposable JSON/SQLite stores; no external calls."""
from dataclasses import asdict, replace
import json
from pathlib import Path
from zipfile import ZipFile
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import requests
from adaptive_crypto.administration import apply_settings, purge_database
from adaptive_crypto.core import Rules
from adaptive_crypto.ledger import atomic_json, queue_event, StateStore
from adaptive_crypto.notifications import dispatch_once
from adaptive_crypto.persistence import SQLiteDatabase, wal_version_supported
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.web import create_app
from test_dashboard import ASSETS
from test_display_formatting import DashboardHTML
from test_smc_formulas import full_fixture


class AdministrationTests(unittest.TestCase):
    backend = "json"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / "custom-settings.json"
        self.rules = Rules(strategy_model="smc_video", smc_pivot_strength=1, fee_rate=.002)
        self.write_settings(self.rules)
        self.owner = open_stores(self.root / "study.json", ASSETS, self.rules, self.backend)
        self.paper, self.holdings = self.owner.__enter__()
        self.addCleanup(self.owner.__exit__, None, None, None)
        self.provider = Mock()
        self.provider.now.return_value = 1800000000000
        self.provider.quotes.return_value = {}
        self.provider.candles.return_value = []
        self.runtime = DashboardRuntime(ASSETS, self.rules, self.paper, position_store=self.holdings,
                                        provider=self.provider, settings_path=self.settings)
        self.chart_provider = Mock()
        self.chart_provider.get.side_effect = RuntimeError("Offline chart")
        self.app = create_app(self.runtime, chart_provider=self.chart_provider)
        self.client = self.app.test_client()
        self.refresh_token()
        offline = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Offline tests"))
        offline.start()
        self.addCleanup(offline.stop)

    def write_settings(self, rules, assets=ASSETS, refresh=15):
        atomic_json(self.settings, {"assets": [{"name": n, **cfg} for n, cfg in assets.items()],
                                    "strategy": asdict(rules), "refresh_seconds": refresh})

    def refresh_token(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.session_transaction() as session:
            self.token = session["csrf_token"]

    def post(self, action, body=None):
        return self.client.post("/api/"+action, json={} if body is None else body,
                                headers={"X-CSRF-Token": self.token})

    def seed(self, fill=True):
        self.holdings.open_position(ASSETS, "BTC", "long", 100, 1, "a"*32, 100)
        closed, _ = self.holdings.open_position(ASSETS, "BTC", "long", 100, 1, "b"*32, 100)
        self.holdings.close_position(closed["id"], 110, 200)
        self.holdings.open_buy_watch(ASSETS, "BTC", 100, 99, 1, "c"*32, 300)
        high, low = full_fixture("long")
        now = low[-1].end+1
        quote = {"bid": low[-1].c-.01, "ask": low[-1].c+.01, "asof_ms": now}
        self.runtime.engine.evaluate("BTC", quote, high, low, now)
        order = self.paper.snapshot()["assets"]["BTC"]["pending"]
        self.assertIsNotNone(order)
        if fill:
            quote = {"bid": order["limit"]-.01, "ask": order["limit"], "asof_ms": now+1000}
            self.runtime.engine.evaluate("BTC", quote, high, low, now+1000)
            self.assertEqual(len(self.paper.snapshot()["assets"]["BTC"]["trades"]), 1)

    def documents(self):
        return {"smc": self.paper.snapshot(), "positions": self.holdings.snapshot()}

    def backup_documents(self, response):
        path = Path(response.get_json()["backup"])
        self.assertTrue(path.is_file())
        with ZipFile(path) as backup:
            return json.loads(backup.read("metadata/documents.json"))["documents"]

    def test_controls_are_on_settings_page_and_render_valid_html(self):
        home = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('id="settings-database"', home)
        self.assertNotIn('id="model-settings"', home)
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertFalse(DashboardHTML(html).errors)
        for label in ("Apply saved settings", "Purge database", "Back up and purge", str(self.settings), "Restart dashboard"):
            self.assertIn(label, html)

    def test_purge_requires_csrf_confirmation_and_fresh_page(self):
        self.seed()
        before = self.documents()
        self.assertEqual(self.client.post("/api/database/purge", json={"confirm": "purge"}).status_code, 400)
        self.assertEqual(self.post("database/purge").status_code, 400)
        self.assertEqual(self.post("database/purge", {"confirm": "wrong"}).status_code, 400)
        self.assertEqual(self.documents(), before)
        self.assertEqual(self.post("database/purge", {"confirm": "purge"}).status_code, 200)
        self.assertEqual(self.post("database/purge", {"confirm": "purge"}).status_code, 400)
        self.assertEqual(self.post("positions", {"asset":"BTC", "side":"long", "entry":100,
            "quantity":1, "request_id":"a"*32}).status_code, 400)
        self.assertEqual(self.holdings.snapshot()["positions"], [])

    def test_purge_backs_up_every_record_then_pauses_until_apply(self):
        self.seed()
        before = self.documents()
        response = self.post("database/purge", {"confirm":"purge"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.backup_documents(response), before)
        state = self.paper.snapshot()
        self.assertEqual(state["cash"], self.rules.paper_equity)
        self.assertEqual(state["outbox"], [])
        self.assertEqual(state["assets"]["BTC"]["trades"], [])
        self.assertIsNone(state["assets"]["BTC"]["pending"])
        for key in ("positions", "buy_watches", "outbox"):
            self.assertEqual(self.holdings.snapshot()[key], [])
        self.assertEqual(self.holdings.snapshot()["watches"], {})
        self.runtime.scan_once()
        self.provider.quotes.assert_called_once()
        self.assertFalse(self.paper.snapshot()["assets"]["BTC"]["trades"])
        self.assertTrue(self.client.get("/health").get_json()["paused"])
        restarted = DashboardRuntime(ASSETS, self.rules, self.paper, position_store=self.holdings)
        self.assertTrue(restarted.paused)
        self.write_settings(replace(self.rules, paper_equity=2000, smc_entry_method="aggressive"))
        self.refresh_token()
        response = self.post("settings/apply")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(self.runtime.paused)
        self.assertEqual(self.paper.snapshot()["cash"], 2000)
        self.assertEqual(self.runtime.rules.smc_entry_method, "aggressive")
        self.assertNotIn("monitoring_paused", self.paper.snapshot())

    def test_apply_updates_engine_assets_charts_refresh_and_preserves_positions(self):
        self.seed()
        before = self.documents()
        rules = replace(self.rules, smc_setup_minutes=15, smc_entry_minutes=1, fee_rate=.004)
        assets = {**ASSETS, "ETH":{"symbol":"ETH/USD", "price_decimals":4}}
        self.write_settings(rules, assets, refresh=25)
        self.runtime.data = {"BTC":{"old":True}}
        response = self.post("settings/apply")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.backup_documents(response), before)
        self.assertEqual(self.paper.snapshot()["assets"]["BTC"]["trades"], before["smc"]["assets"]["BTC"]["trades"])
        self.assertEqual(self.holdings.snapshot()["positions"], before["positions"]["positions"])
        self.assertEqual(self.holdings.snapshot()["buy_watches"], before["positions"]["buy_watches"])
        self.assertEqual(self.runtime.rules, rules)
        self.assertEqual(self.runtime.engine.rules, rules)
        self.assertEqual(self.runtime.assets, assets)
        self.assertEqual(self.runtime.refresh, 25)
        self.assertEqual(self.runtime.data, {})
        self.assertEqual(self.client.get("/api/chart/ETH").get_json()["interval_ms"], 14400000)
        self.assertEqual(self.runtime.low_interval, 60000)
        self.assertIn("ETH", self.runtime.gex.assets)
        self.assertEqual(self.client.get("/").status_code, 200)
        if self.backend == "sqlite":
            self.assertEqual(self.paper._backend.database.information()["contexts"]["smc"]["rules"], asdict(rules))

    def test_apply_cancels_pending_limit_and_queued_delivery(self):
        self.seed(fill=False)
        self.paper.transaction(lambda doc: queue_event(doc, "old", "telegram", "Old settings", 1))
        self.write_settings(replace(self.rules, fee_rate=.003))
        response = self.post("settings/apply")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIsNone(self.paper.snapshot()["assets"]["BTC"]["pending"])
        self.assertTrue(all(e["status"] != "queued" for e in self.paper.snapshot()["outbox"]))

    def test_invalid_missing_and_unsupported_settings_leave_live_state_unchanged(self):
        self.seed()
        before, generation = self.documents(), self.runtime.generation
        for raw in ('{broken', '{"strategy":{"fee_rate":-1}}', '{"strategy":{"strategy_model":"legacy"}}'):
            self.settings.write_text(raw)
            self.assertEqual(self.post("settings/apply").status_code, 400)
            self.assertEqual(self.documents(), before)
            self.assertEqual(self.runtime.generation, generation)
            self.assertEqual(self.runtime.rules, self.rules)
        self.settings.unlink()
        self.assertEqual(self.post("settings/apply").status_code, 400)
        self.assertEqual(self.documents(), before)

    def test_backup_failure_does_not_clear_or_pause(self):
        self.seed()
        before = self.documents()
        with patch("adaptive_crypto.administration._backup", side_effect=OSError("Disk full")):
            response = self.post("database/purge", {"confirm":"purge"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.documents(), before)
        self.assertFalse(self.runtime.paused)

    def test_commit_failure_rolls_back_all_stores_and_runtime(self):
        self.seed()
        before, generation = self.documents(), self.runtime.generation
        if self.backend == "sqlite":
            db = self.paper._backend.database
            original = db._write_rows
            def fail(name, *args):
                if name == "positions":
                    raise OSError("Injected write failure")
                return original(name, *args)
            target = patch.object(db, "_write_rows", side_effect=fail)
        else:
            backend = self.holdings._backend
            original = backend.save
            count = 0
            def fail(document):
                nonlocal count
                count += 1
                if count == 1:
                    raise OSError("Injected write failure")
                return original(document)
            target = patch.object(backend, "save", side_effect=fail)
        with target:
            response = self.post("database/purge", {"confirm":"purge"})
        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(self.documents(), before)
        self.assertEqual(self.runtime.generation, generation)
        self.assertFalse(self.runtime.paused)

    def test_purge_waits_for_scan_and_does_not_recreate_records(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        generation = self.runtime.generation
        errors = []
        def scan():
            entered.set()
            release.wait(3)
            self.holdings.open_position(ASSETS, "BTC", "long", 100, 1, uuid.uuid4().hex, 100)
        def purge():
            try:
                purge_database(self.runtime, generation)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with patch.object(self.runtime, "_scan_once", side_effect=scan):
            scanner = threading.Thread(target=self.runtime.scan_once)
            scanner.start()
            self.assertTrue(entered.wait(1))
            maintenance = threading.Thread(target=purge)
            maintenance.start()
            self.assertFalse(finished.wait(.05))
            release.set()
            scanner.join(3)
            maintenance.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(self.holdings.snapshot()["positions"], [])

    def test_purge_waits_for_inflight_notification_to_finish(self):
        self.paper.transaction(lambda doc: queue_event(doc, "delivery", "telegram", "Fixture", 1))
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def sender(_):
            entered.set()
            release.wait(3)
            return {"status":"sent"}
        def send():
            try:
                dispatch_once(self.paper, "telegram", sender, 100)
            except Exception as exc:
                errors.append(exc)
        def purge():
            try:
                purge_database(self.runtime, self.runtime.generation)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        delivery = threading.Thread(target=send)
        delivery.start()
        self.assertTrue(entered.wait(1))
        maintenance = threading.Thread(target=purge)
        maintenance.start()
        self.assertFalse(finished.wait(.05))
        release.set()
        delivery.join(3)
        maintenance.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(self.paper.snapshot()["outbox"], [])


@unittest.skipUnless(wal_version_supported(sqlite3.sqlite_version_info), "Use a WAL-fixed SQLite runtime")
class SQLiteAdministrationTests(AdministrationTests):
    backend = "sqlite"

    def test_purge_also_clears_inactive_strategy_namespace(self):
        db = self.paper._backend.database
        legacy = StateStore(self.root/"study.json", ASSETS, Rules(), backend=db.namespace("legacy"))
        legacy.transaction(lambda doc: queue_event(doc, "old-legacy", "telegram", "Old", 1))
        response = self.post("database/purge", {"confirm":"purge"})
        self.assertEqual(response.status_code, 200)
        backup = self.backup_documents(response)
        self.assertEqual(backup["legacy"]["outbox"][0]["id"], "old-legacy")
        self.assertEqual(db.documents()["legacy"]["outbox"], [])


if __name__ == "__main__":
    unittest.main()
