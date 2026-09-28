"""Offline NN backtests distinguish confirmed intrabar stops from timed fills."""
import unittest

from adaptive_crypto.core import Candle, H4
from adaptive_crypto.neural_tools import simulate


def candle(index, opening=100, low=99, close=100):
    return Candle(index*H4, opening, max(opening, close)+1, low, close, 1, H4)


class NeuralBacktestChronologyTests(unittest.TestCase):
    def test_intrabar_stop_uses_confirmation_time_without_claiming_exact_fill_time(self):
        candles = [candle(0), candle(1, low=85, close=95), candle(2)]
        result = simulate(candles, ["BUY", "HOLD", "HOLD"], fee=.001, slippage=.0005)
        trade, = result["trades"]
        self.assertEqual(trade["reason"], "stop")
        self.assertEqual(trade["opened_ms"], candles[1].t)
        self.assertEqual(trade["closed_ms"], candles[1].end)
        self.assertFalse(trade["exit_time_exact"])
        self.assertEqual(trade["exit_time_basis"], "candle_close_confirmation")
        expected = 1000/(100*1.0005*1.001)*(100*1.0005*.9)*.9995*.999
        self.assertAlmostEqual(result["final"], expected)
        self.assertAlmostEqual(trade["pnl"], expected-1000)

    def test_later_intrabar_stop_is_not_reported_at_that_candles_open(self):
        candles = [candle(0), candle(1), candle(2, low=85, close=95), candle(3)]
        result = simulate(candles, ["BUY", "HOLD", "HOLD", "HOLD"], fee=0, slippage=0)
        trade, = result["trades"]
        self.assertEqual(trade["closed_ms"], candles[2].end)
        self.assertFalse(trade["exit_time_exact"])
        self.assertEqual(trade["exit"], 90)

    def test_gap_stop_keeps_open_time_and_precedes_sell_or_reentry(self):
        candles = [candle(0), candle(1), candle(2, opening=80, low=79, close=80)]
        for next_signal in ("BUY", "SELL"):
            with self.subTest(signal=next_signal):
                result = simulate(candles, ["BUY", next_signal, "HOLD"], fee=0, slippage=0)
                trade, = result["trades"]
                self.assertEqual(trade["reason"], "gap_stop")
                self.assertEqual(trade["closed_ms"], candles[2].t)
                self.assertTrue(trade["exit_time_exact"])
                self.assertEqual(trade["exit_time_basis"], "candle_open")
                self.assertEqual(result["final"], 800)

    def test_signal_sell_and_terminal_liquidation_keep_their_execution_times(self):
        candles = [candle(0), candle(1), candle(2, opening=110, low=109, close=111)]
        for predictions, reason, closed_ms, basis, final in (
            (["BUY", "SELL", "HOLD"], "sell", candles[2].t, "candle_open", 1100),
            (["BUY", "HOLD", "HOLD"], "end", candles[2].end, "final_candle_close", 1110),
        ):
            with self.subTest(reason=reason):
                result = simulate(candles, predictions, fee=0, slippage=0)
                trade, = result["trades"]
                self.assertEqual(trade["reason"], reason)
                self.assertEqual(trade["closed_ms"], closed_ms)
                self.assertTrue(trade["exit_time_exact"])
                self.assertEqual(trade["exit_time_basis"], basis)
                self.assertEqual(result["final"], final)


if __name__ == "__main__":
    unittest.main()
