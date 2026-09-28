"""Current product roles: read-only market, NN simulation, independent manual exit guides."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from adaptive_crypto.core import H4, M5, M30, DataError, Rules, load_application_settings, load_settings
from adaptive_crypto.gex_targets import context
from adaptive_crypto.market_analysis import analyse_market
from adaptive_crypto.position_guidance import nn_advice, target_advice, monitor_position_guidance
from adaptive_crypto.positions import PositionStore, validate_positions
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.web import create_app
from test_gex_targets import fixture, ASSETS
from test_neural_strategy import FakeModel, bars
from test_display_formatting import DashboardHTML


def reading(now, label="HOLD"):
    return {"signal": {"label":label,"signal_end":now//H4*H4-1,
            "probabilities":{"BUY":.2,"HOLD":.3,"SELL":.5}},
            "expires_ms":now//H4*H4+H4-1,"error":None}


class IndependentGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.high, self.low = fixture()
        self.now = self.low[-1].end+1
        self.quote = {"bid":self.low[-1].c-.00001,"ask":self.low[-1].c,"asof_ms":self.now}
        self.rules = Rules(strategy_model="neural_network",smc_pivot_strength=1,smc_stop_buffer_bps=10,smc_tp_sweep_buffer_bps=5)
        self.profile = {"status":"ready","stale":False,"base":"BTC","spot":self.quote["ask"],
            "asof_ms":self.now-1000,"calculated_ms":self.now-1000,"expires_ms":self.now+1000000,
            "regime":"positive","call_wall":{"strike":160},"put_wall":{"strike":100},"gamma_flip":None}
        self.positions = PositionStore(self.root/"positions.json")
        self.position,_ = self.positions.open_position(ASSETS,"BTC","long",120,1,uuid.uuid4().hex,self.now-1000,stop_price=110)

    def monitor(self, label="HOLD", profile=True, quote=None, now=None, rules=None):
        now, rules = now or self.now, rules or self.rules
        quote = self.quote if quote is None else quote
        ctx = context(self.high,self.low,quote,replace(rules,smc_gex_targets=True),self.profile if profile else None,now,"BTC/USD")
        self.positions.transaction(lambda doc: monitor_position_guidance(doc,"BTC","BTC/USD",self.high,self.low,
            quote,reading(now,label),rules,ctx,now))
        return self.positions.snapshot()["positions"][0]

    def test_all_three_guides_are_independent_and_positions_never_close(self):
        self.monitor()
        p = self.monitor("SELL", quote={**self.quote,"bid":125,"ask":125.01})
        self.assertAlmostEqual(p["position_targets"]["smc"]["price"],140.07)
        self.assertAlmostEqual(p["position_targets"]["gex_smc"]["price"],160.08)
        self.assertEqual(p["nn_guidance"]["light"],"TAKE PROFIT")
        self.assertEqual(target_advice(p,p["position_targets"]["smc"],self.quote,self.now)["light"],"HOLD")
        quote = {**self.quote,"bid":145,"ask":145.01}
        p = self.monitor("BUY",quote=quote)
        self.assertEqual(p["nn_guidance"]["light"],"HOLD")
        self.assertEqual(target_advice(p,p["position_targets"]["smc"],quote,self.now)["light"],"TAKE PROFIT")
        self.assertEqual(target_advice(p,p["position_targets"]["gex_smc"],quote,self.now)["light"],"HOLD")
        self.assertEqual(p["status"],"open")
        validate_positions(self.positions.snapshot())

    def test_missing_gex_never_saves_a_fallback_and_retries_later(self):
        p = self.monitor(profile=False)
        self.assertIsNotNone(p["position_targets"]["smc"])
        self.assertIsNone(p["position_targets"]["gex_smc"])
        self.assertIn("GEX",p["target_errors"]["gex_smc"])
        self.assertNotIn("nearest SMC liquidity used",p["target_errors"]["gex_smc"])
        p = self.monitor()
        self.assertEqual(p["position_targets"]["gex_smc"]["selection"]["method"],"gex_smc")

    def test_old_smc_fallback_is_tp1_and_gex_is_calculated_independently(self):
        p = self.monitor(profile=False)
        self.positions.transaction(lambda doc: (doc["positions"][0].update(
            target_mode="gex_smc",take_profit=p["position_targets"]["smc"]),
            doc["positions"][0].pop("position_targets")))
        p = self.monitor()
        self.assertEqual(p["position_targets"]["smc"]["liquidity_price"],140)
        self.assertEqual(p["position_targets"]["gex_smc"]["liquidity_price"],160)

    def test_targets_are_identical_for_all_previous_model_settings(self):
        expected = self.monitor()["position_targets"]
        for model in ("legacy","legacy_nn","smc_video","smc_nn","neural_network"):
            self.positions.transaction(lambda doc: doc["positions"][0].pop("position_targets"))
            p = self.monitor(rules=replace(self.rules,strategy_model=model,smc_gex_targets=False))
            self.assertEqual(p["position_targets"],expected)

    def test_both_target_lights_use_editable_stop_without_overriding_nn(self):
        self.monitor()
        q = {**self.quote,"bid":109,"ask":109.01}
        p = self.monitor("BUY",quote=q)
        self.assertEqual(p["nn_guidance"]["light"],"HOLD")
        for target in p["position_targets"].values():
            self.assertEqual(target_advice(p,target,q,self.now)["light"],"STOP LOSS")
        events = self.positions.snapshot()["outbox"]
        self.assertEqual(len([e for e in events if e["alert_type"]=="position_stop" and e["status"]=="queued"]),1)
        self.monitor("BUY",quote=q)
        self.assertEqual(len(self.positions.snapshot()["outbox"]),len(events))
        self.assertEqual(p["status"],"open")

    def test_nn_exit_uses_actual_entry_long_and_short_and_fails_closed(self):
        for side,adverse,gain,loss in (("long","SELL",125,115),("short","BUY",115,125)):
            p = {**self.position,"side":side}
            for mark,expected in ((gain,"TAKE PROFIT"),(loss,"STOP LOSS"),(120,"STOP LOSS")):
                q={"bid":mark,"ask":mark,"asof_ms":self.now}
                advice=nn_advice(p,reading(self.now,adverse),q,self.now)
                self.assertEqual(advice["light"],expected)
                self.assertIn("COVER" if side=="short" else "SELL",advice["action_label"])
            self.assertEqual(nn_advice(p,reading(self.now,adverse),q,self.now+30001)["light"],"WAIT")
            self.assertEqual(nn_advice(p,reading(self.now-H4,adverse),q,self.now)["light"],"WAIT")
            self.assertEqual(nn_advice(p,{**reading(self.now,adverse),"error":"No model"},q,self.now)["light"],"WAIT")

    def test_targets_stop_and_snapshots_survive_restart_without_retargeting(self):
        p=self.monitor()
        saved=copy.deepcopy(p["position_targets"])
        self.positions=PositionStore(self.positions.path)
        self.profile["call_wall"]["strike"]=200
        p=self.monitor(rules=replace(self.rules,smc_tp_sweep_buffer_bps=8))
        self.assertEqual(p["position_targets"],saved)
        self.assertEqual(p["stop_price"],110)

    def test_target_alerts_are_distinct_deduplicated_and_labelled_after_refresh(self):
        self.monitor()
        q={**self.quote,"bid":161,"ask":161.01}
        self.monitor(quote=q)
        self.monitor(quote=q)
        events=[e for e in self.positions.snapshot()["outbox"] if e.get("alert_type")=="target_reached"]
        self.assertEqual({e["target_method"] for e in events},{"smc","gex_smc"})
        self.assertEqual(len(events),2)
        self.assertTrue(all(e["text"].startswith("SMC") for e in events))
        self.assertEqual(self.positions.snapshot()["positions"][0]["status"],"open")

    def test_market_guide_is_read_only_with_minimum_entry_and_highest_target(self):
        before=self.positions.snapshot()
        row=analyse_market(self.high,self.low,self.quote,reading(self.now),self.rules,self.profile,self.now,"BTC/USD")
        self.assertIsNotNone(row["suggestion"])
        self.assertGreater(row["suggestion"]["entry_price"],row["suggestion"]["stop"])
        self.assertEqual(row["suggestion"]["exit_liquidity"],160)
        self.assertTrue(row["smc_long"]["checks"])
        self.assertTrue(row["smc_short"]["checks"])
        self.assertEqual(self.positions.snapshot(),before)
        stale=analyse_market(self.high,self.low,self.quote,reading(self.now),self.rules,self.profile,self.now+30001,"BTC/USD")
        self.assertIsNone(stale["suggestion"])


class ApplicationRolesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.rules=Rules(strategy_model="neural_network",smc_entry_minutes=1,smc_setup_minutes=15)
        self.owner=open_stores(self.root/"state.json",ASSETS,self.rules,"sqlite")
        self.store,self.positions=self.owner.__enter__()
        self.addCleanup(self.owner.__exit__,None,None,None)
        with patch("adaptive_crypto.neural_engine.NeuralModel",return_value=FakeModel("HOLD")):
            self.runtime=DashboardRuntime(ASSETS,self.rules,self.store,position_store=self.positions)
        self.now=1770000000000//H4*H4
        self.runtime.provider=Mock()
        self.runtime.provider.now.return_value=self.now
        self.runtime.provider.quotes.return_value={"BTC/USD":{"bid":100,"ask":100.01,"last":100,"asof_ms":self.now}}
        self.runtime.provider.candles.side_effect=lambda symbol,interval,now:bars(now,160,interval)
        self.runtime.gex=Mock()
        self.runtime.gex.snapshot.return_value={"status":"unavailable"}
        self.client=create_app(self.runtime).test_client()
        self.client.get("/positions")
        with self.client.session_transaction() as session:
            self.csrf=session["csrf_token"]

    def post(self,path,body):
        return self.client.post(path,json=body,headers={"X-CSRF-Token":self.csrf})

    def test_old_saved_modes_load_as_nn_without_rewriting_historical_config(self):
        for mode in ("legacy","legacy_nn","smc_nn","smc_video","neural_network"):
            path=self.root/"settings.json"
            path.write_text(json.dumps({"strategy":{"strategy_model":mode}}))
            before=path.read_bytes()
            self.assertEqual(load_application_settings(path)[1].strategy_model,"neural_network")
            self.assertEqual(load_settings(path)[1].strategy_model,mode)
            self.assertEqual(path.read_bytes(),before)

    def test_market_updates_while_paper_paused_and_smc_settings_do_not_change_nn_interval(self):
        before=self.store.snapshot()
        self.runtime.paused=True
        self.runtime.engine.evaluate=Mock(side_effect=AssertionError("Paused execution"))
        self.runtime.scan_once()
        self.runtime.engine.evaluate.assert_not_called()
        self.assertEqual(self.store.snapshot(),before)
        self.assertEqual(self.runtime.low_interval,M5)
        self.assertEqual({c.args[1] for c in self.runtime.provider.candles.call_args_list},{H4,M5,900000,60000})
        self.assertEqual(self.runtime.market["BTC"]["nn"]["signal"]["label"],"HOLD")

    def test_home_has_visible_probabilities_and_no_paper_status(self):
        self.runtime.scan_once()
        self.runtime.data["BTC"]["neural"]["status"]="PAPER-ONLY-STATE"
        before=self.store.snapshot()
        with patch("time.time",return_value=self.now/1000):
            html=self.client.get("/").text
        self.assertNotIn("PAPER-ONLY-STATE",html)
        self.assertNotIn("Live paper trades",html)
        self.assertIn("NN market probabilities",html)
        self.assertIn("10.00%",html)
        self.assertIn("80.00%",html)
        self.assertIn("SMC price guide",html)
        self.assertIn("Bullish SMC qualification",html)
        self.assertIn("Bearish SMC qualification",html)
        self.assertFalse(DashboardHTML(html).errors)
        self.assertIn("Live paper trades",self.client.get("/paper-trading").text)
        self.assertNotIn("Live paper trades",self.client.get("/positions").text)
        self.assertEqual(self.store.snapshot(),before)

    def test_stop_routes_reject_invalid_values_and_require_csrf(self):
        base={"asset":"BTC","position_type":"spot","entry":100,"quantity":1,"request_id":uuid.uuid4().hex}
        for bad in (0,False,True,-1,"NaN","Infinity"):
            self.assertEqual(self.post("/api/positions",{**base,"stop_price":bad}).status_code,400)
        response=self.post("/api/positions",{**base,"stop_price":90})
        self.assertEqual(response.status_code,201,response.json)
        p=response.json["position"]
        url="/api/positions/"+p["id"]+"/stop"
        self.assertEqual(self.client.post(url,json={"stop_price":91}).status_code,400)
        self.assertEqual(self.post(url,{"stop_price":0}).status_code,400)
        self.assertEqual(self.post(url,{"stop_price":91}).json["position"]["stop_price"],91)
        self.assertTrue(self.runtime.wake.is_set())
        self.assertIsNone(self.post(url,{"stop_price":""}).json["position"]["stop_price"])
        self.post("/api/positions/"+p["id"]+"/close",{"close":105})
        self.assertEqual(self.post(url,{"stop_price":92}).status_code,400)

    def test_gex_failure_cannot_block_nn_or_stop_guidance(self):
        self.positions.open_position(ASSETS,"BTC","long",110,1,uuid.uuid4().hex,self.now-1000,stop_price=105)
        self.runtime.gex.snapshot.side_effect=RuntimeError("Options offline")
        self.runtime.scan_once()
        p=self.positions.snapshot()["positions"][0]
        self.assertEqual(p["nn_guidance"]["light"],"HOLD")
        self.assertEqual(p["status"],"open")
        self.assertEqual(self.runtime.position_errors,{})
        self.assertTrue(any(e["alert_type"]=="position_stop" for e in self.positions.snapshot()["outbox"]))

if __name__=="__main__":
    unittest.main()
