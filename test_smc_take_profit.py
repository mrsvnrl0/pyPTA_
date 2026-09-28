"""Offline sweep-target boundaries, execution, evidence and saved-ledger compatibility."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from adaptive_crypto.core import Candle, DataError, M5, Rules
from adaptive_crypto.smc import scan
from adaptive_crypto.smc_engine import SMCEngine
from adaptive_crypto.smc_ledger import SMCStore, monitor_trades
from adaptive_crypto.web import measurement_rows
from test_smc_formulas import full_fixture


ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


class SweepTakeProfitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.rules = Rules(strategy_model="smc_video", market_mode="margin", smc_pivot_strength=1,
                           smc_tp_sweep_buffer_bps=10, fee_rate=.001)
        offline = patch.object(requests.sessions.Session, "request",
                               side_effect=AssertionError("Tests must stay offline"))
        offline.start()
        self.addCleanup(offline.stop)

    def start(self, side):
        self.side = side
        self.high, self.low = full_fixture(side)
        self.now = self.low[-1].end+1
        self.path = self.directory/(side+".smc.json")
        self.store = SMCStore(self.path, ASSETS, self.rules)
        self.engine = SMCEngine(ASSETS, self.rules, self.store)
        self.candidate, = scan(self.high, self.low, self.rules, side)["entries"]

    def evaluate(self, mark, offset=0, entry=False, low=None):
        # Exit observations use bid for a long and ask for a short.
        quote = {"bid": mark if self.side == "long" else mark-.01,
                 "ask": mark+.01 if self.side == "long" else mark,
                 "asof_ms": self.now+offset}
        if entry:
            quote = {"bid": mark-.01 if self.side == "long" else mark,
                     "ask": mark if self.side == "long" else mark+.01,
                     "asof_ms": self.now+offset}
        return self.engine.evaluate("BTC", quote, self.high,
                                    self.low if low is None else low, self.now+offset)

    def record(self):
        self.evaluate(self.low[-1].c)
        self.assertIsNotNone(self.store.snapshot()["assets"]["BTC"]["pending"])

    def fill(self):
        self.record()
        self.evaluate(self.candidate["limit"], 1000, entry=True)
        self.assertEqual(self.trade()["status"], "active")

    def trade(self):
        return self.store.snapshot()["assets"]["BTC"]["trades"][-1]

    def test_custom_buffer_preserves_key_level_and_exposes_formula_with_units(self):
        for side, level, target in (("long", 140, 140.14), ("short", 110, 109.89)):
            with self.subTest(side=side):
                self.start(side)
                entry = self.candidate
                self.assertEqual(entry["target_liquidity_price"], level)
                self.assertAlmostEqual(entry["target"], target)
                self.assertEqual(entry["target_sweep_buffer_bps"], 10)
                evidence = next(c for c in entry["checks"] if c["key"] == "smc_target")
                self.assertEqual(dict(measurement_rows(evidence, places=2)), {
                    "Liquidity level": f"${level:.2f}", "Sweep beyond liquidity": "10.00 bps",
                    "Sweep take-profit": f"${target:.2f}"})
                self.assertIn("exit at sweep price", evidence["required"])
                self.record()
                message = self.store.snapshot()["outbox"][0]["text"]
                self.assertIn(f"${target:,.8f}", message)
                self.assertIn("sweep buffer 10 bps", message)

    def test_sweep_buffer_requires_a_finite_positive_json_number(self):
        for value in (0, -1, 100.01, True, "1", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(DataError):
                replace(self.rules, smc_tp_sweep_buffer_bps=value).validate()
        for value in (.5, 100):
            replace(self.rules, smc_tp_sweep_buffer_bps=value).validate()

    def test_raw_liquidity_consumed_before_order_stays_retired_after_restart(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.start(side)
                self.evaluate(self.candidate["target_liquidity_price"])
                before = self.store.snapshot()
                self.assertIsNone(before["assets"]["BTC"]["pending"])
                self.assertIn(self.candidate["setup_key"], before["assets"]["BTC"]["consumed"])
                self.store = SMCStore(self.path, ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                self.evaluate(self.low[-1].c, 1000)
                self.assertEqual(self.store.snapshot(), before)
                self.assertEqual(before["outbox"], [])

    def test_pending_limit_cancels_at_raw_liquidity_before_sweep_price(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.start(side)
                self.record()
                self.evaluate(self.candidate["target_liquidity_price"], 1000)
                state = self.store.snapshot()
                self.assertIsNone(state["assets"]["BTC"]["pending"])
                self.assertEqual(state["assets"]["BTC"]["trades"], [])
                self.assertEqual(state["cash"], self.rules.paper_equity)
                self.assertIn("before entry", state["assets"]["BTC"]["last_result"])

    def test_bar_touching_limit_and_raw_liquidity_cannot_backfill_a_profit(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.start(side)
                self.record()
                midpoint = self.candidate["limit"]
                level = self.candidate["target_liquidity_price"]
                price = self.low[-1].c
                partial = Candle(self.now, price, price, price, price, 100, M5)
                ambiguous = Candle(self.now+M5, price, max(midpoint, level),
                                   min(midpoint, level), price, 100, M5)
                self.evaluate(price, 2*M5, low=self.low+[partial, ambiguous])
                state = self.store.snapshot()
                self.assertIsNone(state["assets"]["BTC"]["pending"])
                self.assertEqual(state["assets"]["BTC"]["trades"], [])
                self.assertIn("target consumed", state["assets"]["BTC"]["last_result"])

    def test_filled_trade_waits_for_sweep_then_exits_without_reversal_once(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.start(side)
                self.fill()
                level, target = self.candidate["target_liquidity_price"], self.candidate["target"]
                for offset, price in ((2000, level), (3000, (level+target)/2)):
                    self.evaluate(price, offset)
                    self.assertEqual(self.trade()["status"], "active")
                self.store = SMCStore(self.path, ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                self.evaluate(target, 4000)
                trade = self.trade()
                self.assertEqual(trade["status"], "target")
                self.assertEqual(trade["exit"], target)
                sign = 1 if side == "long" else -1
                pnl = (sign*(target-trade["entry"])-self.rules.fee_rate*(trade["entry"]+target))*trade["quantity"]
                self.assertAlmostEqual(trade["realized_pnl"], pnl)
                self.assertAlmostEqual(self.store.snapshot()["cash"], self.rules.paper_equity+pnl)
                before = self.store.snapshot()
                self.store = SMCStore(self.path, ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                self.evaluate(target, 5000)
                self.assertEqual(self.store.snapshot(), before)

    def test_completed_wick_requires_sweep_price_and_preserves_stop_precedence(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.start(side)
                self.fill()
                source = self.store.snapshot()
                level, target = self.candidate["target_liquidity_price"], self.candidate["target"]
                price = self.low[-1].c
                for boundary, status in ((level, "active"), (target, "target")):
                    document = copy.deepcopy(source)
                    bar = Candle(self.now+M5, price, max(price, boundary),
                                 min(price, boundary), price, 100, M5)
                    monitor_trades(document, "BTC", [bar], None, bar.end+1)
                    trade = document["assets"]["BTC"]["trades"][-1]
                    self.assertEqual(trade["status"], status)
                    if status == "target":
                        self.assertEqual(trade["exit"], target)
                document = copy.deepcopy(source)
                stop = self.candidate["stop"]
                bar = replace(bar, h=max(stop, target), l=min(stop, target))
                monitor_trades(document, "BTC", [bar], None, bar.end+1)
                self.assertEqual(document["assets"]["BTC"]["trades"][-1]["status"], "stopped")

    def test_changing_buffer_cancels_pending_and_preserves_filled_target_and_costs(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.start(side)
                self.record()
                pending = self.store.snapshot()
                altered = replace(self.rules, smc_tp_sweep_buffer_bps=20, fee_rate=.002)
                reopened = SMCStore(self.path, ASSETS, altered).snapshot()
                self.assertIsNone(reopened["assets"]["BTC"]["pending"])
                self.assertEqual(reopened["cash"], pending["cash"])
                self.assertEqual(reopened["assets"]["BTC"]["consumed"], pending["assets"]["BTC"]["consumed"])
                # Restore only the disposable fixture to exercise the filled case.
                self.path.write_text(json.dumps(pending), encoding="utf-8")
                self.store = SMCStore(self.path, ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                self.evaluate(self.candidate["limit"], 1000, entry=True)
                before = self.store.snapshot()
                reopened = SMCStore(self.path, ASSETS, altered).snapshot()
                self.assertEqual(reopened["assets"]["BTC"]["trades"], before["assets"]["BTC"]["trades"])
                self.assertEqual(reopened["cash"], before["cash"])
                self.assertTrue((self.directory/"backup"/"latest.zip").is_file())

    def test_legacy_filled_target_loads_without_extension_or_new_metadata(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.start(side)
                self.fill()
                old = self.store.snapshot()
                del old["settings"]["smc_tp_sweep_buffer_bps"]
                trade = old["assets"]["BTC"]["trades"][0]
                trade["target"] = trade.pop("target_liquidity_price")
                del trade["target_sweep_buffer_bps"]
                self.path.write_text(json.dumps(old), encoding="utf-8")
                restored = SMCStore(self.path, ASSETS, self.rules).snapshot()
                self.assertEqual(restored["assets"]["BTC"]["trades"], old["assets"]["BTC"]["trades"])
                self.assertEqual(restored["cash"], old["cash"])

    def test_inconsistent_or_incomplete_saved_sweep_target_fails_without_overwrite(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.start(side)
                self.record()
                source = self.store.snapshot()
                for field, value in (("target", self.candidate["target_liquidity_price"]),
                                     ("target_sweep_buffer_bps", 20), ("target_sweep_buffer_bps", True),
                                     ("target_liquidity_price", None)):
                    damaged = copy.deepcopy(source)
                    damaged["assets"]["BTC"]["pending"][field] = value
                    self.path.write_text(json.dumps(damaged), encoding="utf-8")
                    before = self.path.read_bytes()
                    with self.assertRaises(DataError):
                        SMCStore(self.path, ASSETS, self.rules)
                    self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
