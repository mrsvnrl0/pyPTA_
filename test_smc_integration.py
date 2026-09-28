"""Offline integration of the selected video model, holdings, charts and launcher."""
from dataclasses import asdict, replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import requests
import adaptive_crypto_dashboard as d
from adaptive_crypto import cli, runtime as runtime_module
from adaptive_crypto.core import H4, M5, M30
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.smc_ledger import SMCStore
from test_dashboard import ASSETS, bars
from test_display_formatting import DashboardHTML
from test_smc_formulas import full_fixture


def structure_bars():
    values = [100, 101, 105, 102, 101, 99, 98, 100, 101, 100, 99, 106, 105, 104, 103, 99, 95, 94]
    start = 1780272000000
    return [d.Candle(start+i*M5, value, value+.5, value-.5, value, 100, M5) for i, value in enumerate(values)]


class SMCIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.rules = replace(d.Rules(), strategy_model="smc_video")
        self.store = SMCStore(self.path/"paper.smc.json", ASSETS, self.rules)
        self.positions = PositionStore(self.path/"paper.positions.json")
        self.provider = Mock()
        self.runtime = d.DashboardRuntime(ASSETS, self.rules, self.store, provider=self.provider, position_store=self.positions)
        self.low = structure_bars()
        self.now = self.low[-1].end+2
        self.high = bars(30, M30, (self.now//M30-30)*M30)
        self.provider.now.return_value = self.now
        self.provider.quotes.return_value = {"BTC/USD": {"bid": 94, "ask": 94.1, "last": 94, "volume24_base": 100, "asof_ms": self.now}}
        self.provider.candles.side_effect = lambda pair, interval, at: self.high if interval == M30 else self.low
        network = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Offline tests"))
        network.start()
        self.addCleanup(network.stop)

    def open(self, now):
        return self.positions.open_position(ASSETS, "BTC", "long", 100, 2, uuid.uuid4().hex, now)[0]

    def test_runtime_uses_only_selected_30m_and_5m_feeds(self):
        self.runtime.scan_once()
        intervals = {call.args[1] for call in self.provider.candles.call_args_list}
        self.assertEqual(intervals, {H4, M30, M5})
        snapshot = self.runtime.snapshot()
        self.assertEqual((snapshot["setup_minutes"], snapshot["entry_minutes"]), (30, 5))
        self.assertEqual(snapshot["engine"], "smc-video-v1")
        self.assertNotIn("reclaim", snapshot["data"]["BTC"])
        self.assertIn("smc_short", snapshot["data"]["BTC"])

    def test_structural_alerts_track_both_directions_once_and_survive_restart(self):
        opened = self.low[10].end+2
        self.open(opened)
        self.positions.monitor("BTC", "BTC/USD", self.low[:11], self.rules, opened)
        self.positions.monitor("BTC", "BTC/USD", self.low, self.rules, self.now)
        events = self.positions.snapshot()["outbox"]
        self.assertEqual([e["direction"] for e in events], ["bullish", "bearish"])
        self.assertTrue(all("5M momentum" in e["text"] and "structure break" in e["text"] for e in events))
        self.assertTrue(all("ROC(" not in e["text"] for e in events))
        restarted = PositionStore(self.positions.path)
        restarted.monitor("BTC", "BTC/USD", self.low, self.rules, self.now)
        self.assertEqual(restarted.snapshot()["outbox"], events)

    def test_existing_holding_rebaselines_quietly_when_formula_and_timeframe_change(self):
        old = bars(69, d.M15, (self.now//d.M15-69)*d.M15)
        position = self.open(self.now)
        self.positions.monitor("BTC", "BTC/USD", old, d.Rules(), self.now)
        self.assertEqual(self.positions.snapshot()["watches"]["BTC"]["reading"]["interval_ms"], d.M15)
        self.positions.monitor("BTC", "BTC/USD", self.low, self.rules, self.now)
        snapshot = self.positions.snapshot()
        self.assertEqual(snapshot["positions"][0], position)
        self.assertEqual(snapshot["watches"]["BTC"]["reading"]["basis"], "structure")
        self.assertEqual(snapshot["watches"]["BTC"]["reading"]["interval_ms"], M5)
        self.assertEqual(snapshot["outbox"], [])

    def test_structural_direction_survives_break_candle_leaving_rolling_history(self):
        self.open(self.now)
        self.positions.monitor("BTC", "BTC/USD", self.low, self.rules, self.now)
        prior = self.positions.snapshot()["watches"]["BTC"]["reading"]
        flat = [d.Candle(self.low[-1].t+i*M5, 94, 94.5, 93.5, 94, 100, M5) for i in range(1, 21)]
        self.positions.monitor("BTC", "BTC/USD", self.low+flat[:10], self.rules, flat[9].end+2)
        restarted = PositionStore(self.positions.path)
        restarted.monitor("BTC", "BTC/USD", flat, self.rules, flat[-1].end+2)
        saved = restarted.snapshot()
        reading = saved["watches"]["BTC"]["reading"]
        self.assertEqual((reading["direction"], reading["break_level"], reading["break_ms"]),
                         ("bearish", prior["break_level"], prior["break_ms"]))
        self.assertEqual(saved["outbox"], [])

    def test_stale_or_forming_5m_candles_do_not_generate_direction_alerts(self):
        opened = self.low[10].end+2
        self.open(opened)
        self.positions.monitor("BTC", "BTC/USD", self.low[:11], self.rules, opened)
        baseline = self.positions.snapshot()["watches"]["BTC"]["last_bar_ms"]
        for candles, now in ((self.low[:11], self.now), (self.low, self.low[-1].t+1000)):
            self.positions.monitor("BTC", "BTC/USD", candles, self.rules, now)
            self.assertEqual(self.positions.snapshot()["watches"]["BTC"]["last_bar_ms"], baseline)
            self.assertEqual(self.positions.snapshot()["outbox"], [])

    def test_5m_holdings_continue_when_the_setup_feed_is_down(self):
        opened = self.low[10].end+2
        self.open(opened)
        self.positions.monitor("BTC", "BTC/USD", self.low[:11], self.rules, opened)
        self.provider.candles.side_effect = lambda pair, interval, at: [] if interval == M30 else self.low
        self.runtime.scan_once()
        snapshot = self.runtime.snapshot()
        self.assertIn("30m", snapshot["data"]["BTC"]["errors"])
        self.assertEqual(snapshot["holdings"]["watches"]["BTC"]["reading"]["direction"], "bearish")
        self.assertEqual(snapshot["holdings"]["outbox"], [])  # 5M momentum is display context only.
        self.assertTrue(snapshot["market"]["BTC"]["structure"]["error"])  # No valid 4H feed.

    def test_html_renders_smc_and_structural_readings_without_old_entry_formulas(self):
        self.open(self.now)
        self.runtime.scan_once()
        client = d.create_app(self.runtime).test_client()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        parsed = DashboardHTML(page.get_data(as_text=True))
        self.assertFalse(parsed.errors)
        for text in ("SMC price guide", "Bullish SMC", "Bearish SMC"):
            self.assertIn(text, parsed.text)
        for text in ("4H reclaim", "Reclaim / retest", "ROC(12)", "TP1", "TP2"):
            self.assertNotIn(text, parsed.text)

    def test_chart_request_and_fallback_use_setup_timeframe(self):
        from test_neural_strategy import bars as neural_bars
        self.provider.candles.side_effect = lambda pair,interval,at: neural_bars(self.now,160,H4) if interval == H4 else self.high if interval == M30 else self.low
        self.runtime.scan_once()
        provider = Mock()
        provider.get.side_effect = RuntimeError("Offline chart")
        client = d.create_app(self.runtime, chart_provider=provider).test_client()
        response = client.get("/api/chart/BTC")
        self.assertEqual(response.get_json()["interval_ms"], H4)
        self.assertTrue(response.get_json()["candles"])
        self.assertEqual(provider.get.call_args.kwargs["interval"], 240)

    def test_real_formula_to_pending_fill_and_html_for_both_sides(self):
        for side, midpoint in (("long", 115.5), ("short", 134.5)):
            with self.subTest(side=side):
                high, low = full_fixture(side)
                now = low[-1].end+1
                rules = replace(self.rules, smc_pivot_strength=1, market_mode="margin")
                store = SMCStore(self.path/(side+".smc.json"), ASSETS, rules)
                app_runtime = d.DashboardRuntime(ASSETS, rules, store, provider=Mock(), position_store=self.positions)
                quote = {"bid": low[-1].c-.01, "ask": low[-1].c+.01, "last": low[-1].c,
                         "asof_ms": now, "volume24_base": 100}
                row = app_runtime.engine.evaluate("BTC", quote, high, low, now)
                self.assertEqual(row["smc_"+side]["status"], "PAPER LIMIT WORKING")
                self.assertEqual(row["smc_"+side]["order"]["limit"], midpoint)
                app_runtime.data["BTC"] = row
                page = d.create_app(app_runtime).test_client().get("/")
                self.assertEqual(page.status_code, 200)
                self.assertNotIn("PAPER LIMIT WORKING", page.get_data(as_text=True))
                self.assertIn("SMC price guide",page.get_data(as_text=True))
                quote.update(bid=midpoint-.01, ask=midpoint+.01, asof_ms=now+1000)
                quote["ask" if side == "long" else "bid"] = midpoint
                row = app_runtime.engine.evaluate("BTC", quote, high, low, now+1000)
                self.assertEqual(row["smc_"+side]["status"], "ACTIVE PAPER TRADE")
                trade = store.snapshot()["assets"]["BTC"]["trades"][0]
                self.assertEqual((trade["entry"], trade["side"]), (midpoint, side))
                app_runtime.data["BTC"] = row
                page = d.create_app(app_runtime).test_client().get("/")
                self.assertEqual(page.status_code, 200)
                self.assertNotIn("Sweep take-profit · 100%", page.get_data(as_text=True))
                live_page = d.create_app(app_runtime).test_client().get("/paper-trading").get_data(as_text=True)
                self.assertIn(trade["id"][:12], live_page)

    def test_alternate_video_timeframe_pair_is_supported_and_mismatches_rejected(self):
        other = replace(self.rules, smc_setup_minutes=15, smc_entry_minutes=1).validate()
        app_runtime = d.DashboardRuntime(ASSETS, other, self.store, provider=Mock(), position_store=self.positions)
        self.assertEqual((app_runtime.high_interval, app_runtime.low_interval), (d.M15, 60000))
        with self.assertRaises(d.DataError):
            replace(self.rules, smc_setup_minutes=30, smc_entry_minutes=1).validate()

    def test_legacy_fingerprint_matches_before_smc_fields_existed(self):
        rules = d.Rules()
        old = {k: v for k, v in asdict(rules).items() if not k.startswith(("smc_", "nn_")) and k not in {"strategy_model", "market_mode"}}
        expected = hashlib.sha256(json.dumps({"engine": d.ENGINE_VERSION, "assets": ASSETS, "rules": old}, sort_keys=True).encode()).hexdigest()[:20]
        self.assertEqual(d.fingerprint(ASSETS, rules), expected)

    def test_smc_once_preserves_legacy_ledger_and_manual_holdings_without_workers(self):
        base = self.path/"preserved.json"
        base.write_text('previous ledger preserved byte-for-byte', encoding="utf-8")
        prior = base.read_bytes()
        holdings = PositionStore(base.with_suffix(".positions.json"))
        position = holdings.open_position(ASSETS, "BTC", "long", 100, 1, uuid.uuid4().hex, self.now)[0]
        settings = self.path/"settings.json"
        settings.write_text(json.dumps({"assets": [{"name": name, **cfg} for name,cfg in ASSETS.items()],
                                       "strategy": asdict(self.rules)}), encoding="utf-8")
        with patch.object(runtime_module, "Kraken", return_value=self.provider), patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.argv", ["dashboard", "--once", "--settings", str(settings), "--state", str(base)]), \
                patch("threading.Thread.start", side_effect=AssertionError("No notification workers in --once")):
            cli.main()
        self.assertEqual(base.read_bytes(), prior)
        self.assertTrue(base.with_suffix(".neural.json").exists())
        saved = PositionStore(base.with_suffix(".positions.json")).snapshot()
        self.assertEqual({key: saved["positions"][0][key] for key in position}, position)
        self.assertIn("target_errors", saved["positions"][0])
        self.assertEqual(saved["outbox"], [])


if __name__ == "__main__":
    unittest.main()
