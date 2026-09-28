"""Deterministic offline replay tests; synthetic prices prove accounting, not edge."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from tools.evaluate_nn_candidates import (Bar, EvaluationError, H4, M5,
                                          classification_metrics, evaluate, main,
                                          read_bars, read_experiment,
                                          read_predictions, replay)
from tools.compare_nn_candidate_reports import _paired_bootstrap, compare
from tools.build_nn_candidate_ensemble import FAMILIES, combine
from tools.predict_parente_candidate_baseline import freeze_predictions
from tools.derive_nn_comparison_experiment import derive
from tools.package_nn_candidate_ensemble import _compact_evaluation


START = 10 * H4
END = START + 5 * H4


def experiment(*, order=None, **changes):
    spec = {"test_start_ms": START, "test_end_ms": END,
            "development_cutoff_ms": START - H4, "asset_order": order or ["BTC"],
            "initial_equity": 1000., "fee_rate": .004, "slippage_rate": .0005,
            "spread_bps": 10., "nn_stop_loss": .10, "risk_per_trade": .05,
            "max_total_risk": .10, "max_allocation": .75, "paper_floor": 0.,
            "minimum_notional": 10., "max_spread_bps": 30.,
            "signal_window_seconds": 60, "forward_horizon_bars": 2,
            "stress_fee_multiplier": 2., "stress_slippage_multiplier": 2.,
            "stress_spread_multiplier": 2.}
    return {**spec, **changes}


def bars(*, assets=("BTC",), prices=(100., 100., 100., 100., 100.), lows=None, interval=H4):
    values = {}
    for asset in assets:
        asset_bars = {}
        for i, price in enumerate(prices):
            low = min(price, lows[i]) if lows else price
            t = START + i * interval
            asset_bars[t] = Bar(asset, t, price, price, low, price, 1., interval)
        values[asset] = asset_bars
    return values


def predictions(*, assets=("BTC",), labels=(0, 1, 2), truth=(0, 1, 2)):
    result = {}
    for asset in assets:
        for i, label in enumerate(labels):
            probabilities = [.05, .05, .05]
            probabilities[label] = .90
            end = START + (i + 1) * H4 - 1
            result[(asset, end)] = {"asset": asset, "signal_end_ms": end,
                                    "probabilities": tuple(probabilities),
                                    "label_index": label, "truth_label_index": truth[i]}
    return result


class EvaluationTests(unittest.TestCase):
    def test_next_open_fill_and_recorded_costs(self):
        history = bars(prices=(100., 110., 120., 130., 130.))
        result = replay(history, predictions(), experiment(), limitations=False)
        trade = result["trades"][0]
        self.assertEqual(trade["opened_ms"], START + H4)
        self.assertEqual(trade["closed_ms"], START + 3 * H4)
        self.assertEqual(trade["reason"], "sell")
        self.assertGreater(trade["entry"], 110.)
        self.assertLess(trade["exit"], 130.)
        self.assertAlmostEqual(result["fees_paid_usd"], trade["entry_fee"] + trade["exit_fee"])
        self.assertAlmostEqual(result["final_net_liquidation_equity"], 1000. + trade["realized_pnl"])
        self.assertGreater(result["slippage_cost_usd"], 0.)
        self.assertGreater(result["synthetic_spread_cost_usd"], 0.)

    def test_open_loss_marks_equity_before_any_sell(self):
        history = bars(prices=(100., 100., 80., 70., 60.))
        rows = predictions(labels=(0, 1, 1))
        result = replay(history, rows, experiment(), limitations=False)
        self.assertEqual(result["completed_natural_trades"], 0)
        self.assertEqual(result["terminal_liquidations"], 1)
        self.assertLess(result["paired_equity_4h"][2]["equity"], 810.)
        self.assertGreater(result["max_sampled_drawdown"], .2)
        self.assertAlmostEqual(result["paired_equity_4h"][-1]["equity"], result["final_net_liquidation_equity"])
        self.assertLess(result["net_return"], -.4)

    def test_entry_open_then_same_bar_low_stops_at_next_observation(self):
        history = bars(lows=(100., 80., 80., 100., 100.))
        rows = predictions(labels=(0, 1, 2))
        result = replay(history, rows, experiment(), limitations=True)
        self.assertEqual(result["completed_natural_trades"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["reason"], "stop")
        self.assertEqual(trade["closed_ms"], START + 2 * H4)
        self.assertFalse(trade["exit_time_exact"])
        self.assertEqual(result["stop_range_confirmations_with_unknown_intrabar_time"], 1)

    def test_synthetic_bid_can_cross_stop_when_midpoint_low_does_not(self):
        history = bars(lows=(100., 90.12, 100., 100., 100.))
        rows = predictions(labels=(0, 1, 1))
        result = replay(history, rows, experiment(), limitations=True)
        trade = result["trades"][0]
        self.assertEqual(trade["reason"], "stop")
        self.assertEqual(trade["closed_ms"], START + 2 * H4)
        self.assertGreater(result["synthetic_spread_cost_usd"], 0)
        self.assertLess(trade["exit"], trade["stop"])

    def test_final_entry_bar_stop_precedes_terminal_liquidation(self):
        history = bars(lows=(100., 100., 100., 100., 80.))
        rows = predictions(labels=(1, 1, 1, 0), truth=(1, 1, 1, 0))
        result = replay(history, rows, experiment(), limitations=True)
        self.assertEqual(result["completed_natural_trades"], 1)
        self.assertEqual(result["terminal_liquidations"], 0)
        self.assertEqual(result["trades"][0]["reason"], "stop")
        self.assertEqual(result["trades"][0]["closed_ms"], END)

    def test_mode_change_can_add_second_lot_and_sell_closes_both(self):
        history = bars()
        schedule = [{"from_ms": START, "enabled": True},
                    {"from_ms": START + 2 * H4, "enabled": False}]
        result = replay(history, predictions(labels=(0, 0, 2)), experiment(),
                        limitations=True, mode_schedule=schedule)
        self.assertEqual(result["completed_natural_trades"], 2)
        self.assertEqual([t["limitations_enabled"] for t in result["trades"]], [True, False])
        self.assertIsNotNone(result["trades"][0]["stop"])
        self.assertIsNone(result["trades"][1]["stop"])
        self.assertTrue(all(t["reason"] == "sell" for t in result["trades"]))
        self.assertEqual(result["terminal_liquidations"], 0)
        self.assertAlmostEqual(result["final_net_liquidation_equity"],
                               1000. + sum(t["realized_pnl"] for t in result["trades"]))

    def test_off_all_cash_first_asset_order_and_on_preserves_shared_budget(self):
        history = bars(assets=("BTC", "ETH"))
        rows = predictions(assets=("BTC", "ETH"), labels=(0, 1, 1))
        spec = experiment(order=["BTC", "ETH"])
        off = replay(history, rows, spec, limitations=False)
        self.assertEqual([t["asset"] for t in off["trades"]], ["BTC"])
        self.assertGreater(off["attempts"]["insufficient_budget"], 0)
        reversed_off = replay(history, rows, {**spec, "asset_order": ["ETH", "BTC"]}, limitations=False)
        self.assertEqual([t["asset"] for t in reversed_off["trades"]], ["ETH"])
        on = replay(history, rows, spec, limitations=True)
        self.assertEqual({t["asset"] for t in on["trades"]}, {"BTC", "ETH"})
        self.assertEqual(on["terminal_liquidations"], 2)

    def test_stressed_costs_are_worse_on_flat_prices(self):
        history = bars()
        rows = predictions(labels=(0, 1, 2))
        spec = experiment()
        base = replay(history, rows, spec, limitations=False)
        stress = replay(history, rows, spec, limitations=False, stress=True)
        self.assertLess(stress["net_return"], base["net_return"])
        self.assertGreater(stress["fees_paid_usd"], base["fees_paid_usd"])

    def test_classification_metrics_count_confusion_and_calibration(self):
        rows = list(predictions().values())
        result = classification_metrics(rows)
        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["class_counts"], {"BUY": 1, "HOLD": 1, "SELL": 1})
        self.assertEqual(result["accuracy"], 1.)
        self.assertEqual(result["balanced_accuracy_present_classes"], 1.)
        self.assertEqual(result["macro_f1_all_three_classes"], 1.)
        self.assertGreater(result["log_loss"], 0.)
        self.assertGreater(result["multiclass_brier_divided_by_three"], 0.)

    def test_held_out_gaps_and_missing_predictions_are_unavailable(self):
        history = bars()
        rows = predictions()
        del history["BTC"][START + H4]
        result = evaluate(rows, history, experiment())
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["coverage_4h"]["missing_bar_counts"]["BTC"], 1)
        history = bars()
        del rows[("BTC", START + 2 * H4 - 1)]
        result = evaluate(rows, history, experiment())
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["prediction_coverage"]["missing"], 1)

    def test_source_label_audit_recomputes_future_targets_independently(self):
        history = bars()
        rows = predictions(truth=(1, 1, 1))  # Flat future prices imply HOLD.
        spec = experiment(label_version="parente-source-5-2-v1")
        verified = evaluate(rows, history, spec, verify_labels=True)
        self.assertEqual(verified["label_verification"]["status"], "complete")
        self.assertEqual(verified["label_verification"]["checked"], 3)
        rows[("BTC", START + H4 - 1)]["truth_label_index"] = 0
        with self.assertRaisesRegex(EvaluationError, "Source-label mismatch"):
            evaluate(rows, history, spec, verify_labels=True)

    def test_5m_marks_only_on_complete_common_grid(self):
        history = bars()
        low = bars(prices=(100.,) * (5 * H4 // M5), interval=M5)
        report = evaluate(predictions(), history, experiment(), bars_5m=low)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["mark_cadence_ms"], M5)
        self.assertGreater(report["replay"]["unlimited_base"]["mark_count"], 5)
        del low["BTC"][START + M5]
        report = evaluate(predictions(), history, experiment(), bars_5m=low)
        self.assertEqual(report["mark_cadence_ms"], H4)
        self.assertIn("Incomplete 5M", report["five_minute_status"])

    def test_overlapping_development_or_unknown_prediction_is_rejected(self):
        history = bars()
        rows = predictions()
        with self.assertRaisesRegex(EvaluationError, "overlap"):
            evaluate(rows, history, experiment(development_cutoff_ms=START + H4 - 1))
        rows[("BTC", END - 1)] = rows.pop(("BTC", START + H4 - 1))
        with self.assertRaisesRegex(EvaluationError, "outside"):
            evaluate(rows, history, experiment())

    def test_scoped_comparison_ignores_excluded_asset_predictions_explicitly(self):
        history = bars(assets=("BTC", "SUI"))
        rows = predictions(assets=("BTC", "SUI"))
        report = evaluate(rows, history, experiment(order=["BTC"]))
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["excluded_prediction_assets_outside_declared_scope"], ["SUI"])
        self.assertEqual(report["classification"]["combined"]["samples"], 3)

    def test_scoped_comparison_excludes_pre_warmup_rows_only_when_requested(self):
        history = bars()
        rows = predictions()
        extra_end = START - 1
        rows[("BTC", extra_end)] = {"asset": "BTC", "signal_end_ms": extra_end,
                                    "probabilities": (.05, .9, .05), "label_index": 1,
                                    "truth_label_index": 1}
        with self.assertRaisesRegex(EvaluationError, "outside"):
            evaluate(rows, history, experiment())
        scoped = evaluate(rows, history, experiment(), scope_predictions=True)
        self.assertEqual(scoped["status"], "complete")
        self.assertEqual(scoped["excluded_prediction_rows_outside_declared_time_scope"]["count"], 1)
        self.assertEqual(scoped["classification"]["combined"]["samples"], 3)

    def test_comparator_refuses_unpaired_or_low_trade_evidence(self):
        base_spec = experiment(primary_limitations_enabled=False, label_version="parente-source-5-2")
        history = bars()
        rows = predictions()
        benchmark = evaluate(rows, history, base_spec, identity=("parente", "hash-parente"),
                             hashes={"candles_4h": "same-hash"})
        candidate = evaluate(rows, history, base_spec, identity=("lstm", "hash-lstm"),
                             hashes={"candles_4h": "same-hash"})
        verdict = compare(benchmark, [candidate])
        self.assertEqual(verdict["recommendation"], "no demonstrated winner")
        self.assertIn("Fewer than 30 natural held-out exits", verdict["candidates"]["lstm"]["reasons"])
        mismatched = evaluate(rows, history, {**base_spec, "fee_rate": .01},
                              identity=("gru", "hash-gru"), hashes={"candles_4h": "same-hash"})
        verdict = compare(benchmark, [mismatched])
        self.assertIn("scope differs", " ".join(verdict["candidates"]["gru"]["reasons"]))

    def test_paired_bootstrap_checks_common_cadence_and_is_deterministic(self):
        curve = {"paired_equity_4h": [{"time_ms": START + i * H4, "equity": 1000. + i}
                                     for i in range(8)]}
        one = _paired_bootstrap(curve, curve, repeats=50)
        two = _paired_bootstrap(curve, curve, repeats=50)
        self.assertEqual(one, two)
        self.assertEqual(one["confidence_95_log_return_difference"], [0., 0.])
        other = {"paired_equity_4h": [{"time_ms": START + i * H4 + 1, "equity": 1000. + i}
                                      for i in range(8)]}
        with self.assertRaisesRegex(EvaluationError, "timestamps differ"):
            _paired_bootstrap(curve, other, repeats=50)


class InputAndCliTests(unittest.TestCase):
    def test_parente_baseline_uses_720_completed_history_and_common_source_labels(self):
        class FakeParente:
            volume_stats = {"BTC": [1., 1.]}
            identity = "a" * 64
            trained_through_ms = 0

            def predict(self, candles, asset):
                self_history_lengths.append(len(candles))
                return {"signal_end": candles[-1].end, "model_id": self.identity,
                        "probabilities": {"BUY": .2, "HOLD": .7, "SELL": .1}}

        self_history_lengths = []
        history = {"BTC": {i * H4: Bar("BTC", i * H4, 100. + i * .01,
                                      100. + i * .01, 100. + i * .01,
                                      100. + i * .01, 1., H4) for i in range(725)}}
        spec = {**experiment(label_version="parente-source-5-2-v1"),
                "test_start_ms": 720 * H4, "test_end_ms": 725 * H4,
                "development_cutoff_ms": 719 * H4}
        rows = list(freeze_predictions(history, spec, FakeParente()))
        self.assertEqual(len(rows), 3)
        self.assertEqual(self_history_lengths, [720, 720, 720])
        pred = {(row["asset"], row["signal_end_ms"]):
                {**row, "label_index": 1, "probabilities": tuple(row["probabilities"])} for row in rows}
        report = evaluate(pred, history, spec, identity=("parente_mlp_v1", "a" * 64),
                          verify_labels=True)
        self.assertEqual(report["label_verification"]["checked"], 3)
        with self.assertRaisesRegex(EvaluationError, "no bundled frozen calibration"):
            list(freeze_predictions({"SUI": history["BTC"]},
                                    {**spec, "asset_order": ["SUI"]}, FakeParente()))

    def test_common_comparison_start_depends_only_on_history_coverage(self):
        all_bars = bars(assets=("BTC", "ETH", "SOL", "SUI"),
                        prices=(100.,) * 725)
        spec = {**experiment(order=["BTC", "ETH", "SOL", "SUI"]),
                "test_start_ms": START + 718 * H4,
                "test_end_ms": START + 725 * H4,
                "development_cutoff_ms": START + 717 * H4}
        comparable = derive(spec, all_bars)
        self.assertEqual(comparable["test_start_ms"], START + 719 * H4)
        self.assertEqual(comparable["asset_order"], ["BTC", "ETH", "SOL"])
        self.assertEqual(comparable["comparison_scope"]["warmup_bars"], 720)

    def test_fixed_ensemble_requires_four_paired_real_artifact_identities(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            prediction_paths, manifest_paths = [], []
            for index, family in enumerate(FAMILIES):
                artifact = format(index + 1, "x") * 64
                model_id = f"{family}@{artifact[:12]}"
                pred_path = root / f"{family}.jsonl"
                pred_path.write_text(json.dumps({"model_id": model_id, "artifact_id": artifact,
                                                 "asset": "BTC", "signal_end_ms": START + H4 - 1,
                                                 "probabilities": [0.1 * (index + 1),
                                                                   1.0 - 0.1 * (index + 1), 0.],
                                                 "truth_label_index": 1}) + "\n", encoding="utf-8")
                manifest_path = root / f"{family}.json"
                manifest_path.write_text(json.dumps({"id": family, "artifact_id": artifact,
                                                     "feature_schema": "kraken-ohlcv-16-v1",
                                                     "label_version": "parente-source-5-2-v1",
                                                     "classes": ["BUY", "HOLD", "SELL"],
                                                     "timeframe_ms": H4, "target_horizon_bars": 2,
                                                     "history_bars": 319, "members": {"BTC": {}},
                                                     "latest_model_selection_through_ms": START - H4}),
                                         encoding="utf-8")
                prediction_paths.append(pred_path)
                manifest_paths.append(manifest_path)
            provenance, rows = combine(prediction_paths, manifest_paths, experiment())
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["probabilities"][0], .25)
            self.assertAlmostEqual(rows[0]["probabilities"][1], .75)
            self.assertEqual(rows[0]["artifact_id"], provenance["artifact_id"])
            self.assertEqual(provenance["identity_payload"]["weights"], [.25] * 4)
            same, same_rows = combine(list(reversed(prediction_paths)),
                                      list(reversed(manifest_paths)), experiment())
            self.assertEqual((provenance["artifact_id"], rows), (same["artifact_id"], same_rows))
            with self.assertRaisesRegex(EvaluationError, "exactly four"):
                combine(prediction_paths[:3], manifest_paths, experiment())
            wrong = json.loads(prediction_paths[1].read_text(encoding="utf-8"))
            wrong["truth_label_index"] = 2
            prediction_paths[1].write_text(json.dumps(wrong) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EvaluationError, "truth label differs"):
                combine(prediction_paths, manifest_paths, experiment())

    def test_bundled_ensemble_summary_preserves_metrics_without_trade_arrays(self):
        report = evaluate(predictions(), bars(), experiment())
        compact = _compact_evaluation(report)
        self.assertEqual(compact["classification"], report["classification"])
        self.assertEqual(compact["replay"]["unlimited_base"]["net_return"],
                         report["replay"]["unlimited_base"]["net_return"])
        self.assertNotIn("trades", compact["replay"]["unlimited_base"])
        self.assertNotIn("paired_equity_4h", compact["replay"]["unlimited_base"])

    def test_duplicate_revision_and_class_order_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bars.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["asset", "open_time_ms", "open", "high", "low", "close", "volume"])
                writer.writerow(["BTC", START, 100, 100, 100, 100, 1])
                writer.writerow(["BTC", START, 101, 101, 101, 101, 1])
            with self.assertRaisesRegex(EvaluationError, "Duplicate or revised"):
                read_bars(path, H4)
            prediction = Path(root) / "predictions.jsonl"
            prediction.write_text(json.dumps({"model_id": "m", "artifact_id": "hash", "asset": "BTC",
                                              "signal_end_ms": START + H4 - 1,
                                              "probabilities": [.7, .2, .1], "label_index": 2,
                                              "truth_label_index": 0}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EvaluationError, "mismatch"):
                read_predictions(prediction)

    def test_cli_report_is_read_only_and_has_reproducible_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bars_path, pred_path, spec_path, output = (root / name for name in
                                                      ("bars.csv", "predictions.jsonl", "experiment.json", "report.json"))
            with bars_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["asset", "open_time_ms", "open", "high", "low", "close", "volume"])
                for i in range(5):
                    writer.writerow(["BTC", START + i * H4, 100, 100, 100, 100, 1])
            pred_path.write_text("".join(json.dumps({"model_id": "trained-lstm", "artifact_id": "sha256-test",
                                                    "asset": "BTC", "signal_end_ms": START + (i + 1) * H4 - 1,
                                                    "probabilities": [.9, .05, .05] if i == 0 else [.05, .9, .05],
                                                    "truth_label_index": 0 if i == 0 else 1}) + "\n"
                                         for i in range(3)), encoding="utf-8")
            spec_path.write_text(json.dumps(experiment()), encoding="utf-8")
            self.assertEqual(main(["--predictions", str(pred_path), "--candles-4h", str(bars_path),
                                   "--experiment", str(spec_path), "--output", str(output)]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["model_id"], "trained-lstm")
            self.assertEqual(report["recommendation"], "no demonstrated winner")
            self.assertEqual(set(report["input_sha256"]), {"predictions", "candles_4h", "experiment"})
            self.assertEqual(len(report["input_sha256"]["predictions"]), 64)


if __name__ == "__main__":
    unittest.main()
