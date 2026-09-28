"""Offline simple-baseline regression checks on causal chronological inputs."""
from __future__ import annotations

import unittest

import numpy as np

from adaptive_crypto.core import Candle, H4
from tools.train_nn_simple_baselines import _asset_samples, _fit_logistic, _probabilities


class SimpleBaselineTests(unittest.TestCase):
    def test_logistic_fit_uses_labels_and_is_reproducible(self):
        x = np.zeros((90, 16), dtype=np.float64)
        x[:30, 0], x[30:60, 0], x[60:, 0] = -2, 0, 2
        labels = np.repeat(np.arange(3), 30)
        first = _fit_logistic((x, labels), (x, labels))
        second = _fit_logistic((x, labels), (x, labels))
        self.assertGreater(np.linalg.norm(first[0]), 0)
        np.testing.assert_array_equal(first[0], second[0])
        self.assertGreater((_probabilities(x, first[0], first[1]).argmax(axis=1) == labels).mean(), .9)

    def test_split_purges_targets_across_boundaries_and_future_features(self):
        candles = [Candle(i * H4, 100 + i * .01, 101 + i * .01, 99 + i * .01,
                          100 + i * .01, 10 + i, H4) for i in range(440)]
        spec = {"aligned_history_start_ms": 0, "validation_start_ms": 350 * H4,
                "test_start_ms": 390 * H4, "test_end_ms": 440 * H4}
        before = _asset_samples([candles], spec)
        self.assertEqual(int(before["train"]["ends"][-1]), 347 * H4 + H4 - 1)
        self.assertEqual(int(before["validation"]["ends"][-1]), 387 * H4 + H4 - 1)
        self.assertEqual(int(before["test"]["ends"][-1]), 437 * H4 + H4 - 1)
        altered = list(candles)
        for i in range(420, len(altered)):
            altered[i] = Candle(i * H4, 200 + i * .01, 201 + i * .01,
                                199 + i * .01, 200 + i * .01, 20 + i, H4)
        after = _asset_samples([altered], spec)
        np.testing.assert_array_equal(before["train"]["x"], after["train"]["x"])
        np.testing.assert_array_equal(before["validation"]["x"], after["validation"]["x"])
        np.testing.assert_array_equal(before["test"]["x"][:30], after["test"]["x"][:30])


if __name__ == "__main__":
    unittest.main()
