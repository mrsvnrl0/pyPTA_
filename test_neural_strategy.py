"""Offline neural inference, execution and store integration regressions."""
from dataclasses import asdict, replace
import copy
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from adaptive_crypto.core import Candle, DataError, H4, M5, Rules
from adaptive_crypto.ledger import atomic_json, fingerprint
from adaptive_crypto.neural import CLASSES, FEATURES, NeuralModel, feature_frame, labels
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.neural_ledger import NeuralStore, close_trade, open_trade, validate
from adaptive_crypto.neural_tools import simulate
from adaptive_crypto.persistence import wal_version_supported
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.state_migration import export_state, import_state, verify_state
from adaptive_crypto.administration import apply_settings, purge_database
from adaptive_crypto.web import create_app

ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}
NOW = (1_800_000_000_000//H4)*H4 + 1000
RULES = Rules(strategy_model="neural_network", fee_rate=.001, paper_floor=0,
              risk_per_trade=.02, max_total_risk=.05)
NUMERIC_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("numpy", "pandas", "talib"))


def bars(now=NOW, count=160, interval=H4):
    end = now//interval*interval
    return [Candle(end-(count-i)*interval, 100+math.sin(i/7), 103+math.sin(i/7),
                   97+math.sin(i/7), 100+math.sin(i/7)+math.cos(i/3)*.5,
                   1000+i%17*45, interval) for i in range(count)]


def quote(now=NOW, price=100):
    return {"bid": price, "ask": price+.01, "asof_ms": now}


class FakeModel:
    identity = "a"*64
    trained_through_ms = 0
    metadata = {"trained_through_ms": 0}

    def __init__(self, label="BUY"):
        self.label, self.calls = label, 0

    def predict(self, candles, asset):
        self.calls += 1
        self.asset = asset
        return {"label": self.label, "probabilities": {s: .8 if s == self.label else .1 for s in CLASSES},
                "features": dict.fromkeys(FEATURES, 0.), "signal_end": candles[-1].end, "model_id": self.identity}


