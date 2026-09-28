"""Offline package, launcher, web asset, and compatible-upgrade integration checks."""
from dataclasses import asdict
import io
import json
from pathlib import Path
from zipfile import ZipFile
import unittest
from unittest.mock import Mock, patch

import adaptive_crypto_dashboard as d
from adaptive_crypto import cli, engine, ledger, market_data, runtime, strategy, web
from test_dashboard import ASSETS, TemporaryEngine, momentum_fixture, reclaim_fixture


class PackageIntegrationTests(TemporaryEngine):
    def test_original_entrypoint_exports_the_extracted_implementations(self):
        for name, module in [("Engine", engine), ("StateStore", ledger),
                             ("Kraken", market_data), ("DashboardRuntime", runtime),
                             ("reclaim_scan", strategy), ("create_app", web), ("main", cli)]:
            self.assertIs(getattr(d, name), getattr(module, name))

    def test_template_and_static_assets_resolve_from_package_directory(self):
        app = d.create_app(d.DashboardRuntime(ASSETS, self.rules, self.store))
        client = app.test_client()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'/static/dashboard.css', page.data)
        self.assertIn(b'/static/dashboard.js', page.data)
        for path, mime, content in [('/static/dashboard.css', 'text/css', b'.condition-box'),
                                    ('/static/dashboard.js', 'javascript', b'/api/chart/')]:
            with client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn(mime, response.content_type)
                self.assertIn(content, response.data)
        self.assertEqual(Path(app.root_path), Path(web.__file__).parent)

    def test_once_launcher_uses_selected_state_without_starting_workers(self):
        c4, c15, quote, now = reclaim_fixture()
        provider = Mock()
        provider.now.return_value = now
        provider.quotes.return_value = {"BTC/USD": quote}
        provider.candles.side_effect = lambda pair, interval, at: c4 if interval == d.H4 else c15
        settings = Path(self.temp.name)/"settings.json"
        settings.write_text(json.dumps({"assets": [{"name": "BTC", **ASSETS["BTC"]}],
                                        "strategy": asdict(self.rules)}), encoding="utf-8")
        state_path = Path(self.temp.name)/"once.json"
        with patch.object(runtime, "Kraken", return_value=provider), \
                patch("sys.argv", ["dashboard", "--once", "--settings", str(settings), "--state", str(state_path), "--state-backend", "json"]), \
                patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("threading.Thread.start", side_effect=AssertionError("--once must not start workers")):
            cli.main()
        result = json.loads(output.getvalue())
        self.assertIn("neural",result["assets"]["BTC"])
        self.assertNotIn("reclaim",result["assets"]["BTC"])
        self.assertTrue(state_path.with_suffix(".neural.json").exists())
        self.assertEqual(result["portfolio"]["active_positions"], 0)
        saved = d.StateStore(state_path, ASSETS, self.rules).snapshot()
        self.assertTrue(all(event["status"] == "queued" for event in saved["outbox"]))
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])


class UpgradeV92Tests(TemporaryEngine):
    def write_v92(self, state):
        state["fingerprint"] = d.fingerprint(ASSETS, self.rules, d.PREVIOUS_ENGINE_VERSION)
        d.atomic_json(self.path, state)
        return self.path.read_bytes()

    def test_v92_upgrade_preserves_failed_break_reset_and_momentum_watch(self):
        c4, c15, quote, now = reclaim_fixture()
        pending = d.reclaim_scan(c4, self.rules, now)["setup"]
        pending["confirmation_reset_ms"] = now
        momentum, _, _ = momentum_fixture()
        signal = d.momentum_scan(momentum, self.rules)["signal"]
        state = self.store.snapshot()
        state["assets"]["BTC"].update(pending=pending, reclaim_floor_ms=pending["reclaim_end"],
                                     momentum_watch={key: signal[key] for key in ("key", "stop", "end_ms")})
        original = self.write_v92(state)
        upgraded = d.StateStore(self.path, ASSETS, self.rules)
        self.assertEqual(upgraded.snapshot()["assets"], state["assets"])
        with ZipFile(self.path.parent/"backup"/"latest.zip") as backup:
            self.assertEqual(backup.read("state/"+self.path.name), original)
        d.Engine(ASSETS, self.rules, upgraded).evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(upgraded.snapshot()["assets"]["BTC"]["trades"], [])
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        self.assertEqual(restarted.snapshot(), upgraded.snapshot())
        self.assertEqual(len(list((self.path.parent/"backup").iterdir())), 1)

    def test_v92_upgrade_keeps_cash_trades_targets_and_delivery_records(self):
        self.qualify()
        state = self.store.snapshot()
        for trade in state["assets"]["BTC"]["trades"]:
            trade["engine"] = d.PREVIOUS_ENGINE_VERSION
            trade.pop("stop_history")
        self.write_v92(state)
        upgraded = d.StateStore(self.path, ASSETS, self.rules).snapshot()
        for key in ("cash", "realized_pnl", "assets", "outbox"):
            self.assertEqual(upgraded[key], state[key])
        self.assertEqual(upgraded["fingerprint"], d.fingerprint(ASSETS, self.rules))
        self.assertIn("not been recalculated", upgraded["warnings"][-1])

    def test_v92_upgrade_failure_keeps_original_ledger(self):
        original = self.write_v92(self.store.snapshot())
        with patch.object(ledger, "atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                d.StateStore(self.path, ASSETS, self.rules)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_stop_history_rolls_back_without_corrupting_state(self):
        self.qualify()
        before = self.store.snapshot()
        def corrupt(state):
            trade = state["assets"]["BTC"]["trades"][0]
            trade["stop_history"].append({"effective_ms": trade["opened_ms"]-1, "stop": trade["stop"]})
        with self.assertRaises(d.DataError):
            self.store.transaction(corrupt)
        self.assertEqual(self.store.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
