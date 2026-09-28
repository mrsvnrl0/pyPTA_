"""NN execution must precede optional SMC work and use the actual quote/clock."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from adaptive_crypto.core import H4, M5, M30
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.state_paths import open_stores
from test_neural_strategy import FakeModel, RULES, NOW, bars

ASSETS = {name: {"symbol":name+"/USD","price_decimals":2} for name in ("BTC","ETH","SOL")}


class TimedProvider:
    def __init__(self):
        self.stamp=NOW
        self.calls=[]
        self.smc_delay=0
        self.critical_delay=0
        self.broken_high=set()
        self.prices={name:100 for name in ASSETS}

    def now(self):
        return self.stamp

    def candles(self,symbol,interval,requested):
        self.calls.append(("candles",symbol,interval,self.stamp))
        if interval==M30:
            self.stamp+=self.smc_delay
            if self.smc_delay:
                raise RuntimeError("SMC advisory feed timeout")
        else:
            self.stamp+=self.critical_delay
        if interval==H4 and symbol in self.broken_high:
            raise RuntimeError("4H unavailable")
        return bars(self.stamp,160,interval)

    def quotes(self,symbols):
        self.calls.append(("quote",tuple(symbols),None,self.stamp))
        return {symbol:{"bid":self.prices[symbol.split("/")[0]],
                        "ask":self.prices[symbol.split("/")[0]]+.01,
                        "asof_ms":self.stamp} for symbol in symbols}


class NeuralRuntimeTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rules=replace(RULES,risk_per_trade=.005,max_total_risk=.02)
        self.owner=open_stores(Path(self.temp.name)/"study.json",ASSETS,self.rules,"sqlite")
        self.store,self.positions=self.owner.__enter__()
        self.addCleanup(self.owner.__exit__,None,None,None)
        self.provider=TimedProvider()
        self.model=FakeModel("BUY")
        with patch("adaptive_crypto.neural_engine.NeuralModel",return_value=self.model):
            self.runtime=DashboardRuntime(ASSETS,self.rules,self.store,provider=self.provider,
                                          position_store=self.positions)
        self.runtime.gex=Mock()
        self.runtime.gex.snapshot.return_value=None
        self.executions=[]
        evaluate=self.runtime.engine.evaluate
        def record(name,*args,**kwargs):
            self.executions.append((name,self.provider.stamp,len(self.provider.calls)))
            return evaluate(name,*args,**kwargs)
        self.runtime.engine.evaluate=record

    def trades(self,name):
        return self.store.snapshot()["assets"][name]["trades"]

    def test_all_assets_execute_before_any_slow_smc_request(self):
        self.provider.smc_delay=61000
        self.runtime.scan_once()
        first_advisory=next(i for i,call in enumerate(self.provider.calls) if call[0]=="candles" and call[2]==M30)
        self.assertEqual([x[0] for x in self.executions],list(ASSETS))
        self.assertTrue(all(index<=first_advisory for _,_,index in self.executions))
        for name in ASSETS:
            self.assertEqual(len(self.trades(name)),1)
            self.assertLessEqual(self.trades(name)[0]["opened_ms"]-self.trades(name)[0]["signal_end"],60000)
            self.assertEqual(self.runtime.market[name]["nn"]["signal"]["label"],"BUY")

    def test_quote_is_obtained_after_slow_required_inputs(self):
        self.provider.critical_delay=17000
        self.runtime.scan_once()
        first=self.trades("BTC")
        self.assertEqual(len(first),1)
        self.assertEqual(first[0]["opened_ms"],NOW+34000)
        self.assertEqual(self.provider.calls[0][0],"candles")
        self.assertEqual(self.provider.calls[1][0],"candles")
        self.assertEqual(self.provider.calls[2][0],"quote")
        self.assertEqual(self.runtime.data["BTC"]["quote"]["asof_ms"],NOW+34000)

    def test_inference_delay_cannot_use_an_old_timestamp_to_enter_late(self):
        predict=self.model.predict
        def delayed(high,asset):
            self.provider.stamp+=61000
            return predict(high,asset)
        self.model.predict=delayed
        self.runtime.scan_once()
        for name in ASSETS:
            self.assertEqual(self.trades(name),[])
            self.assertIn("window elapsed",self.store.snapshot()["assets"][name]["last_result"])

    def test_quote_failure_for_one_asset_does_not_block_other_assets(self):
        quotes=self.provider.quotes
        def outage(symbols):
            if symbols==["BTC/USD"]:
                raise RuntimeError("BTC quote unavailable")
            return quotes(symbols)
        self.provider.quotes=outage
        self.runtime.scan_once()
        self.assertEqual(self.trades("BTC"),[])
        self.assertEqual(len(self.trades("ETH")),1)
        self.assertEqual(len(self.trades("SOL")),1)

    def test_stop_runs_before_advisory_work_even_with_missing_4h(self):
        self.runtime.scan_once()
        self.provider.stamp+=M5
        self.provider.smc_delay=61000
        self.provider.broken_high.add("BTC/USD")
        self.provider.prices["BTC"]=80
        self.provider.calls.clear()
        self.executions.clear()
        self.runtime.scan_once()
        self.assertEqual(self.trades("BTC")[0]["status"],"stopped")
        self.assertEqual(self.trades("BTC")[0]["closed_ms"],NOW+M5)
        self.assertEqual(self.trades("ETH")[0]["status"],"active")
        self.assertIsNone(self.runtime.data["BTC"]["neural"]["signal"])

    def test_one_ledger_exception_does_not_prevent_other_asset_execution(self):
        evaluate=self.runtime.engine.evaluate
        def failing(name,*args,**kwargs):
            if name=="BTC":
                raise RuntimeError("Failed BTC commit")
            return evaluate(name,*args,**kwargs)
        self.runtime.engine.evaluate=failing
        self.runtime.scan_once()
        self.assertEqual(self.trades("BTC"),[])
        self.assertIn("Failed BTC commit",self.runtime.data["BTC"]["scan_error"])
        self.assertEqual(len(self.trades("ETH")),1)
        self.assertEqual(len(self.trades("SOL")),1)

    def test_one_asset_inference_failure_does_not_block_remaining_assets(self):
        predict=self.model.predict
        def failing(high,asset):
            if asset=="BTC":
                raise RuntimeError("Invalid BTC features")
            return predict(high,asset)
        self.model.predict=failing
        self.runtime.scan_once()
        self.assertEqual(self.trades("BTC"),[])
        self.assertEqual(len(self.trades("ETH")),1)
        self.assertEqual(len(self.trades("SOL")),1)
        self.assertIn("Invalid BTC features",self.runtime.data["BTC"]["neural"]["error"])

    def test_pause_prevents_paper_orders_while_market_readings_continue(self):
        self.runtime.paused=True
        self.provider.smc_delay=61000
        before=self.store.snapshot()
        self.runtime.scan_once()
        self.assertEqual(self.executions,[])
        self.assertEqual(before,self.store.snapshot())
        self.assertTrue(all(self.runtime.market[name]["nn"]["signal"] for name in ASSETS))


if __name__=="__main__":
    unittest.main()
