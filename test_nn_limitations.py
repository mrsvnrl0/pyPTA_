"""Selectable NN execution limits, multi-lot accounting and mode transitions."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_crypto.core import DataError, H4, M5, Rules
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.neural_ledger import validate
from adaptive_crypto.ledger import fingerprint
from adaptive_crypto.settings_editor import read_editor
from adaptive_crypto.state_paths import open_stores
from test_neural_strategy import ASSETS, NOW, RULES, FakeModel, bars, quote
import test_settings_editor as editor_fixtures
import test_runtime_neural_timing as runtime_fixtures


class ExecutionLimitTests(unittest.TestCase):
    backend = 'json'

    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name); self.base=self.root/'state.json'
        self.rules=RULES
        self.owner=open_stores(self.base,ASSETS,self.rules,self.backend)
        self.store,self.positions=self.owner.__enter__()
        self.addCleanup(self.owner.__exit__,None,None,None)
        self.model=FakeModel('BUY')
        self.engine=NeuralEngine(ASSETS,self.rules,self.store,model=self.model)
        blocker=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline test'))
        blocker.start(); self.addCleanup(blocker.stop)

    def evaluate(self,now=NOW,price=100,**kwargs):
        return self.engine.evaluate('BTC',kwargs.get('quote',quote(now,price)),kwargs.get('high',bars(now)),
                                    kwargs.get('low',bars(now,60,M5)),now,kwargs.get('errors'))

    def trades(self): return self.store.snapshot()['assets']['BTC']['trades']

    def mode(self,limited,**kwargs):
        self.rules=replace(self.rules,nn_limitations=limited,**kwargs)
        self.engine.rules=self.rules
        self.store.transaction(lambda document:document.update(settings=asdict(self.rules)))

    def stacked(self):
        self.evaluate()  # A bounded legacy-style position leaves spare cash.
        self.mode(False)
        self.evaluate(NOW+H4)
        self.assertEqual(len(self.trades()),2)

    def test_default_mode_still_keeps_one_position_on_later_buy(self):
        self.evaluate(); first=self.trades()[0]
        self.evaluate(NOW+H4)
        self.assertEqual(len(self.trades()),1)
        self.assertEqual(self.trades()[0]['stop'],first['stop'])
        self.assertTrue(first['limitations_enabled'])

    def test_off_uses_cash_ignoring_risk_floor_allocation_spread_and_minimum(self):
        self.mode(False,risk_per_trade=.000001,max_total_risk=.000001,max_allocation=.000001,
                  paper_floor=999,minimum_notional=5000,max_spread_bps=.001)
        self.evaluate(quote={**quote(),'ask':150},low=[])
        trade,=self.trades(); state=self.store.snapshot()
        self.assertIsNone(trade['stop']); self.assertFalse(trade['limitations_enabled'])
        self.assertEqual(state['cash'],0)
        self.assertAlmostEqual(trade['collateral']+trade['entry_fee'],state['initial_equity'])
        self.assertAlmostEqual(trade['initial_risk_usd'],state['initial_equity'])
        self.assertFalse(trade['tracking_gap'])
        validate(state)
        self.evaluate(NOW+H4)
        self.assertEqual(len(self.trades()),1)  # No invented cash or tiny residual lot.

    def test_new_buy_can_add_a_lot_and_same_candle_never_repeats(self):
        self.stacked(); row=self.evaluate(NOW+H4+2000)
        self.assertEqual(len(self.trades()),2)
        self.assertEqual(row['neural']['active_count'],2)
        self.assertEqual(len(row['neural']['trades']),2)
        self.assertEqual(row['neural']['status'],'2 ACTIVE PAPER TRADES')
        self.assertIsNotNone(self.trades()[0]['stop']); self.assertIsNone(self.trades()[1]['stop'])
        snapshot=self.store.snapshot()
        self.engine=NeuralEngine(ASSETS,self.rules,self.store,model=FakeModel('BUY'))
        self.evaluate(NOW+H4+3000)
        self.assertEqual(self.store.snapshot(),snapshot)

    def test_sell_closes_all_lots_and_cash_fees_outbox_reconcile(self):
        self.stacked(); self.model.label='SELL'
        self.evaluate(NOW+2*H4,price=106)
        state=self.store.snapshot(); trades=self.trades()
        self.assertTrue(all(t['status']=='sold' for t in trades))
        self.assertAlmostEqual(state['cash'],state['initial_equity']+sum(t['realized_pnl'] for t in trades))
        self.assertEqual(len(state['outbox']),4)
        self.assertEqual(len({e['id'] for e in state['outbox']}),4)
        self.assertIn('2 neural paper positions',state['assets']['BTC']['last_result'])
        validate(state)

    def test_stop_one_lot_and_sell_remaining_lot_on_same_observation(self):
        self.stacked(); self.model.label='SELL'
        self.evaluate(NOW+2*H4,price=80)
        first,second=self.trades()
        self.assertEqual(first['status'],'stopped')
        self.assertEqual(second['status'],'sold')
        validate(self.store.snapshot())

    def test_stop_does_not_close_unstopped_lot_or_reenter_on_same_buy(self):
        self.stacked(); self.evaluate(NOW+2*H4,price=80)
        first,second=self.trades()
        self.assertEqual(first['status'],'stopped'); self.assertEqual(second['status'],'active')
        self.assertIsNone(second['stop']); self.assertGreater(self.store.snapshot()['cash'],0)
        self.evaluate(NOW+2*H4+3000,price=80)
        self.assertEqual(len(self.trades()),2)

    def test_reenable_limits_keeps_multiple_existing_lots_and_their_stop_policy(self):
        self.stacked(); before=copy.deepcopy(self.trades()); self.mode(True)
        validate(self.store.snapshot())
        self.evaluate(NOW+2*H4,price=100)
        self.assertEqual(len(self.trades()),2)
        self.assertEqual([t['stop'] for t in self.trades()],[t['stop'] for t in before])
        self.model.label='SELL'; self.evaluate(NOW+3*H4,price=105)
        self.assertTrue(all(t['status']=='sold' for t in self.trades()))

    def test_no_stop_mode_does_not_sell_on_price_drop_even_after_reenable(self):
        self.mode(False); self.evaluate(low=[]); self.mode(True)
        self.model.label='HOLD'; self.evaluate(NOW+H4,price=20,low=[])
        self.assertEqual(self.trades()[0]['status'],'active'); self.assertIsNone(self.trades()[0]['stop'])

    def test_free_mode_keeps_freshness_clock_and_causal_candle_requirements(self):
        self.mode(False)
        for kwargs in [{'high':[]},{'quote':None},{'quote':quote(NOW-60000)},{'errors':{'clock':'offline'}}]:
            with self.subTest(kwargs=kwargs):
                self.evaluate(**kwargs); self.assertEqual(self.trades(),[])
        self.evaluate(NOW+61000)
        self.assertEqual(self.trades(),[])
        self.evaluate(NOW+H4,low=[])
        self.assertEqual(len(self.trades()),1)

    def test_multiple_lot_exit_rolls_back_all_accounting_and_notifications_on_failed_commit(self):
        self.stacked(); before=self.store.snapshot(); self.model.label='SELL'
        with patch.object(self.store._backend,'save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.evaluate(NOW+2*H4,price=106)
        self.assertEqual(self.store.snapshot(),before)
        self.evaluate(NOW+2*H4+2000,price=106)
        self.assertEqual(len(self.store.snapshot()['outbox']),4)
        validate(self.store.snapshot())

    def test_damaged_stop_policy_or_duplicate_entry_candle_rejected(self):
        self.stacked(); original=self.store.snapshot()
        for change in ['missing_policy','fake_stop','mode_type','risk','duplicate_candle']:
            state=copy.deepcopy(original); first,second=state['assets']['BTC']['trades']
            if change=='missing_policy': second.pop('limitations_enabled')
            elif change=='fake_stop': second['stop']=90
            elif change=='mode_type': second['limitations_enabled']=0
            elif change=='risk': second['initial_risk_usd']=0
            else: second['signal_end']=first['signal_end']
            with self.subTest(change=change),self.assertRaises(DataError):validate(state)

    def test_legacy_trade_without_mode_is_still_valid_and_never_rewritten(self):
        self.evaluate()
        self.store.transaction(lambda doc:doc['assets']['BTC']['trades'][0].pop('limitations_enabled'))
        before=self.trades()[0]
        self.mode(False)
        validate(self.store.snapshot())
        self.assertEqual(self.trades()[0],before)

    def test_durable_reopen_preserves_multiple_lots_and_deduplication(self):
        self.stacked(); before=self.store.snapshot()
        self.owner.__exit__(None,None,None)
        with open_stores(self.base,ASSETS,self.rules,self.backend) as (store,positions):
            self.assertEqual(store.snapshot()['assets']['BTC']['trades'],before['assets']['BTC']['trades'])
            engine=NeuralEngine(ASSETS,self.rules,store,model=FakeModel('BUY'))
            now=NOW+H4+3000
            engine.evaluate('BTC',quote(now),bars(now),bars(now,60,M5),now)
            self.assertEqual(store.snapshot()['assets']['BTC']['trades'],before['assets']['BTC']['trades'])
            self.assertEqual(store.snapshot()['cash'],before['cash'])


class SQLiteExecutionLimitTests(ExecutionLimitTests):
    backend='sqlite'


class LimitSettingsTests(unittest.TestCase):
    setUp=editor_fixtures.SettingsEditorTests.setUp
    post=editor_fixtures.SettingsEditorTests.post
    def test_checkbox_is_checked_by_default_and_save_requires_apply(self):
        page=self.client.get('/settings').data.decode()
        self.assertIn('type="checkbox" class="nn-limitations-checkbox"',page)
        self.assertIn('aria-describedby="nn-limitations-help" checked',page)
        doc,revision=read_editor(self.runtime); doc['strategy']['nn_limitations']=False
        saved=self.post('/api/settings/save',{'settings':doc,'revision':revision})
        self.assertEqual(saved.status_code,200,saved.json)
        self.assertTrue(self.runtime.rules.nn_limitations)
        self.assertFalse(json.loads(self.path.read_text())['strategy']['nn_limitations'])
        response=self.post('/api/settings/apply')
        self.assertEqual(response.status_code,200,response.json)
        self.assertFalse(self.runtime.rules.nn_limitations)
        self.assertIn('OFF · available cash only',self.client.get('/paper-trading').data.decode())

    def test_rule_is_boolean_only_and_does_not_change_legacy_fingerprint(self):
        self.assertTrue(Rules().nn_limitations)
        for value in ['false',0,1,None]:
            with self.subTest(value=value),self.assertRaises(DataError): replace(Rules(),nn_limitations=value).validate()
        self.assertEqual(fingerprint(ASSETS,Rules()),fingerprint(ASSETS,Rules(nn_limitations=False)))

    def test_unstopped_trade_renders_honestly_in_all_tables(self):
        rules=replace(self.runtime.rules,nn_limitations=False)
        self.runtime.rules=self.runtime.engine.rules=rules
        self.runtime.engine.model=FakeModel('BUY'); self.runtime.engine.model_error=None
        self.runtime.engine.evaluate('BTC',quote(),bars(),[],NOW)
        for path in ['/paper-trading','/trade-records']:
            if path=='/trade-records':
                self.runtime.engine.model.label='SELL'
                now=NOW+H4
                self.runtime.engine.evaluate('BTC',quote(now),bars(now),[],now)
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertIn('None · limitations off at entry',response.data.decode())
            self.assertIn('NN SELL only',response.data.decode())


class LimitRuntimeTests(unittest.TestCase):
    setUp=runtime_fixtures.NeuralRuntimeTimingTests.setUp
    trades=runtime_fixtures.NeuralRuntimeTimingTests.trades

    def test_no_stop_mode_does_not_wait_for_stop_candles_before_execution(self):
        self.runtime.rules=self.runtime.engine.rules=replace(self.rules,nn_limitations=False)
        self.runtime._scan_neural_paper()
        self.assertTrue(all(call[2]!=M5 for call in self.provider.calls))
        self.assertEqual(len(self.trades('BTC')),1)
        self.assertIsNone(self.trades('BTC')[0]['stop'])
        self.assertEqual(self.trades('ETH'),[])

    def test_mode_off_retains_stop_history_requests_for_existing_stopped_risk(self):
        self.runtime._scan_neural_paper()
        self.provider.calls=[]; self.provider.stamp+=M5
        self.runtime.rules=self.runtime.engine.rules=replace(self.rules,nn_limitations=False)
        self.runtime._scan_neural_paper()
        self.assertEqual(sum(call[2]==M5 for call in self.provider.calls),3)


if __name__=='__main__': unittest.main()
