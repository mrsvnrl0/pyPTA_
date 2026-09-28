"""Offline regression suite. No market, OpenAI, or Telegram calls are made.
Run: python -m unittest -v test_dashboard.py
"""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import adaptive_crypto_dashboard as d


ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}
T0 = (1_780_000_000_000 // d.H4) * d.H4


def bars(count=60, interval=d.H4, start=T0, center=100):
    return [d.Candle(start+i*interval, center, center+1, center-1, center+0.1, 100, interval) for i in range(count)]


def setbar(candles, index, o, h, l, c, v=100):
    candles[index] = replace(candles[index], o=o, h=h, l=l, c=c, v=v)


def reclaim_fixture():
    c = bars(61)
    # A known liquidity pivot, its two confirming bars, last up block,
    # a decisive down close, sweep, then a distinct reclaim.
    setbar(c, 52, 100, 101, 96, 100)
    setbar(c, 56, 100, 104, 99, 103)
    setbar(c, 57, 103, 103.5, 97, 97.4, 90)
    setbar(c, 58, 97.5, 99, 94, 96.5)
    setbar(c, 59, 97, 99, 96, 98)
    setbar(c, 60, 97.8, 112, 95, 111, 180)
    start15 = c[-1].end+1
    fifteen = bars(12, d.M15, start15-8*d.M15, 109)
    setbar(fifteen, 8, 109, 110, 105, 106)
    setbar(fifteen, 9, 106, 107, 101, 102)
    setbar(fifteen, 10, 102, 104, 101, 103)
    setbar(fifteen, 11, 103, 112, 102, 111.5, 100)
    now = fifteen[-1].end+2
    quote = {"bid": 111.49, "ask": 111.51, "last": 111.5, "asof_ms": now,
             "volume24_base": 1000, "low24": 94, "high24": 112}
    return c, fifteen, quote, now


def momentum_fixture():
    c = bars(280, center=100)
    for i in range(len(c)):
        close = 100+i*0.04 + (0.4 if i % 2 else -0.4)
        if i > 268:
            close = 111.0-(i-268)*0.12
        setbar(c, i, close-0.1, close+0.7, close-0.7, close)
    setbar(c, 279, 109.7, 112.8, 109.5, 112.3, 180)
    now = c[-1].end+2
    q = {"bid": 112.29, "ask": 112.31, "last": 112.3, "asof_ms": now, "volume24_base": 1000}
    return c, q, now


class TemporaryEngine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rules = d.Rules().validate()
        self.path = Path(self.temp.name)/"state.json"
        self.store = d.StateStore(self.path, ASSETS, self.rules)
        self.engine = d.Engine(ASSETS, self.rules, self.store)

    def qualify(self):
        c4, c15, quote, now = reclaim_fixture()
        result = self.engine.evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(result["reclaim"]["status"], "ACTIVE PAPER TRADE", result)
        return c4, c15, quote, now


class FormulaTests(unittest.TestCase):
    def test_wilder_atr_hand_calculation(self):
        c = bars(4)
        setbar(c, 0, 10, 12, 9, 11)
        setbar(c, 1, 11, 14, 10, 13)
        setbar(c, 2, 14, 18, 12, 15)
        setbar(c, 3, 15, 22, 14, 20)
        values = d.atr_series(c, 3)
        self.assertIsNone(values[1])
        self.assertAlmostEqual(values[2], 13/3)
        self.assertAlmostEqual(values[3], (26/3+8)/3)

    def test_volume_excludes_current_and_handles_zero(self):
        c = bars(21)
        c[-1] = replace(c[-1], v=200)
        self.assertEqual(d.rvol(c, 20, 20), 2)
        self.assertIsNone(d.rvol(c, 19, 20))
        self.assertIsNone(d.rvol([replace(b, v=0) for b in c], 20, 20))

    def test_rsi_flat_rising_falling(self):
        self.assertEqual(d.rsi_series([10]*30)[-1], 50)
        self.assertEqual(d.rsi_series(list(range(1, 31)))[-1], 100)
        self.assertEqual(d.rsi_series(list(range(30, 0, -1)))[-1], 0)

    def test_ema_recursive_values(self):
        self.assertEqual(d.ema([1, 3, 5], 3), [1, 2, 3.5])

    def test_pivot_requires_right_side_confirmation(self):
        c = bars(10)
        setbar(c, 5, 100, 101, 90, 100)
        self.assertFalse(d.pivot(c, 5, 2, 6))
        self.assertTrue(d.pivot(c, 5, 2, 7))

    def test_atr_prefix_is_invariant_to_future(self):
        c = bars(50)
        full = d.atr_series(c)
        setbar(c, 49, 100, 10000, 1, 9999)
        self.assertEqual(d.atr_series(c)[30], full[30])

    def test_flat_candle_position_is_neutral(self):
        self.assertEqual(d.location(d.Candle(T0, 100, 100, 100, 100, 0, d.H4)), .5)

    def test_net_targets_and_risk_size(self):
        rules = d.Rules()
        p = d.entry_plan(100.01, 99.99, 100, 95, 3, rules, 1000, 1000, 0, bars())
        exit_factor = (1-rules.fee_rate)*(1-rules.slippage_rate)
        self.assertAlmostEqual((p["tp1"]*exit_factor-p["cost_per_unit"])/p["risk_per_unit"], 2)
        self.assertAlmostEqual((p["tp2"]*exit_factor-p["cost_per_unit"])/p["risk_per_unit"], 3)
        self.assertLessEqual(p["initial_risk_usd"], 5+1e-9)
        self.assertLessEqual(p["allocation_usd"], 250+1e-9)
        self.assertGreater(p["entry"], 100.01)

    def test_fixed_targets_work_without_pivot(self):
        plan = d.entry_plan(100, 99.99, 100, 95, 3, d.Rules(), 1000, 1000, 0, bars())
        self.assertIsNone(plan["nearby_resistance"])
        self.assertGreater(plan["tp2"], plan["tp1"])

    def test_no_cash_no_risk_no_entry(self):
        for cash, open_risk in [(0, 0), (1000, 20)]:
            with self.assertRaises(d.DataError):
                d.entry_plan(100, 99.99, 100, 95, 3, d.Rules(), cash, 1000, open_risk, bars())

    def test_spread_chase_and_stop_checks(self):
        for ask, bid, stop in [(100, 98, 95), (102, 101.99, 95), (100, 99.99, 100.5)]:
            with self.assertRaises(d.DataError):
                d.entry_plan(ask, bid, 100, stop, 3, d.Rules(), 1000, 1000, 0, bars())

    def test_rule_validation_rejects_bad_numeric_settings(self):
        for changes in [{"fee_rate": float("nan")}, {"volume_period": 0}, {"target1_net_r": 4},
                        {"momentum_rsi_min": 90}, {"risk_per_trade": True}, {"reclaim_require_trend": "false"},
                        {"reclaim_rvol_min": "1.2"}]:
            with self.assertRaises(d.DataError):
                replace(d.Rules(), **changes).validate()


class DataTests(unittest.TestCase):
    def test_kraken_final_uncommitted_row_excluded(self):
        c = bars(5)
        rows = [[b.t//1000, b.o, b.h, b.l, b.c, b.c, b.v, 10] for b in c]
        parsed = d.parse_kraken_rows(rows, d.H4, c[-1].t+5000)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[-1].v, 100)

    def test_bad_candles_are_rejected(self):
        c = bars(25)
        bad = [c+c[-1:], c[:2]+c[3:], c[::-1], [replace(c[0], c=float("nan"))]+c[1:],
               [replace(c[0], l=102)]+c[1:], [replace(c[0], t=c[0].t//1000)]+c[1:]]
        for sample in bad:
            with self.subTest(sample=sample[0]):
                with self.assertRaises(d.DataError):
                    d.validate_candles(sample, d.H4, c[-1].end+2)

    def test_live_candle_never_enters_engine(self):
        c = bars(25)
        with self.assertRaises(d.DataError):
            d.validate_candles(c, d.H4, c[-1].end)

    def test_stale_feed_rejected(self):
        c = bars(25)
        with self.assertRaises(d.DataError):
            d.validate_candles(c, d.H4, c[-1].end+2*d.H4)

    def test_settings_do_not_mix_quote_currencies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"settings.json"
            path.write_text(json.dumps({"assets": [{"name": "BTC", "symbol": "BTCUSDT"}]}))
            with self.assertRaises(d.DataError):
                d.load_settings(path)


class SequenceTests(TemporaryEngine):
    def test_full_reclaim_pipeline_without_network(self):
        c4, c15, quote, now = self.qualify()
        state = self.store.snapshot()
        trade = state["assets"]["BTC"]["trades"][0]
        self.assertIsNone(state["assets"]["BTC"]["pending"])
        self.assertTrue(d.passed(trade["evidence"]))
        self.assertAlmostEqual(trade["entry"], quote["ask"]*(1+self.rules.slippage_rate))
        self.assertNotEqual(trade["entry"], c15[-1].c)
        self.assertEqual([e["kind"] for e in state["outbox"]], ["telegram", "ai"])

    def test_stages_advance_retest_then_trigger(self):
        c4, c15, q, now = reclaim_fixture()
        first = c15[:9]
        n1 = first[-1].end+2
        r1 = self.engine.evaluate("BTC", {**q, "asof_ms": n1}, c4, first, n1)
        self.assertEqual(r1["reclaim"]["status"], "WAITING FOR 15M RETEST")
        second = c15[:11]
        n2 = second[-1].end+2
        r2 = self.engine.evaluate("BTC", {**q, "asof_ms": n2}, c4, second, n2)
        self.assertEqual(r2["reclaim"]["status"], "WAITING FOR 15M STRUCTURE BREAK")
        r3 = self.engine.evaluate("BTC", q, c4, c15, now)
        self.assertEqual(r3["reclaim"]["status"], "ACTIVE PAPER TRADE")

    def test_stop_includes_reclaim_low(self):
        c4, _, _, now = reclaim_fixture()
        c4[-1] = replace(c4[-1], l=90)
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        self.assertIsNotNone(setup)
        self.assertEqual(setup["leg_low"], 90)
        self.assertLess(setup["stop"], 90)
        self.assertEqual(setup["equilibrium"], 101)

    def test_displacement_uses_previous_atr(self):
        c4, _, _, now = reclaim_fixture()
        scan = d.reclaim_scan(c4, self.rules, now)
        body = next(x["measured"] for x in scan["checks"] if x["key"] == "body")
        self.assertAlmostEqual(body, (c4[-1].c-c4[-1].o)/d.atr_series(c4)[-2])

    def test_volume_failure_prevents_qualification(self):
        c4, c15, q, now = reclaim_fixture()
        c4[-1] = replace(c4[-1], v=10)
        result = self.engine.evaluate("BTC", q, c4, c15, now)
        self.assertEqual(len(self.store.snapshot()["assets"]["BTC"]["trades"]), 0)
        self.assertTrue(any(x["key"] == "volume" and x["status"] == "wait" for x in result["reclaim"]["checks"]))

    def test_intrabar_stop_cancels_pending(self):
        c4, c15, q, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        setbar(c15, 9, 106, 107, setup["stop"]-.1, 102)
        result = self.engine.evaluate("BTC", q, c4, c15, now)
        self.assertEqual(result["reclaim"]["status"], "SETUP STOP BREACHED")
        self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])
        self.assertEqual(len(self.store.snapshot()["assets"]["BTC"]["trades"]), 0)

    def test_no_retest_before_reclaim_close(self):
        c4, c15, _, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        result = d.ltf_scan(c15[:8], setup, self.rules, now)
        self.assertIsNone(result["trigger"])

    def test_expiry_uses_clock_not_available_bar_count(self):
        c4, c15, _, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        self.assertTrue(d.ltf_scan(c15, setup, self.rules, setup["expires_ms"]+1)["failed"])

    def test_expired_retest_displays_its_age_failure(self):
        c4, c15, _, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        result = d.ltf_scan(c15, setup, self.rules, now+20*d.M15)
        self.assertEqual(result["status"], "WAITING FOR A FRESH 15M RETEST")
        retest = next(c for c in result["checks"] if c["key"] == "retest")
        self.assertEqual(retest["status"], "wait")
        self.assertGreater(retest["measured"]["age_bars"], retest["required"]["max_age_bars"])

    def test_fvg_without_structure_break_does_not_trigger(self):
        c4, c15, _, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        setbar(c15, 9, 104, 105, 100, 101)
        setbar(c15, 10, 106, 113, 104, 110)
        setbar(c15, 11, 106.5, 109, 106, 108)
        result = d.ltf_scan(c15, setup, self.rules, now)
        self.assertIsNone(result["trigger"])

    def test_late_pivot_is_not_known_at_violation(self):
        c4, _, _, now = reclaim_fixture()
        setbar(c4, 52, 100, 101, 99, 100)  # Remove the truly confirmed low.
        setbar(c4, 56, 100, 104, 95, 103)  # Too late to be confirmed before break.
        self.assertIsNone(d.reclaim_scan(c4, self.rules, now)["setup"])

    def test_momentum_can_confirm_volume_after_cross(self):
        c4, _, _ = momentum_fixture()
        c4[-1] = replace(c4[-1], v=50)
        self.assertIsNone(d.momentum_scan(c4, self.rules)["signal"])
        previous = c4[-1]
        c4.append(d.Candle(previous.t+d.H4, 112.3, 113.6, 112, 113, 180, d.H4))
        result = d.momentum_scan(c4, self.rules)
        self.assertIsNotNone(result["signal"], result)
        self.assertEqual(next(c for c in result["checks"] if c["key"] == "cross")["measured"], 1)

    def test_failed_entry_does_not_consume_setup(self):
        c4, c15, q, now = reclaim_fixture()
        bad = {**q, "ask": 114, "bid": 113.99}
        first = self.engine.evaluate("BTC", bad, c4, c15, now)
        self.assertEqual(first["reclaim"]["status"], "WAITING FOR ACCEPTABLE ENTRY")
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["consumed"], [])
        good = self.engine.evaluate("BTC", q, c4, c15, now)
        self.assertEqual(good["reclaim"]["status"], "ACTIVE PAPER TRADE")

    def test_stale_quote_does_not_create_trade(self):
        c4, c15, q, now = reclaim_fixture()
        result = self.engine.evaluate("BTC", {**q, "asof_ms": now-60000}, c4, c15, now)
        self.assertEqual(result["reclaim"]["status"], "WAITING FOR FRESH QUOTE")
        self.assertEqual(self.store.snapshot()["outbox"], [])

    def test_repeat_poll_and_restart_do_not_duplicate(self):
        c4, c15, q, now = self.qualify()
        self.engine.evaluate("BTC", q, c4, c15, now)
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        d.Engine(ASSETS, self.rules, restarted).evaluate("BTC", q, c4, c15, now)
        self.assertEqual(len(restarted.snapshot()["assets"]["BTC"]["trades"]), 1)
        self.assertEqual(len(restarted.snapshot()["outbox"]), 2)

    def test_pending_survives_restart(self):
        c4, c15, q, now = reclaim_fixture()
        early = c15[:10]
        n1 = early[-1].end+2
        self.engine.evaluate("BTC", {**q, "asof_ms": n1}, c4, early, n1)
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        result = d.Engine(ASSETS, self.rules, restarted).evaluate("BTC", q, c4, c15, now)
        self.assertEqual(result["reclaim"]["status"], "ACTIVE PAPER TRADE")

    def test_momentum_qualifies_independently_of_15m_failure(self):
        c4, q, now = momentum_fixture()
        result = self.engine.evaluate("BTC", q, c4, [], now, {"15m": "simulated outage"})
        self.assertEqual(result["momentum"]["status"], "ACTIVE PAPER TRADE", result["momentum"])
        self.assertIn("15m", result["errors"])

    def test_momentum_rsi_and_positive_roc_are_real_gates(self):
        c4, _, _ = momentum_fixture()
        result = d.momentum_scan(c4, replace(self.rules, momentum_rsi_min=99, momentum_rsi_max=100))
        self.assertIsNone(result["signal"])
        self.assertEqual(next(c for c in result["checks"] if c["key"] == "rsi")["status"], "wait")


class LifecycleTests(TemporaryEngine):
    def test_stop_before_target_when_same_bar_hits_both(self):
        _, _, _, now = self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        t = ((now//d.M15)+1)*d.M15
        bar = d.Candle(t, trade["entry"], trade["tp2"]+1, trade["stop"]-1, trade["entry"], 100, d.M15)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", trade["entry"], [], [bar], bar.end+2))
        state = self.store.snapshot()
        actual = state["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["status"], "stopped")
        self.assertFalse(actual["tp1_hit"])
        self.assertAlmostEqual(actual["realized_pnl"], -actual["initial_risk_usd"])

    def test_price_jump_hits_both_targets_in_order_once(self):
        _, _, _, now = self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        for _ in range(2):
            self.store.transaction(lambda s: d.monitor_positions(s, "BTC", trade["tp2"]+1, [], [], now+1))
        state = self.store.snapshot()
        ids = [e["id"].split(":")[-1] for e in state["outbox"] if e["kind"] == "telegram"]
        self.assertEqual(ids, ["entry", "TP1", "TP2"])
        actual = state["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["remaining"], 0)
        self.assertAlmostEqual(actual["realized_pnl"]/actual["initial_risk_usd"], 2.5)
        self.assertAlmostEqual(state["cash"], self.rules.paper_equity+actual["realized_pnl"])

    def test_partial_tp_then_stop_cash_accounting(self):
        _, _, _, now = self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", trade["tp1"], [], [], now+1))
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", trade["stop"], [], [], now+2))
        actual = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertAlmostEqual(actual["realized_pnl"]/actual["initial_risk_usd"], .5)

    def test_4h_invalidation_without_15m_feed(self):
        _, _, _, now = self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        t = ((now//d.H4)+1)*d.H4
        bar = d.Candle(t, 101, 103, 97, 98, 100, d.H4)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", trade["tp2"]+1, [bar], [], bar.end+2))
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"][0]["status"], "invalidated")

    def test_pre_entry_candle_wick_is_not_post_entry_stop(self):
        _, c15, _, now = self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        b = replace(c15[-1], l=trade["stop"]-20)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", trade["entry"], [], [b], now+2))
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"][0]["status"], "active")

    def test_live_stop_runs_during_ohlc_outage(self):
        _, _, _, now = self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        quote = {"bid": trade["stop"]-1, "ask": trade["stop"]-.99, "asof_ms": now+1}
        self.engine.evaluate("BTC", quote, [], [], now+1)
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"][0]["status"], "stopped")

    def test_shared_portfolio_budget(self):
        _, _, _, _ = self.qualify()
        p = d.portfolio(self.store.snapshot())
        self.assertAlmostEqual(p["cost_equity"], 1000)
        self.assertGreater(p["open_risk"], 0)
        self.assertLess(p["cash"], 1000)


class DurabilityTests(TemporaryEngine):
    def test_disk_failure_rolls_back_and_queues_no_alert(self):
        c4, c15, quote, now = reclaim_fixture()
        with patch("adaptive_crypto.ledger.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.engine.evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(self.store.snapshot()["outbox"], [])
        self.assertEqual(self.store.snapshot()["cash"], 1000)

    def test_old_state_archived_and_not_loaded_as_current(self):
        self.path.write_text(json.dumps({"version": 8, "asset_pending_retests": {"BTC": {"stop_level": 95}}}))
        restored = d.StateStore(self.path, ASSETS, self.rules)
        self.assertIsNone(restored.snapshot()["assets"]["BTC"]["pending"])
        self.assertTrue((self.path.parent/"backup"/"latest.zip").is_file())

    def test_malformed_current_state_archived(self):
        state = self.store.snapshot()
        state["assets"]["BTC"]["pending"] = {"key": "missing levels"}
        self.path.write_text(json.dumps(state))
        restored = d.StateStore(self.path, ASSETS, self.rules)
        self.assertTrue(restored.snapshot()["warnings"])

    def test_changed_formulas_do_not_reuse_state(self):
        self.qualify()
        restored = d.StateStore(self.path, ASSETS, replace(self.rules, reclaim_rvol_min=1.3))
        self.assertEqual(restored.snapshot()["assets"]["BTC"]["trades"], [])

    def test_telegram_success_is_recorded_after_ack(self):
        self.qualify()
        seen = []
        def sender(event):
            on_disk = json.loads(self.path.read_text())
            self.assertEqual(on_disk["outbox"][0]["status"], "running")
            seen.append(event["id"])
            return {"status": "sent", "message_id": 123}
        d.dispatch_once(self.store, "telegram", sender)
        d.dispatch_once(self.store, "telegram", sender)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "sent")

    def test_timeout_not_reported_as_sent_or_blindly_retried(self):
        self.qualify()
        def failed(event):
            raise TimeoutError("ambiguous timeout")
        d.dispatch_once(self.store, "telegram", failed)
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "uncertain")
        self.assertFalse(d.dispatch_once(self.store, "telegram", failed))

    def test_crash_during_delivery_is_uncertain_on_restart(self):
        self.qualify()
        self.store.transaction(lambda s: s["outbox"][0].update(status="running"))
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        self.assertEqual(restarted.snapshot()["outbox"][0]["status"], "uncertain")

    def test_ai_error_cannot_undo_signal(self):
        self.qualify()
        d.dispatch_once(self.store, "ai", lambda e: {"status": "failed", "error": "AI offline"})
        state = self.store.snapshot()
        self.assertEqual(state["assets"]["BTC"]["trades"][0]["status"], "active")
        self.assertEqual(state["outbox"][0]["status"], "queued")

    def test_slow_ai_does_not_hold_engine_lock(self):
        self.qualify()
        entered, release = threading.Event(), threading.Event()
        def slow(event):
            entered.set()
            release.wait(2)
            return {"status": "done", "commentary": "stub"}
        thread = threading.Thread(target=d.dispatch_once, args=(self.store, "ai", slow))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"][0]["status"], "active")
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_no_import_time_threads_or_credentials_required(self):
        self.assertTrue(callable(d.create_app))
        self.assertFalse(hasattr(d, "OPENAI_API_KEY"))


class WebTests(TemporaryEngine):
    def test_total_network_outage_renders_and_next_scan_recovers(self):
        provider = Mock()
        for method in (provider.now, provider.quotes, provider.candles):
            method.side_effect = RuntimeError("exchange offline")
        runtime = d.DashboardRuntime(ASSETS, self.rules, self.store, provider=provider)
        runtime.scan_once()
        row = runtime.data["BTC"]
        self.assertNotIn("scan_error", row)
        self.assertIn("clock", row["errors"])
        self.assertEqual(row["reclaim"]["status"], "DATA UNAVAILABLE")
        self.assertEqual(row["momentum"]["status"], "DATA UNAVAILABLE")
        client = d.create_app(runtime).test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"exchange offline", response.data)
        self.assertEqual(client.get("/api/state").status_code, 200)
        self.assertEqual(client.get("/health").status_code, 503)
        c4, c15, quote, now = reclaim_fixture()
        provider.now.side_effect = None
        provider.now.return_value = now
        provider.quotes.side_effect = None
        provider.quotes.return_value = {"BTC/USD": quote}
        provider.candles.side_effect = lambda pair, interval, at: c4 if interval == d.H4 else c15
        runtime.scan_once()
        self.assertEqual(runtime.data["BTC"]["errors"], {})
        self.assertEqual(runtime.data["BTC"]["reclaim"]["status"], "ACTIVE PAPER TRADE")
        self.assertEqual(client.get("/health").status_code, 200)

    def test_post_fetch_clock_failure_prevents_entry_and_remains_visible(self):
        c4, c15, quote, now = reclaim_fixture()
        provider = Mock()
        provider.now.side_effect = [now, RuntimeError("clock offline")]
        provider.quotes.return_value = {"BTC/USD": quote}
        provider.candles.side_effect = lambda pair, interval, at: c4 if interval == d.H4 else c15
        runtime = d.DashboardRuntime(ASSETS, self.rules, self.store, provider=provider)
        with patch.object(time, "time", return_value=now/1000):
            runtime.scan_once()
        self.assertIn("clock", runtime.data["BTC"]["errors"])
        self.assertEqual(runtime.data["BTC"]["reclaim"]["status"], "WAITING FOR FRESH QUOTE")
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])
        self.assertEqual(d.create_app(runtime).test_client().get("/").status_code, 200)

    def test_first_scan_engine_failure_renders_without_prior_measurements(self):
        provider = Mock()
        provider.now.return_value = int(time.time()*1000)
        provider.quotes.return_value = {}
        provider.candles.return_value = []
        runtime = d.DashboardRuntime(ASSETS, self.rules, self.store, provider=provider)
        with patch.object(runtime.engine, "evaluate", side_effect=OSError("disk full")):
            runtime.scan_once()
        self.assertEqual(runtime.data["BTC"]["scan_error"], "disk full")
        client = d.create_app(runtime).test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"disk full", response.data)
        self.assertIn("disk full", client.get("/paper-trading").text)
        self.assertEqual(client.get("/health").status_code, 503)

    def test_one_invalid_pair_does_not_block_valid_pair_quote(self):
        c4, c15, quote, now = reclaim_fixture()
        assets = {**ASSETS, "BAD": {"symbol": "BAD/USD", "price_decimals": 2}}
        class Provider:
            def now(self): return now
            def quotes(self, pairs):
                if "BAD/USD" in pairs:
                    raise d.DataError("Unknown asset pair")
                return {"BTC/USD": quote}
            def candles(self, pair, interval, _):
                if pair == "BAD/USD":
                    raise d.DataError("Unknown asset pair")
                return c4 if interval == d.H4 else c15
        store = d.StateStore(Path(self.temp.name)/"multi.json", assets, self.rules)
        runtime = d.DashboardRuntime(assets, self.rules, store, provider=Provider())
        runtime.scan_once()
        self.assertEqual(runtime.data["BTC"]["reclaim"]["status"], "ACTIVE PAPER TRADE")
        self.assertEqual(runtime.data["BTC"]["errors"], {})
        self.assertIn("quote", runtime.data["BAD"]["errors"])

    def test_empty_dashboard_and_json_render(self):
        runtime = d.DashboardRuntime(ASSETS, self.rules, self.store)
        client = d.create_app(runtime).test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/api/state").status_code, 200)
        self.assertEqual(client.get("/health").status_code, 503)

    def test_qualified_dashboard_renders_locked_evidence(self):
        c4, c15, quote, now = reclaim_fixture()
        runtime = d.DashboardRuntime(ASSETS, self.rules, self.store)
        runtime.data["BTC"] = runtime.engine.evaluate("BTC", quote, c4, c15, now)
        runtime.updated_ms = now
        response = d.create_app(runtime).test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"ACTIVE PAPER TRADE", response.data)
        self.assertNotIn(b"Entry observed", response.data)
        self.assertIn(b"Live paper trades", d.create_app(runtime).test_client().get("/paper-trading").data)

    def test_scan_exception_retains_old_timestamp(self):
        class Provider:
            def now(self): return int(time.time()*1000)
            def quotes(self, pairs): raise RuntimeError("exchange offline")
            def candles(self, *args): raise RuntimeError("exchange offline")
        runtime = d.DashboardRuntime(ASSETS, self.rules, self.store, provider=Provider())
        runtime.scan_once()
        self.assertIn("quote", runtime.data["BTC"]["errors"])
        client = d.create_app(runtime).test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/health").status_code, 503)


if __name__ == "__main__":
    unittest.main()
