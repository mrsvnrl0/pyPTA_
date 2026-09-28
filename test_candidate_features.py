"""Causality and live/batch equivalence for the candidate OHLCV schema."""
import unittest

import numpy as np

from adaptive_crypto.candidate_features import HISTORY_BARS, feature_rows, feature_sequence
from adaptive_crypto.core import Candle, DataError, H4


def history(count=400):
    candles = []
    price = 80.0
    for index in range(count):
        # Alternating moves and volume changes exercise Wilder RSI and every
        # price/volume group without requiring an external training dataset.
        close = price * (1.0 + ((index % 7) - 3) * .001)
        candles.append(Candle(index * H4, price, max(price, close) * 1.002,
                              min(price, close) * .998, close,
                              100.0 + (index % 11) * 3.0, H4))
        price = close
    return candles


class CandidateFeatureTests(unittest.TestCase):
    def test_batch_prefix_matches_live_rolling_window_even_after_future_changes(self):
        candles = history()
        decision = 350
        live = feature_sequence(candles[:decision])
        batch, ends = feature_rows(candles)
        np.testing.assert_allclose(batch[decision - 256 - 64 + 1:decision - 256 + 1].astype(np.float32),
                                   live, rtol=0, atol=1e-7)
        self.assertEqual(ends[decision - 256], candles[decision - 1].end)

        changed_future = candles[:decision] + [Candle(bar.t, bar.o * 1.2, bar.h * 1.3,
                                                      bar.l * .8, bar.c * 1.1, bar.v * 10, H4)
                                               for bar in candles[decision:]]
        altered_batch, _ = feature_rows(changed_future)
        np.testing.assert_array_equal(batch[:decision - 255], altered_batch[:decision - 255])

    def test_full_archive_and_minimal_live_context_agree(self):
        candles = history()
        np.testing.assert_array_equal(feature_sequence(candles), feature_sequence(candles[-HISTORY_BARS:]))
        self.assertTrue(np.isfinite(feature_sequence(candles)).all())

    def test_insufficient_or_gapped_history_is_unavailable(self):
        candles = history(HISTORY_BARS)
        with self.assertRaisesRegex(DataError, "319 contiguous"):
            feature_sequence(candles[:-1])
        candles[190] = Candle(candles[190].t + H4, candles[190].o, candles[190].h,
                              candles[190].l, candles[190].c, candles[190].v, H4)
        with self.assertRaisesRegex(DataError, "gap, duplicate"):
            feature_sequence(candles)


if __name__ == "__main__":
    unittest.main()
