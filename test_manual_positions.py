"""Offline position lifecycle, directional alert, persistence and route regressions."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import requests

import adaptive_crypto_dashboard as d
from adaptive_crypto.positions import PositionStore, momentum_readings, position_pnl
from test_dashboard import ASSETS, bars, momentum_fixture, setbar
from test_display_formatting import DashboardHTML


def position_candles():
    candles = bars(69, d.M15)
    return [replace(b, o=100, h=101, l=99, c=100) for b in candles]


def append_price(candles, close):
    prior = candles[-1]
    candles.append(d.Candle(prior.end+1, prior.c, max(prior.c, close)+1,
                            min(prior.c, close)-1, close, 200, d.M15))
    return candles[-1].end+2


class PositionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"holdings.positions.json"
        self.store = PositionStore(self.path, market_mode="margin")
        self.rules = d.Rules(market_mode="margin")
        self.candles = position_candles()
        self.now = self.candles[-1].end+2
        network = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Tests must remain offline"))
        network.start()
        self.addCleanup(network.stop)

    def open(self, side="long", entry=100, quantity=2, now=None, key=None):
        return self.store.open_position(ASSETS, "BTC", side, entry, quantity, key or uuid.uuid4().hex, now or self.now)[0]

    def monitor(self, now=None, candles=None, error=None, rules=None):
        self.store.monitor("BTC", "BTC/USD", candles or self.candles, rules or self.rules, now or self.now, error)

    def swing(self, price):
        self.now = append_price(self.candles, price)
        self.monitor()


class PositionTests(PositionCase):
    def test_no_open_holding_produces_no_watch_no_alert_and_no_file(self):
        self.swing(120)
        self.swing(80)
        self.assertEqual(self.store.snapshot()["outbox"], [])
        self.assertFalse(self.path.exists())

    def test_open_does_not_alert_on_existing_direction(self):
        self.now = append_price(self.candles, 120)
        self.open()
        self.monitor()
        self.assertEqual(self.store.snapshot()["watches"]["BTC"]["reading"]["direction"], "bullish")
        self.assertEqual(self.store.snapshot()["outbox"], [])

    def test_bullish_and_bearish_transitions_alert_once_per_position_and_bar(self):
        first = self.open()
        second = self.open(side="short")
        self.monitor()
        self.swing(120)
        self.monitor()
        self.swing(80)
        self.monitor()
        events = self.store.snapshot()["outbox"]
        self.assertEqual([e["direction"] for e in events], ["bullish", "bullish", "bearish", "bearish"])
        self.assertEqual(events[0]["position_ids"], [first["id"]])
        self.assertEqual(events[1]["position_ids"], [second["id"]])
        self.assertIn("$+40.00", events[0]["text"])
        self.assertIn("$-40.00", events[1]["text"])
        self.assertEqual(events[2]["candle_ms"], self.candles[-1].t)
        self.assertEqual([e['payload']['action'] for e in events], ['hold','buy','sell','hold'])

    def test_neutral_does_not_alert_but_entering_a_direction_again_does(self):
        self.open()
        self.monitor()
        self.swing(120)
        self.swing(100)
        self.assertEqual(self.store.snapshot()["watches"]["BTC"]["reading"]["direction"], "neutral")
        self.assertEqual(len(self.store.snapshot()["outbox"]), 1)
        self.swing(130)
        self.assertEqual(len(self.store.snapshot()["outbox"]), 2)

    def test_flat_equal_roc_and_signal_is_neutral(self):
        reading = momentum_readings(self.candles, self.rules)[-1]
        self.assertEqual((reading["roc_percent"], reading["signal_percent"], reading["direction"]), (0, 0, "neutral"))

    def test_position_indicators_match_existing_momentum_formulas(self):
        candles, _, _ = momentum_fixture()
        signal = d.momentum_scan(candles, self.rules)
        reading = momentum_readings(candles, self.rules)[-1]
        checks = {c["key"]: c for c in signal["checks"]}
        self.assertEqual(reading["roc_percent"], checks["roc"]["measured"]["roc_percent"])
        self.assertEqual(reading["signal_percent"], checks["roc"]["measured"]["ema"])
        self.assertEqual(reading["rsi"], checks["rsi"]["measured"])
        self.assertEqual(reading["rvol"], checks["volume"]["measured"])
        self.assertEqual(reading["atr"], signal["signal"]["atr"])

    def test_volume_rsi_and_entry_gates_do_not_hide_direction_changes(self):
        self.open()
        self.monitor()
        self.now = append_price(self.candles, 120)
        self.candles[-1] = replace(self.candles[-1], v=0)
        self.monitor()
        self.assertEqual(self.store.snapshot()["outbox"][-1]["direction"], "bullish")
        self.assertEqual(self.store.snapshot()["watches"]["BTC"]["reading"]["rvol"], 0)

    def test_repeated_open_submission_is_idempotent_across_restart(self):
        key = uuid.uuid4().hex
        first = self.open(key=key)
        self.store = PositionStore(self.path, market_mode="margin")
        second = self.open(key=key)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.snapshot()["positions"]), 1)
        with self.assertRaises(d.DataError):
            self.open(key=key, quantity=3)

    def test_close_stops_alerts_and_cancels_unsent_messages(self):
        position = self.open()
        self.monitor()
        self.swing(120)
        closed = self.store.close_position(position["id"], 125, self.now)
        self.assertEqual(position_pnl(closed, 125), {"pnl_usd": 50, "pnl_percent": 25})
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "cancelled")
        self.swing(80)
        self.assertEqual(len(self.store.snapshot()["outbox"]), 1)
        self.assertEqual(self.store.snapshot()["watches"], {})
        sender = Mock()
        self.assertFalse(d.dispatch_once(self.store, "telegram", sender, self.now))
        sender.assert_not_called()

    def test_one_remaining_holding_keeps_monitoring_active(self):
        first = self.open()
        second = self.open()
        self.monitor()
        self.swing(120)
        self.store.close_position(first["id"], 120, self.now)
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "cancelled")
        self.assertEqual(self.store.snapshot()["outbox"][1]["status"], "queued")
        self.swing(80)
        self.assertEqual(self.store.snapshot()["outbox"][-1]["position_ids"], [second["id"]])

    def test_reopen_establishes_a_new_baseline_without_replaying_old_alerts(self):
        first = self.open()
        self.monitor()
        self.swing(120)
        self.store.close_position(first["id"], 120, self.now)
        self.open()
        self.monitor()
        self.assertEqual(len(self.store.snapshot()["outbox"]), 1)
        self.swing(80)
        self.assertEqual(len(self.store.snapshot()["outbox"]), 2)

    def test_stale_incomplete_and_clock_error_data_do_not_advance_alerts(self):
        self.open()
        self.monitor()
        original = self.store.snapshot()["watches"]["BTC"]["last_bar_ms"]
        future = copy.deepcopy(self.candles)
        next_now = append_price(future, 120)
        for candles, now, error in ((future, future[-1].t+1000, None),
                                     (self.candles, next_now, None), (future, next_now, "Clock unavailable")):
            with self.subTest(error=error, now=now):
                self.monitor(now, candles, error)
                self.assertEqual(self.store.snapshot()["watches"]["BTC"]["last_bar_ms"], original)
                self.assertIsNotNone(self.store.snapshot()["watches"]["BTC"]["error"])
                self.assertEqual(self.store.snapshot()["outbox"], [])
        self.monitor(next_now, future)
        self.assertEqual(len(self.store.snapshot()["outbox"]), 1)

    def test_restart_and_catchup_preserve_each_intermediate_direction_once(self):
        self.open()
        self.monitor()
        self.now = append_price(self.candles, 120)
        self.now = append_price(self.candles, 80)
        self.store = PositionStore(self.path, market_mode="margin")
        self.monitor()
        self.store = PositionStore(self.path, market_mode="margin")
        self.monitor()
        self.assertEqual([e["direction"] for e in self.store.snapshot()["outbox"]], ["bullish", "bearish"])

    def test_formula_change_preserves_holdings_and_rebaselines_without_false_alert(self):
        position = self.open()
        self.monitor()
        self.now = append_price(self.candles, 120)
        self.monitor(rules=replace(self.rules, momentum_roc_period=6))
        self.assertEqual(self.store.snapshot()["positions"][0], position)
        self.assertEqual(self.store.snapshot()["outbox"], [])
        self.assertEqual(self.store.snapshot()["watches"]["BTC"]["reading"]["roc_period"], 6)

    def test_history_gap_rebaselines_and_marks_unobserved_swings(self):
        self.open()
        self.monitor()
        later = bars(70, d.M15, self.candles[-1].end+100*d.M15+1)
        setbar(later, 69, 100, 121, 99, 120)
        self.monitor(later[-1].end+2, later)
        watch = self.store.snapshot()["watches"]["BTC"]
        self.assertIn("History gap", watch["error"])
        self.assertEqual(watch["reading"]["direction"], "bullish")
        self.assertEqual(self.store.snapshot()["outbox"], [])

    def test_failed_save_does_not_lose_a_swing_or_publish_an_unsaved_alert(self):
        self.open()
        self.monitor()
        before = self.store.snapshot()
        self.now = append_price(self.candles, 120)
        with patch("adaptive_crypto.positions.atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.monitor()
        self.assertEqual(self.store.snapshot(), before)
        self.monitor()
        self.assertEqual(len(self.store.snapshot()["outbox"]), 1)

    def test_alert_delivery_uses_durable_outbox_and_does_not_repeat_after_restart(self):
        self.open()
        self.monitor()
        self.swing(120)
        sender = Mock(return_value={"status": "sent", "message_id": 123, "error": None})
        self.assertTrue(d.dispatch_once(self.store, "telegram", sender, self.now))
        self.store = PositionStore(self.path, market_mode="margin")
        self.monitor()
        self.assertFalse(d.dispatch_once(self.store, "telegram", sender, self.now))
        self.assertEqual(sender.call_count, 1)

    def test_interrupted_delivery_is_uncertain_after_restart(self):
        self.open()
        self.monitor()
        self.swing(120)
        self.store.transaction(lambda doc: doc["outbox"][0].update(status="running"))
        self.store = PositionStore(self.path, market_mode="margin")
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "uncertain")

    def test_invalid_holdings_file_is_not_overwritten(self):
        self.path.write_text('{"version": 42}', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(d.DataError):
            PositionStore(self.path, market_mode="margin")
        self.assertEqual(self.path.read_bytes(), before)

    def test_short_pnl_is_opposite_of_long_pnl(self):
        long = self.open()
        short = self.open(side="short")
        self.assertEqual(position_pnl(long, 90)["pnl_usd"], -20)
        self.assertEqual(position_pnl(short, 90)["pnl_usd"], 20)

    def test_unrepresentable_pnl_does_not_block_momentum_or_corrupt_close(self):
        position = self.open(entry=1e-15, quantity=1e308)
        self.monitor()
        self.swing(120)
        event = self.store.snapshot()["outbox"][-1]
        self.assertEqual(event["direction"], "bullish")
        self.assertIn("Gross P/L unavailable", event["text"])
        before = self.path.read_bytes()
        with self.assertRaises(d.DataError):
            self.store.close_position(position["id"], 120, self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot()["positions"][0]["status"], "open")


class PositionRouteTests(PositionCase):
    def setUp(self):
        super().setUp()
        self.paper = d.StateStore(Path(self.temp.name)/"paper.json", ASSETS, self.rules)
        self.runtime = d.DashboardRuntime(ASSETS, self.rules, self.paper, provider=Mock(), position_store=self.store)
        self.client = d.create_app(self.runtime).test_client()
        response = self.client.get("/positions")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.token = session["csrf_token"]

    def body(self):
        return {"asset": "BTC", "side": "long", "entry": 100, "quantity": 2, "request_id": uuid.uuid4().hex}

    def post(self, body):
        return self.client.post("/api/positions", json=body, headers={"X-CSRF-Token": self.token})

    def test_open_close_forms_and_gross_pnl_render_without_mutating_paper_ledger(self):
        before = self.paper.snapshot()
        response = self.post(self.body())
        self.assertEqual(response.status_code, 201)
        position = response.get_json()["position"]
        html = self.client.get("/positions").get_data(as_text=True)
        parsed = DashboardHTML(html)
        self.assertFalse(parsed.errors)
        for text in ("My live positions", "MARGIN LONG", "$100.00", "Close position", "15M momentum"):
            self.assertIn(text, parsed.text)
        self.assertNotIn('http-equiv="refresh"', html)
        response = self.client.post(f"/api/positions/{position['id']}/close", json={"close": 125}, headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 200)
        closed = self.client.get("/api/state").get_json()["holdings"]["positions"][0]
        self.assertEqual((closed["status"], closed["pnl_usd"]), ("closed", 50))
        self.assertEqual(self.paper.snapshot(), before)

    def test_csrf_is_required_for_open_and_close(self):
        response = self.client.post("/api/positions", json=self.body())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.snapshot()["positions"], [])
        position = self.post(self.body()).get_json()["position"]
        response = self.client.post(f"/api/positions/{position['id']}/close", json={"close": 125})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.snapshot()["positions"][0]["status"], "open")

    def test_invalid_position_fields_are_rejected(self):
        for changes in ({"entry": 0}, {"entry": True}, {"quantity": -1}, {"entry": "NaN"},
                        {"entry": "Infinity"}, {"asset": "DOGE"}, {"side": "buy"},
                        {"extra": 3}, {"request_id": "invalid"}):
            with self.subTest(changes=changes):
                response = self.post({**self.body(), **changes})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.snapshot()["positions"], [])

    def test_duplicate_create_and_close_requests_are_safe(self):
        body = self.body()
        first = self.post(body)
        repeat = self.post(body)
        self.assertEqual((first.status_code, repeat.status_code), (201, 200))
        self.assertEqual(first.get_json(), repeat.get_json())
        pid = first.get_json()["position"]["id"]
        url = f"/api/positions/{pid}/close"
        one = self.client.post(url, json={"close": 105}, headers={"X-CSRF-Token": self.token})
        two = self.client.post(url, json={"close": 105}, headers={"X-CSRF-Token": self.token})
        self.assertEqual(one.get_json(), two.get_json())
        self.assertEqual(self.client.post(url, json={"close": 110}, headers={"X-CSRF-Token": self.token}).status_code, 400)

    def test_unknown_close_is_not_found(self):
        response = self.client.post("/api/positions/missing/close", json={"close": 100}, headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 404)

    def test_position_monitoring_continues_when_paper_strategy_feed_fails(self):
        from dataclasses import replace
        from test_smc_integration import structure_bars
        low = structure_bars()
        now=low[-1].end+2
        self.open()
        rules=replace(self.rules,strategy_model="smc_video")
        self.store.monitor("BTC","BTC/USD",low[:11],rules,low[10].end+2)
        provider=self.runtime.provider
        provider.now.return_value=now
        provider.quotes.return_value={}
        provider.candles.side_effect=lambda pair,interval,at: low if interval == 300000 else []
        self.runtime.scan_once()
        reading=self.store.snapshot()["watches"]["BTC"]["reading"]
        self.assertEqual(reading["basis"],"structure")
        self.assertEqual(reading["direction"],"bearish")
        self.assertEqual(reading["interval_ms"],300000)
        self.assertTrue(self.runtime.snapshot()["data"]["BTC"]["errors"].get("4h"))
        self.assertEqual(self.runtime.position_errors,{})


if __name__ == "__main__":
    unittest.main()