@unittest.skipUnless(NUMERIC_AVAILABLE, "Install requirements-neural.txt for model tests")
class NeuralMathTests(unittest.TestCase):
    def test_features_and_inference_match_archived_author_reference(self):
        import numpy as np
        fixture = json.loads((Path(__file__).parent/"test_data/neural_author_btc.json").read_text())
        model = NeuralModel()
        candles = [Candle(**row) for row in fixture["candles"]]
        values = model.features(candles, "BTC").iloc[-3:].to_numpy()
        np.testing.assert_allclose(values, fixture["features"], atol=1e-10, rtol=1e-9)
        np.testing.assert_allclose(model.probabilities(values), fixture["probabilities"], atol=2e-6, rtol=2e-5)

    def test_feature_prefix_is_unchanged_by_future_candles(self):
        import numpy as np
        candles = bars(count=180)
        prefix = feature_frame(candles[:150], (1200, 200))
        full = feature_frame(candles, (1200, 200))
        self.assertEqual(list(full), list(FEATURES))
        self.assertEqual(full.shape, (180, 36))
        np.testing.assert_allclose(prefix, full.iloc[:150], equal_nan=True)
        self.assertTrue(np.isfinite(full.iloc[-1]).all())

    def test_labels_future_tail_and_source_paper_difference(self):
        import numpy as np
        values = [100., 100., 100., 127.]
        source = labels(values, backward=1, forward=2)
        paper = labels(values, backward=1, forward=2, convention="paper")
        self.assertEqual(source.iloc[1], -1)
        self.assertEqual(paper.iloc[1], 0)
        self.assertTrue(np.isnan(source.iloc[-2:]).all())
        self.assertEqual(labels([100., 90.], backward=1, forward=1).iloc[0], 1)

    def test_model_outputs_finite_probabilities_and_rejects_bad_features(self):
        import numpy as np
        model = NeuralModel()
        result = model.predict(bars(), "BTC")
        self.assertAlmostEqual(sum(result["probabilities"].values()), 1., places=6)
        self.assertIn(result["label"], CLASSES)
        for bad in ([1, 2], np.full((1, 36), np.nan)):
            with self.assertRaises(DataError):
                model.probabilities(bad)
        with self.assertRaisesRegex(DataError, "calibration"):
            model.predict(bars(), "UNKNOWN")

    def test_bad_model_metadata_and_weights_fail_closed(self):
        import numpy as np
        model = NeuralModel()
        with np.load(model.path, allow_pickle=False) as data:
            original = {k: data[k].copy() for k in data.files}
        with tempfile.TemporaryDirectory() as tmp:
            for case in ("classes", "volume", "weights", "scale"):
                arrays = copy.deepcopy(original)
                metadata = json.loads(str(arrays["metadata"].item()))
                if case == "classes":
                    metadata["classes"].reverse()
                elif case == "volume":
                    metadata["volume_stats"]["BTC"] = [100, 0]
                elif case == "weights":
                    arrays["w0"][0, 0] = np.nan
                else:
                    arrays["scale"][0] = 0
                arrays["metadata"] = np.array(json.dumps(metadata))
                path = Path(tmp)/f"{case}.npz"
                np.savez(path, **arrays)
                with self.subTest(case=case), self.assertRaises(DataError):
                    NeuralModel(path)

    def test_neural_settings_do_not_change_legacy_fingerprint(self):
        self.assertEqual(fingerprint(ASSETS, Rules()), fingerprint(ASSETS, Rules(nn_stop_loss=.05)))
        for updates in ({"nn_stop_loss": 0}, {"nn_stop_loss": .11}, {"nn_signal_max_age_seconds": True},
                        {"nn_model_path": 1}):
            with self.subTest(updates=updates), self.assertRaises(DataError):
                replace(RULES, **updates).validate()

    def test_backtest_uses_next_open_and_charges_both_sides(self):
        candles = [Candle(i*H4, p, p+1, p-1, p, 1, H4) for i, p in enumerate((100, 110, 120, 130))]
        result = simulate(candles, ["BUY", "HOLD", "SELL", "BUY"], fee=.001, slippage=.0005)
        trade, = result["trades"]
        self.assertAlmostEqual(trade["entry"], 110*1.0005)
        self.assertAlmostEqual(trade["exit"], 130*.9995)
        self.assertAlmostEqual(result["final"], 1000/(110*1.0005*1.001)*130*.9995*.999)
        self.assertEqual(trade["opened_ms"], H4)
        self.assertEqual(trade["closed_ms"], 3*H4)

    def test_backtest_gap_stop_precedes_sell_and_no_same_bar_reentry(self):
        candles = [Candle(i*H4, p, p+1, p-1, p, 1, H4) for i, p in enumerate((100, 100, 80))]
        for signal in ("SELL", "BUY"):
            result = simulate(candles, ["BUY", signal, "HOLD"], fee=0, slippage=0)
            self.assertEqual(result["trade_count"], 1)
            self.assertEqual(result["trades"][0]["reason"], "gap_stop")
            self.assertEqual(result["final"], 800)
        with self.assertRaises(DataError):
            simulate([], [])


class NeuralExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"paper.neural.json"
        self.store = NeuralStore(self.path, ASSETS, RULES)
        self.model = FakeModel()
        self.engine = NeuralEngine(ASSETS, RULES, self.store, model=self.model)
        blocker = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Offline tests"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def evaluate(self, now=NOW, price=100, **kwargs):
        return self.engine.evaluate("BTC", kwargs.get("quote", quote(now, price)),
                                    kwargs.get("high", bars(now)), kwargs.get("low", bars(now, 60, M5)),
                                    now, kwargs.get("errors"))

    def trades(self):
        return self.store.snapshot()["assets"]["BTC"]["trades"]

    def test_buy_hold_sell_costs_and_no_duplicate_on_restart(self):
        self.evaluate()
        self.evaluate(now=NOW+2000)
        self.assertEqual(len(self.trades()), 1)
        self.assertEqual(self.model.calls, 1)
        first = self.trades()[0]
        self.assertAlmostEqual(first["entry"], 100.01*1.0005)
        self.assertAlmostEqual(first["stop"], first["entry"]*.9)
        self.store = NeuralStore(self.path, ASSETS, RULES)
        self.engine = NeuralEngine(ASSETS, RULES, self.store, model=self.model)
        self.evaluate(now=NOW+3000)
        self.assertEqual(len(self.trades()), 1)
        self.model.label = "HOLD"
        self.evaluate(now=NOW+H4)
        self.assertEqual(self.trades()[0]["status"], "active")
        self.model.label = "SELL"
        self.evaluate(now=NOW+2*H4, price=105)
        trade = self.trades()[0]
        self.assertEqual(trade["status"], "sold")
        self.assertAlmostEqual(trade["exit"], 105*.9995)
        self.assertAlmostEqual(self.store.snapshot()["cash"], RULES.paper_equity+trade["realized_pnl"])
        self.assertEqual(len(self.store.snapshot()["outbox"]), 2)
        validate(self.store.snapshot())

    def test_expired_or_missing_quotes_do_not_backfill(self):
        self.evaluate(quote=None)
        self.assertEqual(self.trades(), [])
        self.evaluate(now=NOW+61000)
        self.assertEqual(self.trades(), [])
        self.assertIn("elapsed", self.store.snapshot()["assets"]["BTC"]["last_result"])
        self.evaluate(now=NOW+H4)
        self.assertEqual(len(self.trades()), 1)

    def test_quote_before_signal_and_wide_spread_wait_for_recovery(self):
        self.evaluate(quote={**quote(), "asof_ms": NOW-2000})
        self.assertEqual(self.trades(), [])
        self.evaluate(quote={**quote(), "ask": 120})
        self.assertEqual(self.trades(), [])
        self.evaluate(now=NOW+2000)
        self.assertEqual(len(self.trades()), 1)

    def test_stale_missing_and_forming_candles_or_clock_prevent_entry(self):
        for kwargs in ({"high": bars()[:-1]}, {"high": bars(NOW+H4)}, {"low": bars(NOW, 60, M5)[:-1]},
                       {"errors": {"clock": "offline"}}, {"low": []}):
            with self.subTest(kwargs=list(kwargs)):
                self.evaluate(**kwargs)
                self.assertEqual(self.trades(), [])

    def test_training_period_cannot_be_traded(self):
        self.model.trained_through_ms = NOW
        row = self.evaluate()
        self.assertIn("calibration", row["neural"]["error"])
        self.assertEqual(self.trades(), [])

    def test_quote_stop_works_when_model_and_history_are_unavailable(self):
        self.evaluate()
        self.engine.model = None
        self.engine.model_error = "Model unavailable"
        self.evaluate(now=NOW+2000, price=80, high=[], low=[])
        trade = self.trades()[0]
        self.assertEqual(trade["status"], "stopped")
        self.assertAlmostEqual(trade["exit"], 80*.9995)
        self.assertTrue(trade["tracking_gap"])

    def test_completed_stop_bar_uses_adverse_gap_and_precedes_signal(self):
        self.evaluate()
        now = NOW+H4
        low = bars(now, 48, M5)
        low[1] = replace(low[1], o=80, l=79, c=82, h=83)
        self.model.label = "SELL"
        self.evaluate(now=now, low=low)
        self.assertEqual(self.trades()[0]["status"], "stopped")
        self.assertAlmostEqual(self.trades()[0]["exit"], 80*.9995)
        self.assertEqual(len(self.store.snapshot()["outbox"]), 2)

    def test_entry_containing_candle_cannot_backdate_stop(self):
        self.evaluate()
        now = NOW+M5
        low = bars(now, 60, M5)
        low[-1] = replace(low[-1], l=70)
        self.evaluate(now=now, low=low)
        self.assertEqual(self.trades()[0]["status"], "active")

    def test_failed_commit_cannot_publish_trade_or_outbox(self):
        before = self.store.snapshot()
        with patch("adaptive_crypto.neural_ledger.atomic_json", side_effect=OSError("disk full")):
            # Store injects the writer at construction, so fail at its real boundary.
            with patch.object(self.store._backend, "save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.evaluate()
        self.assertEqual(self.store.snapshot(), before)
        self.evaluate()
        self.assertEqual(len(self.trades()), 1)

    def test_damaged_ledger_rejected_without_rewrite(self):
        self.evaluate()
        original = self.store.snapshot()
        for field, value in (("initial_risk_usd", 999), ("asset", "ETH"), ("signal_end", 123),
                             ("probabilities", {"BUY": 1.2, "HOLD": -.2, "SELL": 0})):
            document = copy.deepcopy(original)
            document["assets"]["BTC"]["trades"][0][field] = value
            atomic_json(self.path, document)
            before = self.path.read_bytes()
            with self.subTest(field=field), self.assertRaises(DataError):
                NeuralStore(self.path, ASSETS, RULES)
            self.assertEqual(self.path.read_bytes(), before)

    def test_settings_keep_stops_and_interrupted_delivery_becomes_uncertain(self):
        self.evaluate()
        first = self.trades()[0]
        self.store.transaction(lambda d: d["outbox"][0].update(status="running", attempts=1))
        other = NeuralStore(self.path, ASSETS, replace(RULES, nn_stop_loss=.05, fee_rate=.002))
        self.assertEqual(other.snapshot()["assets"]["BTC"]["trades"][0], first)
        self.assertEqual(other.snapshot()["outbox"][0]["status"], "uncertain")

    def test_asset_display_name_does_not_select_volume_calibration(self):
        self.engine.assets = {"BTC": {"symbol": "ETH/USD"}}
        self.evaluate()
        self.assertEqual(self.model.asset, "ETH")


@unittest.skipUnless(NUMERIC_AVAILABLE, "Install requirements-neural.txt for model tests")
class NeuralIntegrationTests(unittest.TestCase):
    backend = "json"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base, self.settings = self.root/"study.json", self.root/"settings.json"
        self.write_settings(RULES)
        self.owner = open_stores(self.base, ASSETS, RULES, self.backend)
        self.store, self.positions = self.owner.__enter__()
        self.addCleanup(self.owner.__exit__, None, None, None)
        self.provider = Mock()
        self.provider.now.return_value = NOW
        self.provider.quotes.return_value = {"BTC/USD": quote()}
        self.provider.candles.side_effect = lambda symbol, interval, now: bars(now, 160, interval)
        self.runtime = DashboardRuntime(ASSETS, RULES, self.store, provider=self.provider,
                                        position_store=self.positions, settings_path=self.settings)
        self.runtime.engine.model = FakeModel()
        self.runtime.engine.model_error = None
        self.app = create_app(self.runtime, chart_provider=Mock())
        self.client = self.app.test_client()
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.session_transaction() as session:
            self.token = session["csrf_token"]
        blocker = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Offline tests"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def write_settings(self, rules):
        atomic_json(self.settings, {"assets": [{"name": n, **cfg} for n, cfg in ASSETS.items()], "strategy": asdict(rules)})

    def test_scan_dashboard_and_settings_selection_preserve_runtime_state(self):
        self.runtime.scan_once()
        before = self.store.snapshot()
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Active paper position", html)
        self.assertIn("Live paper trades", self.client.get("/paper-trading").get_data(as_text=True))
        self.assertIn("36 input measurements", html)
        self.assertEqual(self.client.get("/api/state").status_code, 200)
        saved=self.settings.read_bytes()
        for removed in ('legacy','legacy_nn','smc_video','smc_nn'):
            response = self.client.post("/api/settings/model", json={"strategy_model": removed}, headers={"X-CSRF-Token": self.token})
            self.assertEqual(response.status_code,400)
            self.assertEqual(self.settings.read_bytes(),saved)
        response = self.client.post("/api/settings/model", json={"strategy_model":"neural_network"}, headers={"X-CSRF-Token":self.token})
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(Path(response.json["backup"]).is_file())
        self.assertTrue(self.runtime.is_neural)
        self.assertEqual(self.store.snapshot(), before)
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertNotIn('<option value="smc_video"', html)

    def test_model_selection_requires_csrf_and_valid_name(self):
        before = self.settings.read_bytes()
        for body, headers in (({"strategy_model": "legacy"}, {}),
                              ({"strategy_model": "unknown"}, {"X-CSRF-Token": self.token})):
            response = self.client.post("/api/settings/model", json=body, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.settings.read_bytes(), before)

    def test_neural_settings_apply_relative_model_and_keep_recorded_trade(self):
        self.runtime.scan_once()
        self.assertEqual(self.runtime.gex.provider, self.runtime.deribit.fetch)
        before = self.store.snapshot()["assets"]["BTC"]["trades"]
        model = NeuralModel()
        (self.root/"model.npz").write_bytes(model.path.read_bytes())
        self.write_settings(replace(RULES, nn_stop_loss=.05, nn_model_path="model.npz"))
        result = apply_settings(self.runtime, self.runtime.generation)
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(self.runtime.rules.nn_stop_loss, .05)
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], before)
        self.assertEqual(self.runtime.gex.provider, self.runtime.deribit.fetch)

    def test_bad_model_apply_is_atomic_and_purge_can_resume(self):
        self.runtime.scan_once()
        before = self.store.snapshot()
        self.write_settings(replace(RULES, nn_model_path="missing.npz"))
        with self.assertRaises(DataError):
            apply_settings(self.runtime, self.runtime.generation)
        self.assertEqual(self.store.snapshot(), before)
        self.write_settings(RULES)
        purge_database(self.runtime, self.runtime.generation)
        self.assertTrue(self.runtime.paused)
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])
        apply_settings(self.runtime, self.runtime.generation)
        self.assertFalse(self.runtime.paused)


