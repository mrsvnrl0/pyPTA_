"""Spot purchase direction, correction, target delivery, and restart regressions."""
import copy
from dataclasses import replace
import json
from pathlib import Path
from zipfile import ZipFile
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import requests

from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.ledger import queue_event
from adaptive_crypto.notifications import dispatch_once
from adaptive_crypto.position_alerts import target_event_id
from adaptive_crypto.positions import PositionStore, position_pnl
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.smc import scan
from adaptive_crypto.smc_engine import SMCEngine, advance_limit
from adaptive_crypto.smc_ledger import SMCStore, entry_plan
from adaptive_crypto.web import create_app
from test_smc_formulas import full_fixture


ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


class SpotBuyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rules = Rules(strategy_model="smc_video", smc_pivot_strength=1, fee_rate=.001)
        self.high, self.low = full_fixture("long")
        self.now = self.low[-1].end+1
        self.positions = PositionStore(self.root/"holdings.positions.json")
        self.paper = SMCStore(self.root/"paper.smc.json", ASSETS, self.rules)
        offline = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Offline tests"))
        offline.start()
        self.addCleanup(offline.stop)

    def quote(self, mark, offset=0):
        return {"bid": mark, "ask": mark+.01, "last": mark, "volume24_base": 100, "asof_ms": self.now+offset}

    def open(self, side="long", entry=115):
        return self.positions.open_position(ASSETS, "BTC", side, entry, 2, uuid.uuid4().hex, self.now-1000)[0]

    def test_spot_default_rejects_short_form_and_direct_store_entry(self):
        self.assertEqual(self.rules.validate().market_mode, "spot")
        with self.assertRaises(DataError):
            replace(self.rules, market_mode="short_buy").validate()
        with self.assertRaisesRegex(DataError, "Spot purchases"):
            self.open("short")
        runtime = DashboardRuntime(ASSETS, self.rules, self.paper, position_store=self.positions)
        client = create_app(runtime).test_client()
        html = client.get("/positions").get_data(as_text=True)
        self.assertIn("Spot buy", html)
        self.assertNotIn('value="short"', html)
        self.assertNotIn("BUY TO COVER SHORT", html)
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        response = client.post("/api/positions", json={"asset": "BTC", "side": "short", "entry": 115,
            "quantity": 2, "request_id": uuid.uuid4().hex}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.positions.snapshot()["positions"], [])

    def test_bearish_setup_can_only_create_a_lower_spot_buy(self):
        high, low = full_fixture("short")
        now = low[-1].end+1
        candidate, = scan(high, low, self.rules, "short")["entries"]
        engine = SMCEngine(ASSETS, self.rules, self.paper)
        quote = {"bid": low[-1].c-.01, "ask": low[-1].c+.01, "asof_ms": now}
        result = engine.evaluate("BTC", quote, high, low, now)
        order = result["smc_short"]["order"]
        self.assertEqual(order["side"], "long")
        self.assertEqual(order["limit"], candidate["target"])
        self.assertLess(order["limit"], candidate["limit"])
        self.assertTrue(result["smc_short"]["checks"])
        self.assertEqual(result["smc_short"]["entries"], [])
        self.assertIn("WAIT TO BUY", self.paper.snapshot()["outbox"][0]["text"])
        self.assertEqual(self.paper.snapshot()["cash"], self.rules.paper_equity)
        with self.assertRaisesRegex(DataError, "Spot mode"):
            entry_plan(candidate, candidate["limit"], self.rules, self.paper.snapshot())
        document = self.paper.snapshot()
        document["assets"]["BTC"]["pending"] = candidate
        advance_limit(document, "BTC", [], quote, self.rules, now)
        self.assertIsNone(document["assets"]["BTC"]["pending"])

    def test_spot_long_target_and_exit_are_profitable_after_modeled_costs(self):
        engine = SMCEngine(ASSETS, self.rules, self.paper)
        result = engine.evaluate("BTC", self.quote(self.low[-1].c), self.high, self.low, self.now)
        order = result["smc_long"]["order"]
        self.assertGreater(order["target"], order["limit"])
        result = engine.evaluate("BTC", self.quote(order["limit"]-.01, 1000), self.high, self.low, self.now+1000)
        trade = self.paper.snapshot()["assets"]["BTC"]["trades"][0]
        engine.evaluate("BTC", self.quote(trade["target"], 2000), self.high, self.low, self.now+2000)
        saved = self.paper.snapshot()
        self.assertGreater(saved["assets"]["BTC"]["trades"][0]["realized_pnl"], 0)
        self.assertIn("SELL TO EXIT LONG", saved["outbox"][-1]["text"])

    def test_correction_preserves_records_reverses_pnl_and_replaces_old_alert_ids(self):
        self.positions.market_mode = "margin"
        wrong = self.open("short")
        closed = self.open("short", entry=110)
        self.positions.close_position(closed["id"], 100, self.now-500)
        original_long = self.open("long")
        old_target = {"price": 99.99, "liquidity_price": 100, "sweep_buffer_bps": 1,
            "pivot_ms": self.high[0].t, "selected_ms": self.now-500, "setup_minutes": 30,
            "state": "watching", "reached_ms": None}
        def old_evidence(document):
            document["positions"][0]["take_profit"] = copy.deepcopy(old_target)
            for kind, status in (("near-tp", "sent"), ("target-hit", "queued")):
                queue_event(document, f"holding:{wrong['id']}:{kind}", "telegram", "BUY TO COVER SHORT", self.now-400, {"side": "short"})
                document["outbox"][-1].update(status=status, position_ids=[wrong["id"]],
                    alert_type="near_take_profit" if kind == "near-tp" else "target_reached")
        self.positions.transaction(old_evidence)
        before = self.positions.snapshot()
        self.positions.market_mode = "spot"
        corrected = self.positions.correct_spot_buys(self.now)
        self.assertEqual(set(corrected), {wrong["id"], closed["id"]})
        saved = self.positions.snapshot()
        self.assertEqual(saved["positions"][2], original_long)
        for old, new in zip(before["positions"], saved["positions"]):
            for key in ("id", "request_id", "entry", "quantity", "status", "close", "opened_ms", "closed_ms"):
                self.assertEqual(old[key], new[key])
            self.assertEqual(new["side"], "long")
        self.assertEqual(position_pnl(saved["positions"][1], 100)["pnl_usd"], -20)
        self.assertEqual(saved["positions"][0]["spot_correction"]["previous_take_profit"], old_target)
        backup = self.root/"backup"/"latest.zip"
        with ZipFile(backup) as archive:
            self.assertEqual(json.loads(archive.read("state/"+self.positions.path.name)), before)
        self.assertEqual([e["status"] for e in saved["outbox"]], ["sent", "cancelled"])
        self.assertTrue(all(e["retired"] and e["expires_ms"] == 0 for e in saved["outbox"]))
        self.assertEqual(self.positions.correct_spot_buys(self.now+1), [])
        self.assertEqual(list((self.root/"backup").iterdir()), [backup])
        self.positions.monitor_take_profit("BTC", "BTC/USD", self.high, self.low,
            self.quote(self.low[-1].c, 1), self.rules, self.now+1)
        position = self.positions.snapshot()["positions"][0]
        target = position["take_profit"]["price"]
        self.assertGreater(target, position["entry"])
        self.assertGreater(target, position["take_profit"]["liquidity_price"])
        self.positions.monitor_take_profit("BTC", "BTC/USD", [], [], self.quote(target*.9995, 2), self.rules, self.now+2)
        warning = next(e for e in self.positions.snapshot()["outbox"] if e["id"] == target_event_id(position, "near-tp"))
        self.assertEqual(warning["payload"]["action"], "prepare_sell")
        sender = Mock(return_value={"status": "sent"})
        self.assertTrue(dispatch_once(self.positions, "telegram", sender, self.now+2))
        self.assertNotIn("BUY TO COVER", sender.call_args.args[0]["text"])
        self.positions.monitor_take_profit("BTC", "BTC/USD", [], [], self.quote(target, 3), self.rules, self.now+3)
        hit = next(e for e in self.positions.snapshot()["outbox"] if e["id"] == target_event_id(position, "target-hit"))
        self.assertEqual(hit["payload"]["action"], "sell")
        restarted = PositionStore(self.positions.path)
        self.assertEqual(restarted.snapshot()["positions"][0]["take_profit"]["price"], target)
        self.assertTrue(all(e.get("retired") for e in restarted.snapshot()["outbox"][:2]))

    def test_mode_switch_cancels_pending_short_and_queued_cover_messages(self):
        high, low = full_fixture("short")
        now = low[-1].end+1
        margin = replace(self.rules, market_mode="margin")
        store = SMCStore(self.root/"margin.smc.json", ASSETS, margin)
        engine = SMCEngine(ASSETS, margin, store)
        quote = {"bid": low[-1].c-.01, "ask": low[-1].c+.01, "asof_ms": now}
        engine.evaluate("BTC", quote, high, low, now)
        self.assertEqual(store.snapshot()["assets"]["BTC"]["pending"]["side"], "short")
        cash = store.snapshot()["cash"]
        restarted = SMCStore(store.path, ASSETS, self.rules)
        self.assertIsNone(restarted.snapshot()["assets"]["BTC"]["pending"])
        self.assertEqual(restarted.snapshot()["cash"], cash)
        self.assertTrue(all(e["status"] == "cancelled" for e in restarted.snapshot()["outbox"]))


if __name__ == "__main__":
    unittest.main()
