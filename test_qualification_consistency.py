"""Regressions for structure history and qualification evidence consistency."""
import copy
from dataclasses import replace
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import adaptive_crypto_dashboard as d
from test_dashboard import ASSETS, bars, momentum_fixture, reclaim_fixture, setbar


def short_history_fixture():
    rules = replace(d.Rules(), atr_period=2, volume_period=2,
                    mss_lookback=20, pivot_strength=1).validate()
    candles = bars(30)
    setbar(candles, 2, 100, 101, 96, 100)
    setbar(candles, 5, 100, 104, 99, 103)
    setbar(candles, 6, 103, 103.5, 97, 97.4)
    setbar(candles, 7, 97.5, 99, 94, 96.5)
    return candles, rules


def paired_retest_fixture():
    c4, _, _, now = reclaim_fixture()
    rules = d.Rules()
    setup = d.reclaim_scan(c4, rules, now)["setup"]
    candles = bars(18, d.M15, setup["reclaim_end"]+1, 109)
    setbar(candles, 0, 109, 110, 101, 109.1)
    setbar(candles, 16, 109, 112, 108, 111.5)
    setbar(candles, 17, 111.5, 120, 101, 111.5)
    return candles, setup, rules


class OfflineConsistencyTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(requests.sessions.Session, "request",
                               side_effect=AssertionError("Consistency tests must remain offline"))
        network.start()
        self.addCleanup(network.stop)


class StructureHistoryTests(OfflineConsistencyTests):
    def test_short_history_waits_with_actual_available_prior_bars(self):
        candles, rules = short_history_fixture()
        candles = candles[:8]
        result = d.reclaim_scan(candles, rules, candles[-1].end+2)
        self.assertEqual(result["status"], "WAITING FOR 4H STRUCTURE HISTORY")
        self.assertIsNone(result["setup"])
        history = next(c for c in result["checks"] if c["key"] == "history")
        self.assertEqual((history["status"], history["measured"], history["required"]), ("wait", 7, 20))
        self.assertEqual(history["candle_ms"], candles[-1].t)

    def test_early_candidates_do_not_abort_either_strategy_in_longer_history(self):
        candles, rules = short_history_fixture()
        with tempfile.TemporaryDirectory() as temp:
            store = d.StateStore(Path(temp)/"state.json", ASSETS, rules)
            result = d.Engine(ASSETS, rules, store).evaluate("BTC", None, candles, [], candles[-1].end+2)
            self.assertIn("checks", result["reclaim"])
            self.assertIn("checks", result["momentum"])
            self.assertEqual(store.snapshot()["assets"]["BTC"]["trades"], [])

    def test_reclaim_can_qualify_as_soon_as_full_prior_structure_is_available(self):
        candles, rules = short_history_fixture()
        setbar(candles, 20, 100, 120, 99, 119, 200)
        candles = candles[:21]
        result = d.reclaim_scan(candles, rules, candles[-1].end+2)
        self.assertIsNotNone(result["setup"], result)
        self.assertEqual(result["setup"]["reclaim_ms"], candles[20].t)
        self.assertEqual(result["setup"]["sweep_ms"], candles[7].t)
        self.assertTrue(d.passed(result["checks"]))


