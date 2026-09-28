"""Training-data chronology and source-label parity regression checks."""
import tempfile
import unittest

import numpy as np

from adaptive_crypto.core import Candle, H4
from adaptive_crypto.neural import labels
from tools.candidate_train_pipeline import (
    Samples, asset_samples, fold_plan, frozen_scaler, source_labels,
    split_samples, write_combined_candles,
)


class CandidateTrainingTests(unittest.TestCase):
    def test_source_labeler_matches_repository_adjusted_ema_and_unknown_tail(self):
        rng = np.random.default_rng(7531)
        closes = 100 * np.exp(np.cumsum(rng.normal(0, .027, 2000)))
        actual = source_labels(closes)
        reference = labels(closes, backward=5, forward=2,
                           alpha=.038, beta=.24, convention="source").to_numpy()
        expected = np.where(np.isnan(reference), -1, reference + 1).astype(np.int8)
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(actual[-2:], [-1, -1])

    def test_a_gap_cannot_become_a_training_sequence_or_forward_target(self):
        def segment(start, count):
            result = []
            close = 100.
            for i in range(count):
                nxt = close * (1.01 if i % 2 else .99)
                result.append(Candle((start + i) * H4, close, max(close, nxt) * 1.001,
                                     min(close, nxt) * .999, nxt, 100. + i, H4))
                close = nxt
            return result

        first, second = segment(0, 400), segment(402, 400)
        samples = asset_samples([first, second])
        self.assertEqual(len(samples.y), 2 * (400 - 319 - 2 + 1))
        self.assertTrue(np.all(samples.label_target_open_ms - (samples.signal_ends - H4 + 1)
                               == 2 * H4))
        self.assertFalse(np.any((samples.signal_ends - H4 + 1 >= 400 * H4)
                                & (samples.signal_ends - H4 + 1 < 402 * H4)))

    def test_split_purges_targets_at_both_boundaries(self):
        stamps = np.arange(900, dtype=np.int64) * H4 + H4 - 1
        samples = Samples(np.zeros((900, 64, 16), dtype=np.float32),
                          np.ones(900, dtype=np.int8), stamps,
                          np.arange(2, 902, dtype=np.int64) * H4)
        spec = {"aligned_history_start_ms": 100 * H4,
                "validation_start_ms": 400 * H4,
                "test_start_ms": 600 * H4,
                "test_end_ms": 900 * H4}
        masks = split_samples(samples, spec)
        self.assertEqual(np.flatnonzero(masks["train"])[0], 100)
        self.assertEqual(np.flatnonzero(masks["train"])[-1], 397)
        self.assertEqual(np.flatnonzero(masks["validation"])[0], 400)
        self.assertEqual(np.flatnonzero(masks["validation"])[-1], 597)
        self.assertEqual(np.flatnonzero(masks["test"])[0], 600)
        self.assertEqual(np.flatnonzero(masks["test"])[-1], 897)

    def test_scaler_uses_only_given_training_values(self):
        train = np.arange(2 * 64 * 16, dtype=np.float32).reshape(2, 64, 16)
        mean, scale = frozen_scaler(train)
        shifted = train + 10_000
        np.testing.assert_allclose(frozen_scaler(train)[0], mean)
        self.assertTrue(np.all(frozen_scaler(shifted)[0] > mean))
        self.assertTrue(np.isfinite(scale).all())
        self.assertTrue(np.all(scale > 0))

    def test_expanding_folds_reserve_disjoint_early_stopping_and_outer_validation(self):
        spec = {"aligned_history_start_ms": 0,
                "test_end_ms": 1000 * H4,
                "experiment_id": "chronology-fixture"}
        plan = fold_plan(spec)
        self.assertEqual(plan["schema"], 2)
        for fold in plan["folds"]:
            self.assertLess(fold["train_start_ms"], fold["fit_target_before_ms"])
            self.assertEqual(fold["fit_target_before_ms"],
                             fold["early_stopping_start_ms"])
            self.assertEqual(fold["early_stopping_end_ms"],
                             fold["validation_start_ms"])
            self.assertLess(fold["validation_start_ms"],
                            fold["validation_end_ms"])

    def test_resume_rejects_tampered_derived_evaluation_candles(self):
        candles = [Candle(i * H4, 100., 101., 99., 100.5, 12., H4)
                   for i in range(3)]
        segments = {"BTC": [candles]}
        spec = {"asset_order": ["BTC"]}
        with tempfile.TemporaryDirectory() as directory:
            target = write_combined_candles(segments, spec, directory)
            self.assertEqual(write_combined_candles(segments, spec, directory), target)
            target.write_bytes(target.read_bytes() + b"BTC,invalid\r\n")
            with self.assertRaisesRegex(ValueError, "differ from verified source"):
                write_combined_candles(segments, spec, directory)


if __name__ == "__main__":
    unittest.main()
