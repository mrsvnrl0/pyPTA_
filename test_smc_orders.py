"""Offline chronology, persistence and long/short accounting for SMC paper limits."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from adaptive_crypto.core import Candle, DataError, M5, M30, Rules, check
from adaptive_crypto.notifications import dispatch_once
from adaptive_crypto.smc_engine import SMCEngine
from adaptive_crypto.smc_ledger import SMCStore, close_trade, entry_plan, open_trade, portfolio


ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}
BASE = 1_800_000_000


def feed(now, interval, price=110):
    return [Candle(t, price, price+1, price-1, price, 100, interval)
            for t in range(BASE, now//interval*interval, interval)]


class SMCOrderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/"study.smc.json"
        self.rules = Rules(strategy_model="smc_video", market_mode="margin", fee_rate=.001, slippage_rate=.002)
        self.store = SMCStore(self.path, ASSETS, self.rules)
        self.engine = SMCEngine(ASSETS, self.rules, self.store)
        self.now = BASE+12*M30+1234
        self.side = "long"
        self.source = self.candidate()
        scanner = patch("adaptive_crypto.smc_engine.scan", side_effect=self.scan)
        scanner.start()
        self.addCleanup(scanner.stop)
        offline = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Tests must stay offline"))
        offline.start()
        self.addCleanup(offline.stop)

    def candidate(self, side="long"):
        end = self.now//M5*M5-1
        key = "smc:"+side+":"+str(end)
        return {"setup_key": key, "side": side, "limit": 100,
                "stop": 90 if side == "long" else 110,
                "target": 130 if side == "long" else 70, "gross_r": 3,
                "method": "conservative", "signal_end": end, "signal_ms": end-M5+1,
                "checks": [check("fixture", "Completed evidence", True, 1, 1, end)],
                "setup": {"key": key, "bos_end": end-M30, "zone_low": 95, "zone_high": 105}}

    def scan(self, high, low, rules, side, consumed):
        entries = [copy.deepcopy(self.source)] if side == self.side and self.source["setup_key"] not in consumed else []
        return {"status": "TEST QUALIFICATION", "checks": copy.deepcopy(self.source["checks"]),
                "setup": copy.deepcopy(self.source["setup"]), "entries": entries}

    def quote(self, price=None, now=None):
        price = price if price is not None else (110 if self.side == "long" else 90)
        return {"bid": price-.01, "ask": price+.01, "asof_ms": now if now is not None else self.now}

    def evaluate(self, now=None, low=None, high=None, quote=None, errors=None):
        now = now if now is not None else self.now
        price = 110 if self.side == "long" else 90
        return self.engine.evaluate("BTC", self.quote(now=now) if quote is None else quote,
                                    feed(now, M30, price) if high is None else high,
                                    feed(now, M5, price) if low is None else low, now, errors)

    def record(self, side="long"):
        self.side = side
        self.source = self.candidate(side)
        self.evaluate()
        return self.store.snapshot()["assets"]["BTC"]["pending"]

    def bar_after_order(self, **changes):
        start = (self.now//M5+1)*M5
        baseline = Candle(start, 110, 111, 99, 108, 100, M5) if self.side == "long" else Candle(start, 90, 101, 89, 92, 100, M5)
        return replace(baseline, **changes)

    def evaluate_bar(self, bar, quote=None):
        now = bar.end+2
        low = feed(now, M5, 110 if self.side == "long" else 90)
        low[-1] = bar
        return self.evaluate(now=now, low=low, quote=quote)

    def trade(self):
        return self.store.snapshot()["assets"]["BTC"]["trades"][-1]

    def test_recorded_limit_waits_for_future_touch(self):
        order = self.record()
        self.assertIsNotNone(order)
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])
        self.assertEqual(self.store.snapshot()["cash"], self.rules.paper_equity)
        self.assertEqual(order["placed_ms"], self.now)
        self.assertGreater(order["placed_ms"], order["signal_end"])
        self.assertEqual([e["kind"] for e in self.store.snapshot()["outbox"]], ["telegram", "ai"])

    def test_current_candle_before_recording_cannot_fill_limit(self):
        self.record()
        bar = replace(self.bar_after_order(), t=self.now//M5*M5)
        self.evaluate_bar(bar)
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])
        self.assertIsNotNone(self.store.snapshot()["assets"]["BTC"]["pending"])
        full = self.bar_after_order()
        self.evaluate_bar(full)
        self.assertEqual(self.trade()["opened_ms"], full.t)
        self.assertEqual(self.trade()["entry"], 100)
        self.assertEqual(self.trade()["strategy"], "smc_long")

    def test_midpoint_already_reached_cannot_create_order(self):
        for side, price in (("long", 99), ("short", 101)):
            with self.subTest(side=side):
                self.side, self.source = side, self.candidate(side)
                result = self.evaluate(quote=self.quote(price))
                self.assertIn("already reached", result["smc_"+side]["entry_error"])
                self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])

    def test_live_target_already_consumed_cannot_create_order(self):
        for side, price in (("long", 131), ("short", 69)):
            with self.subTest(side=side):
                self.side, self.source = side, self.candidate(side)
                result = self.evaluate(quote=self.quote(price))
                self.assertIn("Liquidity target already reached", result["smc_"+side]["entry_error"])
                self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])

    def test_live_missed_touch_stays_retired_after_quote_recovery_and_restart(self):
        for side, kind, price in (("long", "midpoint", 99), ("long", "target", 131),
                                  ("short", "midpoint", 101), ("short", "target", 69)):
            with self.subTest(side=side, kind=kind):
                path = Path(self.temporary.name)/(side+kind+".json")
                self.store = SMCStore(path, ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                self.side, self.source = side, self.candidate(side)
                result = self.evaluate(quote=self.quote(price))
                self.assertIn("SETUP RETIRED", result["smc_"+side]["status"])
                self.assertEqual(self.store.snapshot()["assets"]["BTC"]["consumed"], [self.source["setup_key"]])
                self.evaluate(now=self.now+1)
                self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])
                self.store = SMCStore(path, ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                self.evaluate(now=self.now+2)
                self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])
                self.assertEqual(self.store.snapshot()["outbox"], [])

    def test_spread_and_cash_waiting_do_not_retire_untouched_setup(self):
        for reason in ("spread", "cash"):
            with self.subTest(reason=reason):
                self.store = SMCStore(Path(self.temporary.name)/(reason+".json"), ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                if reason == "cash":
                    self.store.transaction(lambda d: d.update(cash=0))
                quote = {"bid": 109, "ask": 111, "asof_ms": self.now} if reason == "spread" else self.quote()
                result = self.evaluate(quote=quote)
                self.assertIn("EXECUTION WAITING", result["smc_long"]["status"])
                self.assertEqual(self.store.snapshot()["assets"]["BTC"]["consumed"], [])
                self.store.transaction(lambda d: d.update(cash=self.rules.paper_equity))
                self.evaluate(now=self.now+1)
                self.assertIsNotNone(self.store.snapshot()["assets"]["BTC"]["pending"])

    def test_irreversible_touch_is_retired_even_during_wide_spread(self):
        result = self.evaluate(quote={"bid": 90, "ask": 99, "asof_ms": self.now})
        self.assertIn("SETUP RETIRED", result["smc_long"]["status"])
        self.evaluate(now=self.now+1)
        self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])

    def test_failed_retirement_commit_rolls_back_durable_observation(self):
        before, disk = self.store.snapshot(), self.path.read_bytes()
        with patch("adaptive_crypto.smc_ledger.atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.evaluate(quote=self.quote(99))
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.path.read_bytes(), disk)
        self.evaluate(quote=self.quote(99))
        self.evaluate(now=self.now+1)
        self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])

    def test_target_and_limit_same_bar_do_not_create_historical_profit(self):
        self.record()
        self.evaluate_bar(self.bar_after_order(h=131))
        data = self.store.snapshot()
        self.assertIsNone(data["assets"]["BTC"]["pending"])
        self.assertEqual(data["assets"]["BTC"]["trades"], [])
        self.assertEqual(data["cash"], self.rules.paper_equity)
        self.assertEqual(data["realized_pnl"], 0)

    def test_entry_bar_stop_uses_conservative_loss(self):
        self.record()
        self.evaluate_bar(self.bar_after_order(l=89))
        trade = self.trade()
        self.assertEqual(trade["status"], "stopped")
        self.assertAlmostEqual(trade["exit"], 90*(1-self.rules.slippage_rate))
        self.assertLess(trade["realized_pnl"], 0)

    def test_opening_gap_beyond_stop_cancels_unfilled_limit(self):
        self.record()
        self.evaluate_bar(self.bar_after_order(o=85, l=84))
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertEqual(record["trades"], [])
        self.assertIsNone(record["pending"])
        self.assertIn("opening price beyond stop", record["last_result"])

    def invalidation_feed(self, side="long", later_touch=False, earlier_touch=False, same_bar_touch=False):
        self.side, self.source = side, self.candidate(side)
        self.source["setup"].update(zone_low=105 if side == "long" else 85,
                                    zone_high=115 if side == "long" else 95)
        self.evaluate()
        start = self.now//M30*M30
        invalidation = Candle(start, 110, 111, 103, 104, 100, M30) if side == "long" else Candle(start, 90, 97, 89, 96, 100, M30)
        now = invalidation.end+2+(M5 if later_touch else 0)
        high = feed(now, M30, 110 if side == "long" else 90)
        high[-1] = invalidation
        low = feed(now, M5, 110 if side == "long" else 90)
        index = next(i for i, b in enumerate(low) if b.end == invalidation.end)
        low[index] = Candle(low[index].t, 110, 111, 103, 104, 100, M5) if side == "long" else Candle(low[index].t, 90, 97, 89, 96, 100, M5)
        if earlier_touch or later_touch or same_bar_touch:
            touch_start = (start+M5 if earlier_touch else invalidation.end+1 if later_touch else low[index].t)
            touch_index = next(i for i, b in enumerate(low) if b.t == touch_start)
            low[touch_index] = self.bar_after_order(t=touch_start)
            if same_bar_touch:
                low[touch_index] = replace(low[touch_index], c=invalidation.c)
        inside = [b for b in low if invalidation.t <= b.t <= invalidation.end]
        high[-1] = replace(invalidation, h=max(b.h for b in inside), l=min(b.l for b in inside))
        return now, high, low

    def test_order_block_invalidation_prevents_later_candle_or_quote_fill(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.store = SMCStore(Path(self.temporary.name)/(side+"-invalid.json"), ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                now, high, low = self.invalidation_feed(side, later_touch=True)
                self.evaluate(now=now, high=high, low=low, quote=self.quote(99 if side == "long" else 101, now))
                record = self.store.snapshot()["assets"]["BTC"]
                self.assertEqual(record["trades"], [])
                self.assertIsNone(record["pending"])
                self.assertIn("order block invalidated", record["last_result"])

    def test_order_block_close_does_not_cancel_an_earlier_verified_fill(self):
        for same_bar in (False, True):
            with self.subTest(same_bar=same_bar):
                self.store = SMCStore(Path(self.temporary.name)/("prior-"+str(same_bar)+".json"), ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                now, high, low = self.invalidation_feed(earlier_touch=not same_bar, same_bar_touch=same_bar)
                self.evaluate(now=now, high=high, low=low)
                self.assertEqual(self.trade()["status"], "active")
                self.assertLess(self.trade()["opened_ms"], high[-1].end)
                self.assertEqual(self.trade()["stop"], 90)

    def test_order_block_invalidation_during_outage_records_unknown_prior_fills(self):
        now, high, low = self.invalidation_feed()
        self.evaluate(now=now, high=high, low=[])
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertEqual(record["trades"], [])
        self.assertIsNone(record["pending"])
        self.assertIn("earlier fills are unknown", record["last_result"])

    def test_clock_error_cannot_apply_order_block_invalidation(self):
        now, high, low = self.invalidation_feed()
        self.evaluate(now=now, high=high, low=low, errors={"clock": "unverified"})
        self.assertIsNotNone(self.store.snapshot()["assets"]["BTC"]["pending"])
        self.evaluate(now=now, high=high, low=low)
        self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])

    def test_delayed_invalidation_preserves_fill_proven_by_stale_completed_history(self):
        now, high, low = self.invalidation_feed(earlier_touch=True)
        self.evaluate(now=now, high=high, low=low[:-2], quote={})
        self.assertEqual(self.trade()["status"], "active")
        self.assertTrue(self.trade()["tracking_gap"])

    def test_quote_fill_uses_executable_side_and_can_stop_on_spread(self):
        self.record()
        quote = {"bid": 89, "ask": 99, "asof_ms": self.now+1000}
        self.evaluate(now=self.now+1000, quote=quote)
        trade = self.trade()
        self.assertEqual(trade["entry"], 99)
        self.assertEqual(trade["status"], "stopped")
        self.assertAlmostEqual(trade["exit"], 89*(1-self.rules.slippage_rate))

    def test_partial_entry_candle_wick_cannot_retroactively_exit(self):
        self.record()
        self.evaluate(now=self.now+1000, quote=self.quote(99, self.now+1000))
        bar = replace(self.bar_after_order(h=150, l=80, c=105), t=self.now//M5*M5)
        self.evaluate_bar(bar)
        self.assertEqual(self.trade()["status"], "active")
        self.assertEqual(self.trade()["last_bar_end"], bar.end)

    def test_partial_entry_candle_close_can_prove_exit_after_fill(self):
        self.record()
        self.evaluate(now=self.now+1000, quote=self.quote(99, self.now+1000))
        bar = replace(self.bar_after_order(l=80, c=85), t=self.now//M5*M5)
        self.evaluate_bar(bar)
        self.assertEqual(self.trade()["status"], "stopped")
        self.assertAlmostEqual(self.trade()["exit"], 85*(1-self.rules.slippage_rate))

    def test_full_post_entry_bar_with_stop_and_target_uses_stop(self):
        self.record()
        first = self.bar_after_order()
        self.evaluate_bar(first)
        self.evaluate_bar(replace(first, t=first.t+M5, l=89, h=131))
        self.assertEqual(self.trade()["status"], "stopped")
        self.assertAlmostEqual(self.trade()["exit"], 90*(1-self.rules.slippage_rate))

    def test_long_and_short_collateral_fees_and_target_accounting(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                self.store = SMCStore(Path(self.temporary.name)/(side+".json"), ASSETS, self.rules)
                self.engine = SMCEngine(ASSETS, self.rules, self.store)
                self.record(side)
                first = self.bar_after_order()
                self.evaluate_bar(first)
                trade, opened = self.trade(), self.store.snapshot()
                q = trade["quantity"]
                self.assertAlmostEqual(opened["cash"], 1000-q*100-q*100*self.rules.fee_rate)
                self.assertAlmostEqual(portfolio(opened)["cost_equity"], 1000-q*100*self.rules.fee_rate)
                self.assertLess(opened["cash"], 1000, "Short sale proceeds must not increase available cash")
                exit_bar = replace(first, t=first.t+M5, h=131, l=101, o=110) if side == "long" else replace(first, t=first.t+M5, l=69, h=99, o=90)
                self.evaluate_bar(exit_bar)
                closed = self.trade()
                target = 130 if side == "long" else 70
                pnl = 30*q-(100+target)*q*self.rules.fee_rate
                self.assertEqual(closed["status"], "target")
                self.assertAlmostEqual(closed["realized_pnl"], pnl)
                self.assertAlmostEqual(self.store.snapshot()["cash"], 1000+pnl)
                self.assertEqual(portfolio(self.store.snapshot())["open_risk"], 0)
                self.assertEqual(portfolio(self.store.snapshot())["active_positions"], 0)

    def test_short_stop_includes_adverse_slippage_and_all_costs(self):
        self.record("short")
        self.evaluate_bar(self.bar_after_order(h=111))
        trade = self.trade()
        exit_price = 110*(1+self.rules.slippage_rate)
        expected = (100-exit_price)*trade["quantity"]-(100+exit_price)*trade["quantity"]*self.rules.fee_rate
        self.assertEqual(trade["status"], "stopped")
        self.assertAlmostEqual(trade["exit"], exit_price)
        self.assertAlmostEqual(trade["realized_pnl"], expected)
        self.assertAlmostEqual(self.store.snapshot()["cash"], 1000+expected)

    def test_entry_plan_caps_risk_cash_allocation_and_fee_adjusted_reward(self):
        source = self.candidate()
        document = self.store.snapshot()
        plan = entry_plan(source, 100, self.rules, document)
        stop_fill = 90*(1-self.rules.slippage_rate)
        risk_unit = 100-stop_fill+self.rules.fee_rate*(100+stop_fill)
        reward_unit = 30-self.rules.fee_rate*230
        self.assertAlmostEqual(plan["initial_risk_usd"], 1000*self.rules.risk_per_trade)
        self.assertAlmostEqual(plan["net_r"], reward_unit/risk_unit)
        self.assertLessEqual(plan["collateral"]+plan["entry_fee"], 1000*self.rules.max_allocation)
        document["cash"] = 5
        with self.assertRaises(DataError):
            entry_plan(source, 100, self.rules, document)
        with self.assertRaises(DataError):
            entry_plan(source, 89, self.rules, self.store.snapshot())

    def test_limit_fill_rechecks_shared_cash_and_risk_budget(self):
        self.record()
        self.store.transaction(lambda d: d.update(cash=0))
        self.evaluate_bar(self.bar_after_order())
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertIsNone(record["pending"])
        self.assertEqual(record["trades"], [])
        self.assertIn("Insufficient", record["last_result"])

    def test_zero_minimum_notional_does_not_enable_zero_budget_trade(self):
        document = self.store.snapshot()
        document["cash"] = 0
        with self.assertRaises(DataError):
            entry_plan(self.candidate(), 100, replace(self.rules, minimum_notional=0), document)

    def test_portfolio_risk_budget_includes_other_assets_open_risk(self):
        self.record()
        self.evaluate_bar(self.bar_after_order())
        document = self.store.snapshot()
        tight_rules = replace(self.rules, max_total_risk=self.rules.risk_per_trade)
        with self.assertRaises(DataError):
            entry_plan(self.candidate("short"), 100, tight_rules, document)

    def test_invalid_stale_or_future_quote_cannot_fill_pending_limit(self):
        self.record()
        for quote in ({"bid": 99, "ask": 99.01, "asof_ms": self.now-31000},
                      {"bid": 99, "ask": 99.01, "asof_ms": self.now+3000},
                      {"bid": 100, "ask": 99, "asof_ms": self.now},
                      {"bid": float("nan"), "ask": 99, "asof_ms": self.now}):
            with self.subTest(quote=quote):
                self.evaluate(quote=quote)
                self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])
                self.assertIsNotNone(self.store.snapshot()["assets"]["BTC"]["pending"])

    def test_restart_deduplicates_order_fill_close_and_notifications(self):
        order = self.record()
        self.store = SMCStore(self.path, ASSETS, self.rules)
        self.engine = SMCEngine(ASSETS, self.rules, self.store)
        self.evaluate()
        self.assertEqual(len(self.store.snapshot()["outbox"]), 2)
        first = self.bar_after_order()
        self.evaluate_bar(first)
        self.store = SMCStore(self.path, ASSETS, self.rules)
        self.engine = SMCEngine(ASSETS, self.rules, self.store)
        self.evaluate_bar(first)
        exit_bar = replace(first, t=first.t+M5, h=131, l=101)
        self.evaluate_bar(exit_bar)
        before = self.store.snapshot()
        self.evaluate_bar(exit_bar)
        self.assertEqual(before, self.store.snapshot())
        self.assertEqual(len(before["assets"]["BTC"]["trades"]), 1)
        self.assertEqual(before["assets"]["BTC"]["consumed"], [order["setup_key"]])
        self.assertEqual(len(before["outbox"]), 4)
        sender = Mock(return_value={"status": "sent", "message_id": 7})
        while dispatch_once(self.store, "telegram", sender, exit_bar.end+10):
            pass
        self.assertEqual(sender.call_count, 3)
        self.store = SMCStore(self.path, ASSETS, self.rules)
        self.assertFalse(dispatch_once(self.store, "telegram", sender, exit_bar.end+10))

    def test_lifecycle_helpers_are_idempotent_and_reject_backdated_fill(self):
        order = self.record()
        with self.assertRaises(DataError):
            self.store.transaction(lambda d: open_trade(d, "BTC", order, 100, self.rules, self.now-1))
        self.evaluate_bar(self.bar_after_order())
        before = self.store.snapshot()
        self.store.transaction(lambda d: open_trade(d, "BTC", order, 100, self.rules, self.now+M5))
        self.assertEqual(before, self.store.snapshot())
        def close(d):
            trade = d["assets"]["BTC"]["trades"][0]
            close_trade(d, "BTC", trade, 130, "target", self.now+3*M5)
            close_trade(d, "BTC", trade, 130, "target", self.now+3*M5)
        self.store.transaction(close)
        expected = 1000+self.trade()["realized_pnl"]
        self.assertAlmostEqual(self.store.snapshot()["cash"], expected)

    def test_stale_forming_missing_and_clock_errors_prevent_entries(self):
        for mode in ("stale", "forming", "missing", "clock", "quote", "source"):
            with self.subTest(mode=mode):
                high, low, errors, quote = feed(self.now, M30), feed(self.now, M5), {}, self.quote()
                if mode == "stale":
                    low = low[:-1]
                elif mode == "forming":
                    low.append(replace(low[-1], t=low[-1].t+M5))
                elif mode == "missing":
                    low.pop(-2)
                elif mode == "clock":
                    errors["clock"] = "unverified"
                elif mode == "source":
                    errors["5m"] = "endpoint failed"
                else:
                    quote["asof_ms"] -= 31000
                self.evaluate(high=high, low=low, quote=quote, errors=errors)
                self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])
                self.assertEqual(self.store.snapshot()["outbox"], [])

    def test_clock_error_cannot_fill_or_exit_from_unverified_candles(self):
        self.record()
        first = self.bar_after_order()
        low = feed(first.end+2, M5)
        low[-1] = first
        self.evaluate(now=first.end+2, low=low, errors={"clock": "unverified"})
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])
        self.evaluate_bar(first)
        later = replace(first, t=first.t+M5, l=80)
        low = feed(later.end+2, M5)
        low[-1] = later
        self.evaluate(now=later.end+2, low=low, errors={"clock": "unverified"})
        self.assertEqual(self.trade()["status"], "active")

    def test_stale_but_valid_completed_bars_can_prove_existing_stop(self):
        self.record()
        first = self.bar_after_order()
        self.evaluate_bar(first)
        stop = replace(first, t=first.t+M5, l=85)
        low = feed(stop.end+2, M5)
        low[-1] = stop
        self.evaluate(now=stop.end+M5*3, low=low, quote={})
        self.assertEqual(self.trade()["status"], "stopped")

    def test_missing_whole_candle_history_cancels_limit(self):
        self.record()
        now = self.now+10*M5
        low = feed(now, M5)[-6:]
        self.evaluate(now=now, low=low)
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertIsNone(record["pending"])
        self.assertEqual(record["trades"], [])
        self.assertIn("missing entry-timeframe history", record["last_result"])

    def test_feed_outage_keeps_fresh_quote_stop_monitoring(self):
        self.record()
        self.evaluate_bar(self.bar_after_order())
        now = self.now+5*M5
        self.evaluate(now=now, low=[], quote=self.quote(85, now))
        self.assertEqual(self.trade()["status"], "stopped")
        self.assertTrue(self.trade()["tracking_gap"])

    def test_settings_change_cancels_limit_and_preserves_cash_trades_costs(self):
        self.record()
        self.evaluate_bar(self.bar_after_order())
        before = self.store.snapshot()
        altered = replace(self.rules, fee_rate=.01, paper_equity=1500)
        reopened = SMCStore(self.path, ASSETS, altered).snapshot()
        self.assertEqual(reopened["cash"], before["cash"])
        self.assertEqual(reopened["assets"]["BTC"]["trades"], before["assets"]["BTC"]["trades"])
        self.assertEqual(reopened["assets"]["BTC"]["trades"][0]["fee_rate"], self.rules.fee_rate)
        self.assertTrue((self.path.parent/"backup"/"latest.zip").is_file())
        self.assertIn("Existing paper trades retain", reopened["warnings"][-1])

    def test_settings_change_cancels_pending_without_releasing_unreserved_cash(self):
        self.record()
        reopened = SMCStore(self.path, ASSETS, replace(self.rules, smc_stop_buffer_bps=2)).snapshot()
        self.assertIsNone(reopened["assets"]["BTC"]["pending"])
        self.assertEqual(reopened["cash"], self.rules.paper_equity)
        self.assertEqual(len(reopened["assets"]["BTC"]["consumed"]), 1)

    def test_restart_keeps_uncertain_telegram_delivery_from_being_resent(self):
        self.record()
        def running(d):
            for event in d["outbox"]:
                event.update(status="running", attempts=1)
        self.store.transaction(running)
        data = SMCStore(self.path, ASSETS, self.rules).snapshot()
        self.assertEqual([e["status"] for e in data["outbox"]], ["uncertain", "queued"])

    def test_failed_save_rolls_back_order_cash_consumption_and_outbox(self):
        before, disk = self.store.snapshot(), self.path.read_bytes()
        with patch("adaptive_crypto.smc_ledger.atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.evaluate()
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.path.read_bytes(), disk)
        self.record()
        before, disk = self.store.snapshot(), self.path.read_bytes()
        with patch("adaptive_crypto.smc_ledger.atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.evaluate_bar(self.bar_after_order())
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.path.read_bytes(), disk)

    def test_damaged_study_is_not_overwritten(self):
        self.record()
        damaged = self.store.snapshot()
        damaged["assets"]["BTC"]["pending"]["placed_ms"] = damaged["assets"]["BTC"]["pending"]["signal_end"]-1
        self.path.write_text(json.dumps(damaged), encoding="utf-8")
        disk = self.path.read_bytes()
        with self.assertRaises(DataError):
            SMCStore(self.path, ASSETS, self.rules)
        self.assertEqual(self.path.read_bytes(), disk)


if __name__ == "__main__":
    unittest.main()
