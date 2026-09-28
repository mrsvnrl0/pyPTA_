"""Lower buy watches and bearish-source spot paper orders; no live messages."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import requests

from adaptive_crypto.core import DataError, M5, Rules
from adaptive_crypto.notifications import dispatch_once
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.smc import scan
from adaptive_crypto.smc_engine import SMCEngine
from adaptive_crypto.smc_ledger import SMCStore
from adaptive_crypto.web import create_app
from test_smc_formulas import full_fixture


ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


class WatchFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rules = Rules(strategy_model="smc_video", smc_pivot_strength=1, smc_stop_buffer_bps=10, fee_rate=.001)
        self.high, self.low = full_fixture("short")
        self.now = self.low[-1].end+1
        self.positions = PositionStore(self.root/"holdings.json")
        self.paper = SMCStore(self.root/"paper.json", ASSETS, self.rules)
        offline = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Tests must remain offline"))
        offline.start()
        self.addCleanup(offline.stop)

    def quote(self, ask, offset=0):
        return {"ask": ask, "bid": ask-.00001, "asof_ms": self.now+offset,
                "last": ask, "volume24_base": 100}

    def watch(self, reference=2465.89, target=2439.7795):
        return self.positions.open_buy_watch(ASSETS, "BTC", reference, target, 1,
                                              uuid.uuid4().hex, self.now-1)[0]

    def monitor(self, ask, offset=0, **kwargs):
        self.positions.monitor_buy_watches("BTC", "BTC/USD", kwargs.get("high", self.high),
            kwargs.get("low", self.low), kwargs.get("quote", self.quote(ask, offset)),
            kwargs.get("rules", self.rules), self.now+offset, kwargs.get("clock_error"))


class BuyWatchTests(WatchFixture):
    def test_reference_only_waits_and_exact_lower_ask_alerts_buy_without_owning_crypto(self):
        watch = self.watch()
        original_cash = self.paper.snapshot()["cash"]
        self.monitor(2465.89)
        saved = self.positions.snapshot()
        self.assertTrue(saved["buy_watches"][0]["armed"])
        self.assertEqual(saved["positions"], [])
        self.assertTrue(saved["outbox"])
        self.assertEqual(saved["outbox"][0]["payload"]["action"], "wait")
        self.assertIn("BEARISH MOMENTUM", saved["outbox"][0]["text"])
        self.monitor(2439.77950001, 1000)
        self.assertEqual(self.positions.snapshot()["buy_watches"][0]["status"], "watching")
        self.monitor(2439.7795, 2000)
        saved = self.positions.snapshot()
        hit, = [e for e in saved["outbox"] if e["alert_type"] == "buy_watch_hit"]
        self.assertEqual(hit["payload"]["action"], "buy")
        self.assertEqual(hit["payload"]["buy_price"], 2439.7795)
        self.assertEqual(hit["payload"]["buy_quote"], 2439.7795)
        self.assertNotIn("COVER", hit["text"])
        self.assertEqual(hit["position_ids"], [])
        self.assertEqual(hit["buy_watch_ids"], [watch["id"]])
        self.assertEqual(saved["positions"], [])
        self.assertEqual(self.paper.snapshot()["cash"], original_cash)
        self.assertTrue(all(e["status"] == "cancelled" for e in saved["outbox"] if e["alert_type"] == "buy_watch_momentum"))
        sender = Mock(return_value={"status": "sent"})
        self.assertTrue(dispatch_once(self.positions, "telegram", sender, self.now+2000))
        self.monitor(2439.77, 3000)
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+3000))
        self.assertEqual(sender.call_count, 1)

    def test_stale_bid_only_clock_failure_and_precreation_quotes_do_not_trigger(self):
        self.watch()
        self.monitor(2465)
        above = self.quote(2440, 1000)
        above["bid"] = 2430
        cases = [dict(quote=above), dict(quote=self.quote(2439, -40000)),
                 dict(quote=self.quote(2439, -2)), dict(clock_error="Unverified clock")]
        for changes in cases:
            with self.subTest(changes=changes):
                self.monitor(2439, 1000, **changes)
                self.assertFalse(any(e["alert_type"] == "buy_watch_hit" for e in self.positions.snapshot()["outbox"]))
        # Saved buy prices continue to work with a fresh ask through a candle outage.
        self.monitor(2439, 2000, high=[], low=[])
        self.assertEqual(self.positions.snapshot()["buy_watches"][0]["status"], "reached")

    def test_first_observation_below_level_is_missed_not_backfilled(self):
        self.watch()
        self.monitor(2439)
        saved = self.positions.snapshot()
        self.assertEqual(saved["buy_watches"][0]["status"], "missed")
        sender = Mock()
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now))
        self.monitor(2465, 1000)
        self.monitor(2439, 2000)
        self.assertFalse(any(e["alert_type"] == "buy_watch_hit" for e in self.positions.snapshot()["outbox"]))

    def test_restart_rebound_and_expiry_require_fresh_price_without_resetting_retry(self):
        watch = self.watch()
        self.monitor(2465)
        self.monitor(2439, 1000)
        event_id = f"buy-watch:{watch['id']}:buy"
        self.positions.transaction(lambda d: next(e for e in d["outbox"] if e["id"] == event_id).update(retry_ms=self.now+20000))
        self.positions = PositionStore(self.positions.path)
        sender = Mock(return_value={"status": "sent"})
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+1001))
        self.monitor(2439, 2000)
        self.monitor(2441, 3000)
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+20000))
        self.monitor(2439, 21000)
        event = next(e for e in self.positions.snapshot()["outbox"] if e["id"] == event_id)
        self.assertEqual(event["retry_ms"], self.now+20000)
        self.assertTrue(dispatch_once(self.positions, "telegram", sender, self.now+21000))
        self.positions = PositionStore(self.positions.path)
        self.monitor(2439, 22000)
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+22000))

    def test_formula_target_uses_lower_sweep_and_stays_fixed_across_restart(self):
        reference = self.low[-1].c+5
        self.watch(reference, None)
        self.monitor(self.low[-1].c)
        saved = self.positions.snapshot()["buy_watches"][0]
        self.assertEqual(saved["target_source"], "sweep")
        self.assertLess(saved["buy_price"], reference)
        self.assertAlmostEqual(saved["buy_price"], saved["liquidity_price"]*(1-self.rules.smc_tp_sweep_buffer_bps/10000))
        self.positions = PositionStore(self.positions.path)
        self.monitor(self.low[-1].c, 1000, rules=replace(self.rules, smc_tp_sweep_buffer_bps=20))
        self.assertEqual(self.positions.snapshot()["buy_watches"][0]["buy_price"], saved["buy_price"])

    def test_undelivered_buy_quote_expires_and_can_only_refresh_at_buy_level(self):
        self.watch()
        self.monitor(2465)
        self.monitor(2439, 1000)
        sender = Mock(return_value={"status": "sent"})
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+31001))
        self.monitor(2440, 32000)
        self.assertFalse(dispatch_once(self.positions, "telegram", sender, self.now+32000))
        self.monitor(2439, 33000)
        self.assertTrue(dispatch_once(self.positions, "telegram", sender, self.now+33000))

    def test_routes_are_idempotent_csrf_protected_and_cancel_only_the_watch(self):
        runtime = DashboardRuntime(ASSETS, self.rules, self.paper, position_store=self.positions)
        client = create_app(runtime).test_client()
        html = client.get("/positions").get_data(as_text=True)
        self.assertIn("Add SHORT buy watch", html)
        body = {"asset": "BTC", "reference_price": 2465.89, "buy_price": 2439.7795,
                "quantity": 1, "request_id": uuid.uuid4().hex}
        self.assertEqual(client.post("/api/buy-watches", json=body).status_code, 400)
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        self.assertEqual(client.post("/api/buy-watches", json={**body, "buy_price": 2500}, headers=headers).status_code, 400)
        opened = client.post("/api/buy-watches", json=body, headers=headers)
        self.assertEqual(opened.status_code, 201)
        self.assertEqual(client.post("/api/buy-watches", json=body, headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/buy-watches", json={**body, "quantity": 2}, headers=headers).status_code, 400)
        watch = opened.get_json()["buy_watch"]
        self.assertEqual(client.post(f"/api/buy-watches/{watch['id']}/cancel", json={}, headers=headers).status_code, 200)
        self.assertEqual(self.positions.snapshot()["buy_watches"][0]["status"], "cancelled")
        self.assertEqual(self.positions.snapshot()["positions"], [])
        self.assertEqual(client.get("/positions").status_code, 200)

    def test_watch_without_an_owned_holding_is_scanned_and_exposed_as_current(self):
        self.watch()
        provider = Mock()
        provider.now.return_value = self.now
        provider.quotes.return_value = {"BTC/USD": self.quote(2465)}
        provider.candles.side_effect = lambda symbol, interval, now: self.high if interval == self.rules.smc_setup_minutes*60000 else self.low
        runtime = DashboardRuntime(ASSETS, self.rules, self.paper, provider=provider, position_store=self.positions)
        runtime.scan_once()
        provider.now.return_value = self.now+1000
        provider.quotes.return_value = {"BTC/USD": self.quote(2439, 1000)}
        runtime.scan_once()
        with patch("adaptive_crypto.runtime.time.time", return_value=(self.now+1000)/1000):
            snapshot = runtime.snapshot()
        hit, = [e for e in snapshot["holdings"]["outbox"] if e["alert_type"] == "buy_watch_hit"]
        self.assertTrue(hit["is_current"])
        self.assertEqual(snapshot["holdings"]["positions"], [])

    def test_both_forms_save_target_mode_validate_it_and_reject_changed_retries(self):
        runtime = DashboardRuntime(ASSETS, self.rules, self.paper, position_store=self.positions)
        client = create_app(runtime).test_client()
        html = client.get("/positions").get_data(as_text=True)
        self.assertEqual(html.count('name="target_mode"'), 1)
        self.assertIn('<option value="gex_smc">SMC + GEX</option>', html)
        with client.session_transaction() as session:
            headers = {"X-CSRF-Token": session["csrf_token"]}
        for path, record_key, fields in (
                ("/api/positions", "position", {"side": "long", "entry": 180}),
                ("/api/buy-watches", "buy_watch", {"reference_price": 180, "buy_price": ""})):
            for mode in ("smc", "gex_smc"):
                body = {"asset": "BTC", "quantity": 1, "request_id": uuid.uuid4().hex,
                        "target_mode": mode, **fields}
                result = client.post(path, json=body, headers=headers)
                self.assertEqual(result.status_code, 201)
                self.assertEqual(result.get_json()[record_key]["target_mode"], mode)
                self.assertEqual(client.post(path, json=body, headers=headers).status_code, 200)
                changed = {**body, "target_mode": "smc" if mode == "gex_smc" else "gex_smc"}
                self.assertEqual(client.post(path, json=changed, headers=headers).status_code, 400)
            for invalid in (None, "", "gex", [], True):
                body = {"asset": "BTC", "quantity": 1, "request_id": uuid.uuid4().hex,
                        "target_mode": invalid, **fields}
                self.assertEqual(client.post(path, json=body, headers=headers).status_code, 400)
        restarted = PositionStore(self.positions.path).snapshot()
        self.assertEqual([p["target_mode"] for p in restarted["positions"]], ["smc", "gex_smc"])
        self.assertEqual([w["target_mode"] for w in restarted["buy_watches"]], ["smc", "gex_smc"])


class BearishPaperBuyTests(WatchFixture):
    def setUp(self):
        super().setUp()
        self.engine = SMCEngine(ASSETS, self.rules, self.paper)
        self.source, = scan(self.high, self.low, self.rules, "short")["entries"]

    def evaluate(self, ask, offset=0, **kwargs):
        return self.engine.evaluate("BTC", kwargs.get("quote", self.quote(ask, offset)),
            kwargs.get("high", self.high), kwargs.get("low", self.low), self.now+offset)

    def record(self):
        self.evaluate(self.low[-1].c)
        order = self.paper.snapshot()["assets"]["BTC"]["pending"]
        self.assertIsNotNone(order)
        return order

    def test_bearish_paper_opportunity_waits_then_buys_lower_and_exits_as_long(self):
        order = self.record()
        self.assertEqual(order["side"], "long")
        self.assertEqual(order["limit"], self.source["target"])
        self.assertEqual(order["reference_price"], self.source["limit"])
        self.assertAlmostEqual(order["stop"], order["limit"]*(1-self.rules.smc_stop_buffer_bps/10000))
        self.assertGreater(order["target_liquidity_price"], order["limit"])
        self.assertEqual(self.paper.snapshot()["cash"], self.rules.paper_equity)
        self.assertIn("WAIT TO BUY", self.paper.snapshot()["outbox"][0]["text"])
        self.evaluate(order["reference_price"], 1000)
        self.assertEqual(self.paper.snapshot()["assets"]["BTC"]["trades"], [])
        self.evaluate(order["buy_liquidity_price"], 2000)
        self.assertIsNotNone(self.paper.snapshot()["assets"]["BTC"]["pending"])
        row = self.evaluate(order["limit"], 3000)
        trade, = self.paper.snapshot()["assets"]["BTC"]["trades"]
        self.assertEqual((trade["side"], trade["entry"], trade["strategy"]), ("long", order["limit"], "smc_short"))
        self.assertEqual(row["smc_short"]["status"], "ACTIVE PAPER TRADE")
        self.assertIn("BUY TO OPEN LONG", self.paper.snapshot()["outbox"][-1]["text"])
        self.evaluate(order["target"]+.00001, 4000)
        saved = self.paper.snapshot()
        self.assertEqual(saved["assets"]["BTC"]["trades"][0]["status"], "target")
        self.assertGreater(saved["assets"]["BTC"]["trades"][0]["realized_pnl"], 0)
        self.assertIn("SELL TO EXIT LONG", saved["outbox"][-1]["text"])
        self.assertNotIn("BUY TO COVER", "\n".join(e["text"] for e in saved["outbox"]))

    def test_pending_lower_buy_survives_restart_without_being_a_short_sale(self):
        order = self.record()
        self.paper = SMCStore(self.paper.path, ASSETS, self.rules)
        self.engine = SMCEngine(ASSETS, self.rules, self.paper)
        self.assertEqual(self.paper.snapshot()["assets"]["BTC"]["pending"], order)
        self.evaluate(order["limit"], 1000)
        self.assertEqual(len(self.paper.snapshot()["assets"]["BTC"]["trades"]), 1)
        self.evaluate(order["limit"], 2000)
        self.assertEqual(len(self.paper.snapshot()["assets"]["BTC"]["trades"]), 1)

    def test_source_invalidation_and_already_touched_buy_cannot_create_purchase(self):
        self.evaluate(self.source["target"])
        self.assertIsNone(self.paper.snapshot()["assets"]["BTC"]["pending"])
        self.assertEqual(self.paper.snapshot()["assets"]["BTC"]["trades"], [])
        self.paper = SMCStore(self.root/"second.json", ASSETS, self.rules)
        self.engine = SMCEngine(ASSETS, self.rules, self.paper)
        order = self.record()
        self.evaluate(order["source_stop"]+1, 1000)
        self.assertIsNone(self.paper.snapshot()["assets"]["BTC"]["pending"])
        self.assertEqual(self.paper.snapshot()["assets"]["BTC"]["trades"], [])

    def test_corrupted_saved_lower_buy_fails_without_overwriting(self):
        self.record()
        saved = self.paper.snapshot()
        saved["assets"]["BTC"]["pending"]["source_signal"]["target"] += 1
        self.paper.path.write_text(json.dumps(saved), encoding="utf-8")
        original = self.paper.path.read_bytes()
        with self.assertRaises(DataError):
            SMCStore(self.paper.path, ASSETS, self.rules)
        self.assertEqual(self.paper.path.read_bytes(), original)

    def test_future_lower_candle_touch_can_fill_below_the_bearish_order_block(self):
        order = self.record()
        first = replace(self.low[-1], t=self.now)
        filling = replace(self.low[-1], t=self.now+M5, o=order["limit"]+.2,
                          h=order["limit"]+1, l=order["limit"]-.01, c=order["limit"]+.1)
        self.evaluate(order["limit"]+.1, filling.end+1-self.now, low=self.low+[first, filling])
        trade, = self.paper.snapshot()["assets"]["BTC"]["trades"]
        self.assertEqual(trade["entry"], order["limit"])
        self.assertEqual(trade["opened_ms"], filling.t)
        self.assertEqual(trade["status"], "active")


if __name__ == "__main__":
    unittest.main()
