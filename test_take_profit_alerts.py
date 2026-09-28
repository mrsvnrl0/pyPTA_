"""Offline alert timing, saved targets, Telegram dispatch and holding integration."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import requests

from adaptive_crypto.core import Candle, DataError, M5, M30, Rules
from adaptive_crypto.notifications import dispatch_once, telegram_send
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.smc import scan
from adaptive_crypto.smc_engine import SMCEngine
from adaptive_crypto.smc_ledger import SMCStore
from adaptive_crypto.web import create_app
from test_display_formatting import DashboardHTML
from test_smc_formulas import full_fixture


ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


class AlertCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.rules = Rules(strategy_model="smc_video", market_mode="margin", smc_pivot_strength=1, fee_rate=.001)
        offline = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Tests must stay offline"))
        offline.start()
        self.addCleanup(offline.stop)

    def setup_side(self, side):
        self.side = side
        self.high, self.low = full_fixture(side)
        self.now = self.low[-1].end+1
        self.market = self.low[-1].c

    def quote(self, mark, offset=0):
        return {"bid": mark if self.side == "long" else mark-.01,
                "ask": mark+.01 if self.side == "long" else mark,
                "last": mark, "volume24_base": 100, "asof_ms": self.now+offset}

    def paper(self, side, fill=True):
        self.setup_side(side)
        self.paper_path = self.directory/(uuid.uuid4().hex+".smc.json")
        self.store = SMCStore(self.paper_path, ASSETS, self.rules)
        self.engine = SMCEngine(ASSETS, self.rules, self.store)
        candidate, = scan(self.high, self.low, self.rules, side)["entries"]
        self.target = candidate["target"]
        self.engine.evaluate("BTC", self.quote(self.market), self.high, self.low, self.now)
        if fill:
            quote = self.quote(candidate["limit"], 1000)
            quote.update(bid=candidate["limit"]-.01 if side == "long" else candidate["limit"],
                         ask=candidate["limit"] if side == "long" else candidate["limit"]+.01)
            self.engine.evaluate("BTC", quote, self.high, self.low, self.now+1000)
            self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"][0]["status"], "active")

    def holding(self, side):
        self.setup_side(side)
        self.positions = PositionStore(self.directory/(uuid.uuid4().hex+".positions.json"), market_mode="margin")
        self.position = self.positions.open_position(ASSETS, "BTC", side, 115 if side == "long" else 135,
                                                     2, uuid.uuid4().hex, self.now-1000)[0]
        self.monitor_holding(self.market)
        self.target = self.positions.snapshot()["positions"][0]["take_profit"]["price"]

    def monitor_holding(self, mark, offset=0, **kwargs):
        self.positions.monitor_take_profit("BTC", "BTC/USD", kwargs.get("high", self.high),
            kwargs.get("low", self.low), kwargs.get("quote", self.quote(mark, offset)),
            kwargs.get("rules", self.rules), self.now+offset, kwargs.get("clock_error"))

    def near(self):
        return self.target*(.9995 if self.side == "long" else 1.0005)

    def paper_quote(self, mark, offset=2000, **kwargs):
        return self.engine.evaluate("BTC", kwargs.get("quote", self.quote(mark, offset)),
            kwargs.get("high", self.high), kwargs.get("low", self.low), self.now+offset, kwargs.get("errors"))

    def alerts(self, store=None):
        return [e for e in (store or self.store).snapshot()["outbox"] if e.get("alert_type") == "near_take_profit"]


class PaperTakeProfitAlertTests(AlertCase):
    def test_inclusive_tenth_percent_boundary_uses_executable_side_and_sends_once(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.paper(side)
                boundary = self.target*(.999 if side == "long" else 1.001)
                outside = boundary+(-.001 if side == "long" else .001)
                quote = self.quote(outside, 2000)
                quote["last"] = self.near()
                self.paper_quote(outside, quote=quote)
                self.assertEqual(self.alerts(), [])
                self.paper_quote(boundary, 3000)
                event, = self.alerts()
                self.assertAlmostEqual(event["payload"]["distance_percent"], .1)
                self.assertIn("within 0.1%", event["text"])
                self.assertIn("live "+("bid" if side == "long" else "ask"), event["text"])
                sender = Mock(return_value={"status": "sent", "message_id": 7})
                while dispatch_once(self.store, "telegram", sender, self.now+4000):
                    pass
                self.assertEqual(sum(c.args[0].get("alert_type") == "near_take_profit" for c in sender.call_args_list), 1)
                self.store = SMCStore(self.paper_path, ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                self.paper_quote(self.market, 5000)
                self.paper_quote(self.near(), 6000)
                self.assertEqual(len(self.alerts()), 1)
                self.assertEqual(self.alerts()[0]["status"], "sent")

    def test_stale_future_inverted_and_unverified_clock_quotes_cannot_warn(self):
        self.paper("long")
        for quote, errors in (({**self.quote(self.near(), 2000), "asof_ms": self.now-30000}, None),
                              ({**self.quote(self.near(), 2000), "asof_ms": self.now+5001}, None),
                              ({**self.quote(self.near(), 2000), "bid": self.target+1}, None),
                              (self.quote(self.near(), 2000), {"clock": "unverified"}),
                              (None, None)):
            self.paper_quote(self.near(), quote=quote, errors=errors)
            self.assertEqual(self.alerts(), [])
        self.paper_quote(self.near(), 3000, high=[], low=[])
        self.assertEqual(len(self.alerts()), 1, "A feed outage must not suppress a fresh quote against an existing target")

    def test_pending_orders_and_already_crossed_targets_do_not_warn(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.paper(side, fill=False)
                self.paper_quote(self.near())
                self.assertEqual(self.alerts(), [])
                self.paper(side)
                self.paper_quote(self.target)
                self.assertEqual(self.alerts(), [])

    def test_closing_or_leaving_range_cancels_queued_warning(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.paper(side)
                self.paper_quote(self.near())
                self.paper_quote(self.market, 3000)
                self.assertEqual(self.alerts()[0]["status"], "cancelled")
                self.paper_quote(self.near(), 4000)
                self.assertEqual(self.alerts()[0]["status"], "queued")
                self.paper_quote(self.target, 5000)
                self.assertEqual(self.alerts()[0]["status"], "cancelled")
                sender = Mock(return_value={"status": "sent"})
                while dispatch_once(self.store, "telegram", sender, self.now+6000):
                    pass
                self.assertFalse(any(c.args[0].get("alert_type") == "near_take_profit" for c in sender.call_args_list))

    def test_expired_observation_is_cancelled_at_dispatch_and_can_refresh_before_attempt(self):
        self.paper("long")
        self.paper_quote(self.near())
        expires = self.alerts()[0]["expires_ms"]
        sender = Mock(return_value={"status": "sent"})
        while dispatch_once(self.store, "telegram", sender, expires):
            pass
        self.assertEqual(self.alerts()[0]["status"], "cancelled")
        self.assertFalse(any(c.args[0].get("alert_type") == "near_take_profit" for c in sender.call_args_list))
        self.paper_quote(self.near(), 33000)
        self.assertTrue(dispatch_once(self.store, "telegram", sender, self.now+33001))
        self.assertEqual(self.alerts()[0]["status"], "sent")

    def test_alert_creation_and_trade_state_roll_back_on_failed_save(self):
        self.paper("long")
        before, disk = self.store.snapshot(), self.paper_path.read_bytes()
        with patch("adaptive_crypto.smc_ledger.atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.paper_quote(self.near())
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.paper_path.read_bytes(), disk)

    def test_notification_setting_migration_preserves_pending_orders_and_cash(self):
        self.paper("long", fill=False)
        old = self.store.snapshot()
        del old["settings"]["smc_tp_alert_bps"]
        self.paper_path.write_text(json.dumps(old), encoding="utf-8")
        restored = SMCStore(self.paper_path, ASSETS, replace(self.rules, smc_tp_alert_bps=25)).snapshot()
        self.assertEqual(restored["assets"], old["assets"])
        self.assertEqual(restored["cash"], old["cash"])
        self.assertEqual(restored["warnings"], old["warnings"])
        self.assertFalse((self.directory/"backup"/"latest.zip").exists())


class HoldingTakeProfitAlertTests(AlertCase):
    def test_target_uses_confirmed_opposing_liquidity_and_stays_fixed_across_restart_and_settings(self):
        for side, level, target in (("long", 140, 140.014), ("short", 110, 109.989)):
            with self.subTest(side=side):
                self.holding(side)
                saved = self.positions.snapshot()["positions"][0]
                self.assertEqual(saved["take_profit"]["liquidity_price"], level)
                self.assertAlmostEqual(saved["take_profit"]["price"], target)
                self.assertEqual({k: saved[k] for k in self.position}, self.position)
                self.positions = PositionStore(self.positions.path, market_mode="margin")
                self.monitor_holding(self.market, 1000, rules=replace(self.rules, smc_tp_sweep_buffer_bps=20))
                self.assertEqual(self.positions.snapshot()["positions"][0]["take_profit"], saved["take_profit"])
                self.assertEqual(self.alerts(self.positions), [])

    def test_near_target_sends_telegram_once_for_each_side_and_remains_a_manual_holding(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.holding(side)
                self.monitor_holding(self.near(), 1000)
                sender = Mock(return_value={"status": "sent", "message_id": 42})
                self.assertTrue(dispatch_once(self.positions, "telegram", sender, self.now+1001))
                event = sender.call_args.args[0]
                self.assertEqual(event["position_ids"], [self.position["id"]])
                self.assertIn("Holding remains open", event["text"])
                self.assertIn("TAKE-PROFIT APPROACHING", event["text"])
                self.positions = PositionStore(self.positions.path, market_mode="margin")
                self.monitor_holding(self.near(), 2000)
                self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+2001))
                self.assertEqual(self.positions.snapshot()["positions"][0]["status"], "open")

    def test_target_hit_cancels_pending_warning_and_does_not_retarget_or_close_holding(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.holding(side)
                self.monitor_holding(self.near(), 1000)
                self.monitor_holding(self.target, 2000)
                self.monitor_holding(self.near(), 3000)
                record = self.positions.snapshot()["positions"][0]
                self.assertEqual((record["status"], record["take_profit"]["state"]), ("open", "reached"))
                self.assertEqual(record["take_profit"]["price"], self.target)
                self.assertEqual(self.alerts(self.positions)[0]["status"], "cancelled")

    def test_manual_close_cancels_warning_and_stops_monitoring(self):
        self.holding("long")
        self.monitor_holding(self.near(), 1000)
        self.positions.close_position(self.position["id"], self.near(), self.now+2000)
        self.monitor_holding(self.near(), 3000)
        sender = Mock()
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+3001))
        self.assertEqual(self.alerts(self.positions)[0]["status"], "cancelled")

    def test_target_selection_requires_current_feeds_but_saved_target_only_needs_fresh_quote(self):
        self.holding("long")
        self.monitor_holding(self.near(), 1000, quote=None)
        self.monitor_holding(self.near(), 2000, clock_error="unverified")
        self.assertEqual(self.alerts(self.positions), [])
        self.monitor_holding(self.near(), 3000, high=[], low=[])
        self.assertEqual(len(self.alerts(self.positions)), 1)
        other = PositionStore(self.directory/"waiting.positions.json", market_mode="margin")
        other.open_position(ASSETS, "BTC", "long", 115, 2, uuid.uuid4().hex, self.now)
        other.monitor_take_profit("BTC", "BTC/USD", [], self.low, self.quote(self.market), self.rules, self.now)
        self.assertNotIn("take_profit", other.snapshot()["positions"][0])
        self.assertIn("Waiting for current", other.snapshot()["positions"][0]["take_profit_error"])

    def test_historical_near_wick_never_warns_and_later_target_wick_retires_watch(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.holding(side)
                price = self.market
                partial = Candle(self.now, price, max(price, self.target), min(price, self.target), price, 100, M5)
                self.monitor_holding(price, M5, low=self.low+[partial])
                self.assertEqual(self.positions.snapshot()["positions"][0]["take_profit"]["state"], "watching")
                later = replace(partial, t=self.now+M5)
                self.monitor_holding(price, 2*M5, low=self.low+[partial, later])
                self.assertEqual(self.positions.snapshot()["positions"][0]["take_profit"]["state"], "reached")
                self.assertEqual(self.alerts(self.positions), [])

    def test_restart_requires_a_fresh_observation_before_dispatching_queued_warning(self):
        self.holding("long")
        self.monitor_holding(self.near(), 1000)
        self.positions = PositionStore(self.positions.path, market_mode="margin")
        self.assertEqual(self.alerts(self.positions)[0]["status"], "cancelled")
        self.monitor_holding(self.near(), 2000)
        self.assertEqual(self.alerts(self.positions)[0]["status"], "queued")
        sender = Mock(return_value={"status": "uncertain"})
        self.assertTrue(dispatch_once(self.positions, "telegram", sender, self.now+2001))
        self.positions = PositionStore(self.positions.path, market_mode="margin")
        self.monitor_holding(self.near(), 3000)
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+3001))

    def test_changed_or_missing_target_metadata_is_rejected_without_overwrite(self):
        self.holding("long")
        original = self.positions.snapshot()
        for field, value in (("price", 140), ("sweep_buffer_bps", 20), ("selected_ms", -1), ("liquidity_price", None)):
            with self.subTest(field=field):
                damaged = copy.deepcopy(original)
                damaged["positions"][0]["take_profit"][field] = value
                self.positions.path.write_text(json.dumps(damaged), encoding="utf-8")
                before = self.positions.path.read_bytes()
                with self.assertRaises(DataError):
                    PositionStore(self.positions.path, market_mode="margin")
                self.assertEqual(self.positions.path.read_bytes(), before)

    def test_runtime_monitors_holdings_and_renders_saved_target_and_alert_text(self):
        self.holding("long")
        provider = Mock()
        provider.now.return_value = self.now+1000
        provider.quotes.return_value = {"BTC/USD": self.quote(self.near(), 1000)}
        provider.candles.side_effect = lambda pair, interval, at: self.high if interval == M30 else self.low
        store = SMCStore(self.directory/"runtime.smc.json", ASSETS, self.rules)
        runtime = DashboardRuntime(ASSETS, self.rules, store, provider=provider, position_store=self.positions)
        runtime.scan_once()
        self.assertEqual(len(self.alerts(self.positions)), 1)
        response = create_app(runtime).test_client().get("/positions")
        self.assertEqual(response.status_code, 200)
        parsed = DashboardHTML(response.get_data(as_text=True))
        self.assertFalse(parsed.errors)
        for value in ("within 0.10%", "SMC", "TP1", "TP2", "$140.01", "SELL TO EXIT LONG", "TAKE-PROFIT APPROACHING"):
            self.assertIn(value, parsed.text)

    def test_telegram_transport_receives_warning_and_records_ack_without_network(self):
        self.holding("long")
        self.monitor_holding(self.near(), 1000)
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"ok": True, "result": {"message_id": 123}}
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "12345"}), \
                patch("adaptive_crypto.notifications.requests.sessions.Session.request", return_value=response) as post:
            self.assertTrue(dispatch_once(self.positions, "telegram", telegram_send, self.now+1001))
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "12345")
        self.assertIn("TAKE-PROFIT APPROACHING", post.call_args.kwargs["json"]["text"])
        self.assertEqual(self.alerts(self.positions)[0]["message_id"], 123)

    def test_alert_range_can_be_disabled_without_changing_saved_target(self):
        self.holding("long")
        before = self.positions.snapshot()["positions"][0]["take_profit"]
        self.monitor_holding(self.near(), 1000)
        self.monitor_holding(self.near(), 2000, rules=replace(self.rules, smc_tp_alert_bps=0))
        self.assertEqual(self.alerts(self.positions)[0]["status"], "cancelled")
        self.assertEqual(self.positions.snapshot()["positions"][0]["take_profit"], before)
        for value in (-1, 101, True, "10", float("nan")):
            with self.subTest(value=value), self.assertRaises(DataError):
                replace(self.rules, smc_tp_alert_bps=value).validate()

    def test_rate_limited_warning_can_refresh_without_bypassing_retry_deadline(self):
        self.holding("long")
        self.monitor_holding(self.near(), 1000)
        sender = Mock(side_effect=[{"status": "queued", "retry_ms": self.now+6000}, {"status": "sent"}])
        self.assertTrue(dispatch_once(self.positions, "telegram", sender, self.now+1001))
        self.monitor_holding(self.market, 2000)
        self.assertEqual(self.alerts(self.positions)[0]["status"], "cancelled")
        self.monitor_holding(self.near(), 3000)
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+4000))
        self.assertTrue(dispatch_once(self.positions, "telegram", sender, self.now+6001))
        self.assertEqual(self.alerts(self.positions)[0]["status"], "sent")
        self.assertEqual(len(self.alerts(self.positions)), 1)


if __name__ == "__main__":
    unittest.main()
