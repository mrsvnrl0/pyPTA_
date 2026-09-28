"""A single applied NN model must drive Home, positions and the paper simulation."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from adaptive_crypto.administration import apply_settings
from adaptive_crypto.core import H4
from adaptive_crypto.neural import DEFAULT_MODEL
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.state_paths import open_stores
from test_neural_strategy import ASSETS, RULES, NOW, bars, quote, NUMERIC_AVAILABLE


@unittest.skipUnless(NUMERIC_AVAILABLE, "Install requirements-neural.txt for model tests")
class SharedNeuralModelTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.settings = self.root/"model.npz", self.root/"settings.json"
        with np.load(DEFAULT_MODEL, allow_pickle=False) as model:
            self.arrays = {key:model[key].copy() for key in model.files}
        for i in range(4):
            self.arrays["w"+str(i)] = np.zeros_like(self.arrays["w"+str(i)])
            self.arrays["b"+str(i)] = np.zeros_like(self.arrays["b"+str(i)])
        self.write_model("BUY")
        self.rules = replace(RULES, nn_model_path="model.npz")
        self.settings.write_text(json.dumps({"assets":[{"name":n,**v} for n,v in ASSETS.items()],
                                             "strategy":asdict(self.rules)}))
        self.owner = open_stores(self.root/"study.json",ASSETS,self.rules,"sqlite")
        self.store,self.positions = self.owner.__enter__()
        self.addCleanup(self.owner.__exit__,None,None,None)
        self.provider = Mock()
        self.provider.now.return_value = NOW
        self.provider.quotes.return_value = {"BTC/USD":quote()}
        self.provider.candles.side_effect = lambda symbol,interval,now:bars(now,160,interval)
        self.runtime = DashboardRuntime(ASSETS,self.rules,self.store,provider=self.provider,
                                        position_store=self.positions,settings_path=self.settings)
        self.runtime.gex = Mock()
        self.runtime.gex.snapshot.return_value = None
        self.positions.open_position(ASSETS,"BTC","long",100,1,uuid.uuid4().hex,NOW-2000)
        self.runtime.positions.monitor_buy_watches = Mock()

    def write_model(self,label):
        self.arrays["b3"][:] = 0
        self.arrays["b3"][("BUY","HOLD","SELL").index(label)] = 10
        self.np.savez(self.path,**self.arrays)

    def test_replacing_file_cannot_make_home_sell_while_paper_buys(self):
        self.write_model("SELL")
        with patch.object(self.runtime.engine.model,"predict",wraps=self.runtime.engine.model.predict) as predict:
            self.runtime.scan_once()
            self.assertEqual(predict.call_count,1)
        market=self.runtime.market["BTC"]["nn"]["signal"]
        paper=self.runtime.data["BTC"]["neural"]["signal"]
        position=self.positions.snapshot()["positions"][0]["nn_guidance"]
        self.assertEqual(market,paper)
        self.assertEqual(market["label"],"BUY")
        self.assertEqual(position["signal"],"BUY")
        self.assertEqual(len(self.store.snapshot()["assets"]["BTC"]["trades"]),1)

    def test_apply_reloads_all_consumers_together_without_retrading_same_candle(self):
        self.runtime.scan_once()
        first=self.runtime.market["BTC"]["nn"]["signal"]["model_id"]
        self.write_model("SELL")
        with patch("adaptive_crypto.administration.NaiveGEX",return_value=self.runtime.gex):
            apply_settings(self.runtime,self.runtime.generation)
        self.runtime.scan_once()
        market=self.runtime.market["BTC"]["nn"]["signal"]
        paper=self.runtime.data["BTC"]["neural"]["signal"]
        self.assertEqual(market,paper)
        self.assertEqual(market["label"],"SELL")
        self.assertNotEqual(market["model_id"],first)
        self.assertEqual(self.positions.snapshot()["positions"][0]["nn_guidance"]["signal"],"SELL")
        trades=self.store.snapshot()["assets"]["BTC"]["trades"]
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]["status"],"active")

    def test_pause_still_reads_applied_model_without_trading(self):
        self.runtime.paused=True
        before=self.store.snapshot()
        self.write_model("SELL")
        self.runtime.scan_once()
        self.assertEqual(self.runtime.market["BTC"]["nn"]["signal"]["label"],"BUY")
        self.assertEqual(self.store.snapshot(),before)

    def test_model_failure_is_consistent_across_consumers_and_stops_still_run(self):
        self.runtime.scan_once()
        self.runtime.engine.model=None
        self.runtime.engine.model_error="Model unavailable"
        self.provider.quotes.return_value={"BTC/USD":quote(price=80)}
        self.runtime.scan_once()
        self.assertIsNone(self.runtime.market["BTC"]["nn"]["signal"])
        self.assertIsNone(self.runtime.data["BTC"]["neural"]["signal"])
        self.assertEqual(self.runtime.market["BTC"]["nn"]["error"],"Model unavailable")
        self.assertEqual(self.positions.snapshot()["positions"][0]["nn_guidance"]["light"],"WAIT")
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"][0]["status"],"stopped")

    def test_read_is_pure_and_returned_signal_cannot_mutate_the_shared_cache(self):
        before=self.store.snapshot()
        first=self.runtime.engine.read("BTC",bars(),NOW)
        first["signal"]["label"]="SELL"
        first["signal"]["probabilities"]["BUY"]=0
        second=self.runtime.engine.read("BTC",bars(),NOW)
        self.assertEqual(second["signal"]["label"],"BUY")
        self.assertGreater(second["signal"]["probabilities"]["BUY"],.99)
        self.assertEqual(self.store.snapshot(),before)

    def test_cached_signal_is_not_reused_when_current_candles_or_clock_are_missing(self):
        self.runtime.engine.read("BTC",bars(),NOW)
        for high,now,errors in (([],NOW,{}),(bars(),NOW+H4,{}),(bars(),NOW,{"clock":"offline"})):
            with self.subTest(now=now,errors=errors):
                reading=self.runtime.engine.read("BTC",high,now,errors)
                self.assertIsNone(reading["signal"])
                self.assertTrue(reading["error"])
                self.assertEqual(reading["expires_ms"],now)


if __name__=="__main__":
    unittest.main()