@unittest.skipUnless(wal_version_supported(sqlite3.sqlite_version_info), "Requires fixed SQLite runtime")
class NeuralSQLiteIntegrationTests(NeuralIntegrationTests):
    backend = "sqlite"

    def test_sqlite_failed_commit_cannot_publish_trade_and_claim_cannot_send(self):
        from adaptive_crypto.notifications import dispatch_once
        db = self.store._backend.database
        before = self.store.snapshot()
        with patch.object(db, "_commit", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.runtime.engine.evaluate("BTC", quote(), bars(), bars(NOW, 60, M5), NOW)
        self.assertEqual(self.store.snapshot(), before)
        self.runtime.scan_once()
        sender = Mock(return_value={"status": "sent"})
        with patch.object(db, "_commit", side_effect=OSError("claim failure")):
            with self.assertRaises(OSError):
                dispatch_once(self.store, "telegram", sender, NOW)
        sender.assert_not_called()
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "queued")

    def test_sqlite_model_switch_keeps_independent_balances_and_restart_identity(self):
        self.runtime.scan_once()
        original = self.store.snapshot()
        self.owner.__exit__(None, None, None)
        with open_stores(self.base, ASSETS, replace(RULES, strategy_model="smc_video")) as (smc, holdings):
            self.assertEqual(smc.snapshot()["assets"]["BTC"]["trades"], [])
            self.assertEqual(smc.snapshot()["cash"], RULES.paper_equity)
        with open_stores(self.base, ASSETS, RULES) as (neural, holdings):
            engine = NeuralEngine(ASSETS, RULES, neural, model=FakeModel())
            engine.evaluate("BTC", quote(), bars(), bars(NOW, 60, M5), NOW)
            self.assertEqual(neural.snapshot(), original)

    def test_sqlite_export_import_preserves_neural_trade_and_holdings(self):
        self.runtime.scan_once()
        original = self.store.snapshot()
        self.owner.__exit__(None, None, None)
        self.assertEqual(verify_state(self.base)["integrity"], "ok")
        result = export_state(self.base, self.root/"exported", self.settings)
        exported = Path(result["state_base"])
        import_state(exported, self.root/"exported"/"settings.json")
        with open_stores(exported, ASSETS, RULES) as (neural, holdings):
            self.assertEqual(neural.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
