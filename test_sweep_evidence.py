"""Regression coverage for sweep depth, price thresholds and candle chronology."""
from dataclasses import replace
import math
import unittest

import adaptive_crypto_dashboard as d
from test_dashboard import reclaim_fixture, setbar


class SweepEvidenceTests(unittest.TestCase):
    def setUp(self):
        # A short ATR period gives exactly representable boundary prices.
        self.rules = replace(d.Rules(), atr_period=2, sweep_atr=0.5).validate()
        self.candles = reclaim_fixture()[0][:59]
        self.liquidity = 96.0
        self.previous_atr = 5.046875
        self.threshold = 93.4765625

    def scan(self):
        result = d.reclaim_scan(self.candles, self.rules, self.candles[-1].end+2)
        evidence = next(c for c in result["checks"] if c["key"] == "sweep")
        return result, evidence

    def test_waiting_shows_actual_latest_candle_and_matching_price_requirements(self):
        setbar(self.candles, 58, 97.5, 100, 97, 99)
        result, evidence = self.scan()
        self.assertEqual(result["status"], "WAITING FOR LIQUIDITY SWEEP")
        self.assertEqual(evidence["status"], "wait")
        self.assertEqual(evidence["candle_ms"], self.candles[-1].t)
        self.assertEqual(evidence["measured"]["low"], 97)
        self.assertEqual(evidence["measured"]["close"], 99)
        self.assertEqual(evidence["measured"]["liquidity_low"], self.liquidity)
        self.assertEqual(evidence["measured"]["previous_atr"], self.previous_atr)
        self.assertAlmostEqual(evidence["measured"]["sweep_depth_atr"], -1/self.previous_atr)
        self.assertEqual(evidence["required"], {
            "low_below": self.threshold, "close_above": self.liquidity,
            "sweep_depth_atr_above": 0.5})
        self.assertIn("Latest completed 4H candle", evidence["note"])
        self.assertIn("Negative depth", evidence["note"])

    def test_depth_is_strict_and_uses_atr_before_the_sweep(self):
        for low, expected in ((self.threshold, "wait"),
                              (math.nextafter(self.threshold, -math.inf), "pass"),
                              (math.nextafter(self.threshold, math.inf), "wait")):
            with self.subTest(low=low):
                setbar(self.candles, 58, 97.5, 99, low, 96.5)
                _, evidence = self.scan()
                self.assertEqual(evidence["status"], expected)
                self.assertEqual(evidence["measured"]["previous_atr"], self.previous_atr)
                self.assertEqual(evidence["required"]["low_below"], self.threshold)
        # The current candle's range must not inflate its own ATR baseline.
        setbar(self.candles, 58, 97.5, 1000, 93, 96.5)
        _, evidence = self.scan()
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["required"]["low_below"], self.threshold)

    def test_deep_wick_requires_same_candle_close_strictly_above_liquidity(self):
        for close, expected in ((95.5, "wait"), (96, "wait"), (96.5, "pass")):
            with self.subTest(close=close):
                setbar(self.candles, 58, 97.5, 99, 93, close)
                _, evidence = self.scan()
                self.assertEqual(evidence["status"], expected)
        # A later recovery cannot be combined with the previous candle's wick.
        setbar(self.candles, 58, 97.5, 99, 93, 95.5)
        self.candles.append(d.Candle(self.candles[-1].end+1, 97, 100, 96, 99, 100, d.H4))
        result, evidence = self.scan()
        self.assertEqual(result["status"], "WAITING FOR LIQUIDITY SWEEP")
        self.assertEqual(evidence["status"], "wait")
        self.assertEqual(evidence["measured"]["sweep_depth_atr"], 0)
        self.assertEqual(evidence["measured"]["low"], 96)

    def test_qualified_reclaim_retains_actual_sweep_candle_measurements(self):
        candles, _, _, now = reclaim_fixture()
        result = d.reclaim_scan(candles, replace(d.Rules(), sweep_atr=0.5), now)
        self.assertIsNotNone(result["setup"])
        evidence = next(c for c in result["checks"] if c["key"] == "sweep")
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["candle_ms"], candles[58].t)
        self.assertEqual(evidence["measured"]["low"], 94)
        self.assertEqual(evidence["measured"]["close"], 96.5)
        self.assertNotIn("Latest completed", evidence["note"])
        self.assertEqual(result["setup"]["sweep_ms"], evidence["candle_ms"])


if __name__ == "__main__":
    unittest.main()