class LowerTimeframeEvidenceTests(OfflineConsistencyTests):
    def fixture(self):
        c4, candles, _, now = reclaim_fixture()
        rules = d.Rules()
        setup = d.reclaim_scan(c4, rules, now)["setup"]
        return candles, setup, rules, now

    def test_expired_original_retest_is_visible_beside_a_newer_valid_retest(self):
        candles, setup, rules = paired_retest_fixture()
        original = copy.deepcopy(setup)
        result = d.ltf_scan(candles, setup, rules, candles[-1].end+2)
        retest, trigger = result["checks"][-2:]
        self.assertEqual(retest["status"], "pass")
        self.assertEqual(retest["candle_ms"], candles[17].t)
        self.assertIn("newer retest needs its own structure break", retest["note"])
        self.assertEqual(trigger["status"], "wait")
        self.assertEqual(trigger["candle_ms"], candles[16].t)
        self.assertEqual(trigger["measured"]["retest_opened_ms"], candles[0].t)
        self.assertGreater(trigger["measured"]["retest_age_bars"], trigger["required"]["max_retest_age_bars"])
        self.assertLess(trigger["measured"]["age_bars"], trigger["required"]["max_age_bars"])
        self.assertIsNone(result["trigger"])
        self.assertEqual(setup, original)
        measured = dict(d.measurement_rows(trigger, places=2))
        required = dict(d.measurement_rows(trigger, required=True, places=2))
        self.assertEqual(measured["Paired retest opened"], d.utc(candles[0].t))
        self.assertEqual((measured["Paired retest age"], required["Paired retest age"]), ("17 bars", "≤ 16 bars"))

    def test_failed_close_location_retains_candidate_prices_and_all_requirements(self):
        candles, setup, rules, now = self.fixture()
        setbar(candles, 11, 103, 125, 102, 111.5)
        result = d.ltf_scan(candles, setup, rules, now)
        evidence = result["checks"][-1]
        self.assertEqual(evidence["status"], "wait")
        self.assertEqual(evidence["candle_ms"], candles[-1].t)
        for key, expected in {"open": 103, "high": 125, "low": 102, "close": 111.5, "close_change": 8.5}.items():
            self.assertEqual(evidence["measured"][key], expected)
        self.assertAlmostEqual(evidence["measured"]["close_location"], 9.5/23)
        self.assertEqual(evidence["required"]["close_location_min"], .6)
        self.assertEqual(evidence["required"]["close_above"], 110)
        self.assertEqual(evidence["required"]["close_change_above"], 0)
        self.assertIsNone(result["trigger"])

    def test_sixty_percent_close_location_boundary_is_inclusive(self):
        for close, expected in ((111, "pass"), (math.nextafter(111, -math.inf), "wait")):
            with self.subTest(close=close):
                candles, setup, rules, now = self.fixture()
                setbar(candles, 11, 103, 117, 102, close)
                result = d.ltf_scan(candles, setup, rules, now)
                self.assertEqual(result["checks"][-1]["status"], expected)
                self.assertEqual(result["trigger"] is not None, expected == "pass")

    def test_bullish_close_and_prior_high_break_are_strict(self):
        for open_price, close in ((111, 111), (111.5, 111), (103, 110)):
            with self.subTest(open=open_price, close=close):
                candles, setup, rules, now = self.fixture()
                setbar(candles, 11, open_price, 112, 102, close)
                result = d.ltf_scan(candles, setup, rules, now)
                evidence = result["checks"][-1]
                self.assertEqual(evidence["status"], "wait")
                self.assertEqual(evidence["measured"]["close_change"], close-open_price)
                self.assertIsNone(result["trigger"])

    def test_fresh_historical_break_keeps_its_own_prices_and_retest(self):
        candles, setup, rules, _ = self.fixture()
        actual_break = candles[-1]
        candles.append(d.Candle(candles[-1].end+1, 111.5, 200, 110.5, 111.4, 100, d.M15))
        result = d.ltf_scan(candles, setup, rules, candles[-1].end+2)
        evidence = result["checks"][-1]
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["candle_ms"], actual_break.t)
        self.assertEqual(evidence["measured"]["high"], actual_break.h)
        self.assertEqual(evidence["measured"]["retest_opened_ms"], actual_break.t)
        self.assertTrue(d.passed(result["checks"]))

    def test_a_failed_break_candle_explains_why_it_cannot_confirm_a_replacement(self):
        candles, setup, rules, _ = self.fixture()
        candles.append(d.Candle(candles[-1].end+1, 111.5, 113, 101, 110, 100, d.M15))
        result = d.ltf_scan(candles, setup, rules, candles[-1].end+2)
        evidence = result["checks"][-1]
        self.assertEqual(evidence["status"], "wait")
        self.assertFalse(evidence["measured"]["replacement_confirmation_allowed"])
        self.assertTrue(evidence["required"]["replacement_confirmation_allowed"])
        self.assertIsNone(result["trigger"])

    def test_live_reset_does_not_display_old_candles_as_new_candidates(self):
        candles, setup, rules, now = self.fixture()
        setup["confirmation_reset_ms"] = now
        result = d.ltf_scan(candles, setup, rules, now)
        evidence = result["checks"][-1]
        self.assertIsNone(evidence["candle_ms"])
        self.assertIsNone(evidence["measured"])
        self.assertEqual(evidence["status"], "wait")
        self.assertIn("confirmation reset", evidence["note"])

    def test_missing_retest_is_not_fabricated_for_a_bullish_candidate(self):
        candles, setup, rules, now = self.fixture()
        for i in (9, 10):
            setbar(candles, i, 106, 109, 105, 107)
        setbar(candles, 11, 107, 113, 106, 112)
        result = d.ltf_scan(candles, setup, rules, now)
        evidence = result["checks"][-1]
        self.assertIsNone(evidence["measured"]["retest_opened_ms"])
        self.assertIsNone(evidence["measured"]["retest_age_bars"])
        self.assertEqual(evidence["status"], "wait")
        self.assertIsNone(result["trigger"])


class MomentumEntryAgeTests(OfflineConsistencyTests):
    def evaluate(self, age_ms, rules=None):
        rules = rules or d.Rules()
        candles, quote, _ = momentum_fixture()
        now = candles[-1].end+age_ms
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/"state.json"
        store = d.StateStore(path, ASSETS, rules)
        engine = d.Engine(ASSETS, rules, store)
        result = engine.evaluate("BTC", {**quote, "asof_ms": now}, candles, [], now)
        return result, store, path, engine, candles, quote, now

    def test_expired_signal_has_a_failed_entry_age_measurement(self):
        result, store, *_ = self.evaluate(31*60000)
        momentum = result["momentum"]
        evidence = next(c for c in momentum["checks"] if c["key"] == "entry_age")
        self.assertEqual(momentum["status"], "SIGNAL TOO OLD FOR A NEW ENTRY")
        self.assertEqual(evidence["status"], "wait")
        self.assertEqual(evidence["measured"], {"age_minutes": 31})
        self.assertEqual(evidence["required"], {"max_age_minutes": 30})
        self.assertEqual(d.measurement_rows(evidence, True), [("Age since confirmation close", "≤ 30 minutes")])
        self.assertFalse(d.passed(momentum["checks"]))
        self.assertEqual(store.snapshot()["assets"]["BTC"]["trades"], [])
        self.assertEqual(store.snapshot()["outbox"], [])

    def test_entry_age_boundary_is_inclusive_and_raw_milliseconds_drive_status(self):
        for age, qualifies in ((30*60000, True), (30*60000+1, False)):
            with self.subTest(age_ms=age):
                result, store, *_ = self.evaluate(age)
                evidence = next(c for c in result["momentum"]["checks"] if c["key"] == "entry_age")
                self.assertEqual(evidence["status"], "pass" if qualifies else "wait")
                self.assertEqual(bool(store.snapshot()["assets"]["BTC"]["trades"]), qualifies)

    def test_displayed_age_limit_follows_configured_freshness(self):
        result, *_ = self.evaluate(16*60000, replace(d.Rules(), trigger_fresh_bars=1))
        evidence = next(c for c in result["momentum"]["checks"] if c["key"] == "entry_age")
        self.assertEqual(evidence["required"]["max_age_minutes"], 15)
        self.assertEqual(evidence["status"], "wait")

    def test_passed_entry_evidence_survives_restart_and_does_not_expire_an_active_trade(self):
        result, store, path, _, candles, quote, now = self.evaluate(10*60000)
        original = copy.deepcopy(store.snapshot()["assets"]["BTC"]["trades"][0]["evidence"])
        restarted = d.StateStore(path, ASSETS, d.Rules())
        now += 25*60000
        result = d.Engine(ASSETS, d.Rules(), restarted).evaluate(
            "BTC", {**quote, "asof_ms": now}, candles, [], now)
        self.assertEqual(result["momentum"]["status"], "ACTIVE PAPER TRADE")
        self.assertEqual(result["momentum"]["checks"], original)
        self.assertEqual(len(restarted.snapshot()["assets"]["BTC"]["trades"]), 1)


if __name__ == "__main__":
    unittest.main()
