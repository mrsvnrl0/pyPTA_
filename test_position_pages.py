"""Navigation, mixed manual types, individual alerts and combined NN actions."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from adaptive_crypto.core import Rules, H4, M30, M5, DataError
from adaptive_crypto.ledger import atomic_json, queue_event
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.web import create_app
from adaptive_crypto.notifications import dispatch_once
from adaptive_crypto.position_neural import combined_advice, monitor_neural_positions, PositionNeuralReader
from adaptive_crypto.trade_records import paper_records
from adaptive_crypto.position_options import paper_key, paper_alert_allowed
from adaptive_crypto.smc_ledger import close_trade
from test_display_formatting import DashboardHTML
from test_smc_formulas import full_fixture
from test_neural_strategy import bars, NOW, FakeModel

ASSETS = {'BTC': {'symbol':'BTC/USD', 'price_decimals':2}}


class PositionPagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root/'settings.json'
        self.rules = Rules(strategy_model='smc_video', smc_pivot_strength=1)
        atomic_json(self.settings, {'assets':[{'name':'BTC', **ASSETS['BTC']}], 'strategy':{'strategy_model':'smc_video'}})
        self.owner = open_stores(self.root/'state.json', ASSETS, self.rules, 'sqlite')
        self.store, self.positions = self.owner.__enter__()
        self.addCleanup(self.owner.__exit__, None, None, None)
        self.runtime = DashboardRuntime(ASSETS, self.rules, self.store, position_store=self.positions, settings_path=self.settings)
        self.app = create_app(self.runtime)
        self.client = self.app.test_client()
        self.client.get('/positions')
        with self.client.session_transaction() as session:
            self.token = session['csrf_token']

    def post(self, url, body):
        return self.client.post(url, json=body, headers={'X-CSRF-Token':self.token})

    def open(self, **values):
        body = dict(asset='BTC', position_type='spot', entry=100, quantity=2, request_id=uuid.uuid4().hex, target_mode='smc')
        body.update(values)
        response = self.post('/api/positions', body)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['position']

    def test_navigation_and_live_closed_separation(self):
        live = self.open(entry=101)
        closed = self.open(entry=97)
        self.post(f"/api/positions/{closed['id']}/close", {'close':102})
        home = self.client.get('/').text
        positions = self.client.get('/positions').text
        history = self.client.get('/trade-records').text
        for html in (home, positions, history, self.client.get('/settings').text):
            self.assertFalse(DashboardHTML(html).errors)
            for target in ('/positions','/trade-records','/settings'):
                self.assertIn(f'href="{target}"', html)
        self.assertNotIn('class="metrics"', home)
        self.assertNotIn('id="holdings"', home)
        self.assertIn(live['id'], positions)
        self.assertNotIn(closed['id'][:8], positions)
        self.assertIn(closed['id'][:8], history)
        self.assertNotIn(live['id'][:8], history)
        self.assertIn('My manual trades', history)

    def test_every_manual_type_and_target_available_in_spot_paper_mode(self):
        for ptype, side in [('spot','long'),('margin_long','long'),('margin_short','short')]:
            for method in ('smc','gex_smc','nn'):
                p = self.open(position_type=ptype, target_mode=method, nn_target_mode='smc', momentum_alerts=False, tp_alerts=True)
                self.assertEqual((p['side'], p['position_type'], p['target_mode']), (side, ptype, method))
                snap = next(x for x in self.runtime.snapshot()['holdings']['positions'] if x['id'] == p['id'])
                self.assertFalse(snap['review_required'])
        reopened = type(self.positions)(self.positions.path, market_mode='spot', backend=self.positions._backend)
        self.assertEqual(len(reopened.snapshot()['positions']), 9)

    def test_invalid_preferences_or_inconsistent_types_do_not_create_position(self):
        for values in ({'side':'long','position_type':'margin_short'}, {'target_mode':'unknown'}, {'tp_alerts':'false'}, {'nn_target_mode':'nn'}):
            body = dict(asset='BTC', position_type='spot', entry=100, quantity=1, request_id=uuid.uuid4().hex)
            body.update(values)
            self.assertEqual(self.post('/api/positions', body).status_code, 400)
        self.assertEqual(self.positions.snapshot()['positions'], [])

    def test_individual_alert_switches_cancel_only_matching_queued_events(self):
        p, other = self.open(), self.open()
        def seed(doc):
            for pos in (p, other):
                for kind in ('position_momentum','near_take_profit'):
                    queue_event(doc, pos['id']+kind, 'telegram', 'Test', NOW)
                    doc['outbox'][-1].update(position_ids=[pos['id']], alert_type=kind)
        self.positions.transaction(seed)
        response = self.post(f"/api/positions/{p['id']}/alerts", {'momentum_alerts':False,'tp_alerts':True})
        self.assertEqual(response.status_code, 200)
        events = {e['id']:e for e in self.positions.snapshot()['outbox']}
        self.assertEqual(events[p['id']+'position_momentum']['status'],'cancelled')
        self.assertEqual(events[p['id']+'near_take_profit']['status'],'queued')
        self.assertEqual(events[other['id']+'position_momentum']['status'],'queued')
        self.post(f"/api/positions/{p['id']}/alerts", {'momentum_alerts':True,'tp_alerts':True})
        self.assertTrue(self.positions.snapshot()['outbox'][0]['retired'])

    def test_alert_controls_require_csrf_and_open_position(self):
        p = self.open()
        url = f"/api/positions/{p['id']}/alerts"
        payload = {'momentum_alerts':False,'tp_alerts':False}
        self.assertEqual(self.client.post(url,json=payload).status_code,400)
        self.post(f"/api/positions/{p['id']}/close",{'close':101})
        self.assertEqual(self.post(url,payload).status_code,400)

    def test_targets_still_calculated_when_tp_alerts_off(self):
        high, low = full_fixture('long')
        now = low[-1].end+1
        p, _ = self.positions.open_position(ASSETS,'BTC','long',115,2,uuid.uuid4().hex,now-1000, 'nn', 'spot', 'smc', True, False)
        quote = {'bid':low[-1].c,'ask':low[-1].c+.01,'asof_ms':now}
        self.positions.monitor_take_profit('BTC','BTC/USD',high,low,quote,self.rules,now)
        target = self.positions.snapshot()['positions'][0]['take_profit']['price']
        quote.update(bid=target+.1,ask=target+.2,asof_ms=now+1000)
        self.positions.monitor_take_profit('BTC','BTC/USD',high,low,quote,self.rules,now+1000)
        self.assertEqual(self.positions.snapshot()['positions'][0]['take_profit']['state'],'reached')
        self.assertEqual(self.positions.snapshot()['outbox'],[])

    def test_dispatch_rechecks_preferences(self):
        p = self.open(momentum_alerts=False)
        def seed(doc):
            queue_event(doc,'disabled','telegram','Test',NOW)
            doc['outbox'][-1].update(alert_type='position_momentum',position_ids=[p['id']])
        self.positions.transaction(seed)
        sender = Mock()
        self.assertFalse(dispatch_once(self.positions,'telegram',sender,NOW))
        sender.assert_not_called()

    def test_personal_monitors_fetch_smc_and_nn_candles_under_legacy_strategy(self):
        p = self.open(target_mode='nn')
        self.runtime.rules = replace(self.rules, strategy_model='legacy')
        self.runtime.high_interval, self.runtime.low_interval = H4, 900000
        self.runtime.provider = Mock()
        self.runtime.provider.now.return_value = NOW
        self.runtime.provider.candles.side_effect = lambda symbol,interval,now: bars(now,160,interval)
        self.runtime.position_neural = Mock()
        self.runtime.position_neural.read.return_value = {'signal':None,'error':'Fixture has no model'}
        self.runtime.gex = Mock()
        with patch('adaptive_crypto.runtime.target_context',return_value=None):
            self.runtime._monitor_personal('BTC',ASSETS['BTC'],self.positions.snapshot(),bars(),bars(interval=900000),{'bid':100,'ask':100.01,'asof_ms':NOW},NOW,{})
        requested = {c.args[1] for c in self.runtime.provider.candles.call_args_list}
        self.assertEqual(requested,{M30,M5})
        self.runtime.position_neural.read.assert_called_once()
        self.runtime.gex.snapshot.assert_called_once_with('BTC')

    def paper_trade(self):
        high, low = full_fixture('long')
        now = low[-1].end+1
        quote = {'bid':low[-1].c-.01, 'ask':low[-1].c+.01, 'asof_ms':now}
        self.runtime.engine.evaluate('BTC',quote,high,low,now)
        quote.update(bid=115.49,ask=115.5,asof_ms=now+1000)
        self.runtime.engine.evaluate('BTC',quote,high,low,now+1000)
        return self.store.snapshot()['assets']['BTC']['trades'][0], now+2000

    def test_paper_alert_preferences_survive_reopen_without_changing_execution(self):
        trade, now = self.paper_trade()
        before = self.store.snapshot()
        key = paper_key('smc',trade['id'])
        url = f'/api/paper-positions/{key}/alerts'
        self.assertEqual(self.client.post(url,json={'momentum_alerts':False,'tp_alerts':False}).status_code,400)
        response = self.post(url,{'momentum_alerts':False,'tp_alerts':False})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.store.snapshot()['assets'],before['assets'])
        self.assertEqual(self.store.snapshot()['cash'],before['cash'])
        self.assertIn(f'action="{url}"',self.client.get('/paper-trading').text)
        reopened = type(self.positions)(self.positions.path,market_mode='spot',backend=self.positions._backend)
        self.assertFalse(reopened.snapshot()['paper_alert_preferences'][key]['tp_alerts'])
        # Exiting the paper trade still changes the ledger with its notifications off.
        def close(doc):
            current = doc['assets']['BTC']['trades'][0]
            close_trade(doc,'BTC',current,current['target'],'target',now)
        self.store.transaction(close)
        sender = Mock(return_value={'status':'sent'})
        while dispatch_once(self.store,'telegram',sender,now):
            pass
        self.assertFalse(any(c.args[0]['id'].startswith(trade['id']+':') for c in sender.call_args_list))
        self.assertNotIn(trade['id'][:12],self.client.get('/positions').text)
        self.assertIn(trade['id'][:12],self.client.get('/trade-records').text)
        self.assertEqual(self.post(url,{'momentum_alerts':True,'tp_alerts':True}).status_code,404)

    def test_paper_notification_mapping_preserves_stop_and_other_positions(self):
        doc = {'assets':{'BTC':{'trades':[{'id':'first'},{'id':'second'}]}}}
        prefs = {paper_key('smc','first'):{'momentum_alerts':True,'tp_alerts':False}}
        for suffix, payload, expected in [('near-tp',None,False),('TP1',None,False),('TP2',None,False),
                                         ('closed',{'status':'target'},False),('STOP',None,True),
                                         ('closed',{'status':'stopped'},True),('exit',{'status':'sold'},True)]:
            event = dict(id='first:'+suffix,kind='telegram',payload=payload)
            self.assertEqual(paper_alert_allowed(doc,event,prefs,'smc'),expected)
            event['id'] = 'second:'+suffix
            self.assertTrue(paper_alert_allowed(doc,event,prefs,'smc'))

    def test_neural_notifications_baseline_change_disable_and_tp_override(self):
        p, _ = self.positions.open_position(ASSETS,'BTC','long',100,1,uuid.uuid4().hex,NOW-H4,'nn','spot','smc')
        def monitor(label,end,now,mark=105):
            reading = {'signal':{'label':label,'signal_end':end,'model_id':'fixture'},'expires_ms':end+H4}
            self.positions.transaction(lambda doc: monitor_neural_positions(doc,'BTC','BTC/USD',reading,
                {'bid':mark,'ask':mark+.01,'asof_ms':now},self.rules,now))
        monitor('BUY',NOW-1000,NOW)
        self.assertFalse(self.positions.snapshot()['outbox'])
        monitor('SELL',NOW+H4-1000,NOW+H4)
        event = self.positions.snapshot()['outbox'][-1]
        self.assertEqual(event['payload']['action'],'SELL')
        self.assertEqual(event['action_label'],'STOP LOSS · SELL TO EXIT LONG')
        self.assertIn('STOP LOSS',event['title'])
        self.assertIn('Stop-loss exit: NN classification opposes this position.',event['text'])
        self.assertEqual(self.positions.snapshot()['positions'][0]['status'],'open')
        monitor('SELL',NOW+H4-1000,NOW+H4+1000)
        self.assertEqual(len(self.positions.snapshot()['outbox']),1)
        self.positions.set_alerts(p['id'],False,True,NOW+H4+2000)
        monitor('HOLD',NOW+2*H4-1000,NOW+2*H4)
        self.assertEqual(len(self.positions.snapshot()['outbox']),1)
        self.assertEqual(self.positions.snapshot()['outbox'][0]['status'],'cancelled')
        doc = self.positions.snapshot()
        doc['positions'][0].update(momentum_alerts=True,take_profit={'price':106})
        doc['outbox'][0].update(status='queued',signal_end=NOW+2*H4-1000,
                               payload={'action':'HOLD'})
        reading = {'signal':{'label':'HOLD','signal_end':NOW+2*H4-1000,'model_id':'fixture'},'expires_ms':NOW+3*H4}
        monitor_neural_positions(doc,'BTC','BTC/USD',reading,{'bid':107,'ask':107.01,'asof_ms':NOW+2*H4},self.rules,NOW+2*H4)
        self.assertEqual(doc['positions'][0]['neural_advice']['action'],'SELL')
        self.assertEqual(doc['positions'][0]['neural_advice']['action_label'],'TAKE PROFIT · SELL TO EXIT LONG')
        self.assertEqual(doc['outbox'][0]['status'],'cancelled')

    def test_nn_position_lights_distinguish_protection_hold_and_target(self):
        from adaptive_crypto.position_guidance import nn_advice
        self.positions.open_position(ASSETS,'BTC','long',100,1,uuid.uuid4().hex,NOW-H4,'nn','spot','smc')
        for signal, bid, expected in [('BUY',105,'hold'),('HOLD',105,'hold'),('SELL',105,'take-profit'),
                                      ('SELL',95,'stop-loss'),('BUY',110,'hold'),('HOLD',110,'hold')]:
            with self.subTest(signal=signal,bid=bid):
                quote = {'bid':bid,'ask':bid+.01,'asof_ms':NOW}
                reading = {'signal':{'label':signal,'signal_end':NOW//H4*H4-1},'expires_ms':NOW+H4}
                self.positions.transaction(lambda doc: doc['positions'][0].update(
                    nn_guidance=nn_advice(doc['positions'][0],reading,quote,NOW)))
                self.runtime.market['BTC'] = {'quote':quote}
                before = (self.store.snapshot(),self.positions.snapshot())
                with patch('time.time',return_value=NOW/1000):
                    html = self.client.get('/positions').text
                parsed = DashboardHTML(html)
                self.assertFalse(parsed.errors)
                lights = [attrs for tag,attrs in parsed.tags if 'signal-light' in attrs.get('class','').split()]
                self.assertEqual(len(lights),9)
                self.assertEqual([x['class'] for x in lights[:3] if x['aria-pressed'] == 'true'],
                                 [f'signal-light signal-{expected} is-selected'])
                self.assertIn('SELL (STOP LOSS)',parsed.text)
                self.assertIn('SELL (TAKE PROFIT)',parsed.text)
                with patch('time.time',return_value=(NOW+30001)/1000):
                    stale = self.client.get('/positions').text
                self.assertNotIn('aria-pressed="true"',stale)
                self.assertIn('WAITING FOR FRESH NN / QUOTE',stale)
                self.assertEqual((self.store.snapshot(),self.positions.snapshot()),before)

    def test_all_positions_have_three_exit_guides_and_shorts_keep_cover_direction(self):
        from adaptive_crypto.position_guidance import nn_advice
        for mode in ('smc','gex_smc'):
            self.open(target_mode=mode)
        self.assertEqual(self.client.get('/positions').text.count('data-position-signal'),6)
        self.positions.open_position(ASSETS,'BTC','short',100,1,uuid.uuid4().hex,NOW-H4,'nn','margin_short','smc')
        quote = {'bid':104.99,'ask':105,'asof_ms':NOW}
        reading = {'signal':{'label':'BUY','signal_end':NOW//H4*H4-1},'expires_ms':NOW+H4}
        def seed(doc):
            for p in doc['positions']:
                p['nn_guidance']=nn_advice(p,reading,quote,NOW)
        self.positions.transaction(seed)
        self.runtime.market['BTC'] = {'quote':quote}
        with patch('time.time',return_value=NOW/1000):
            html = self.client.get('/positions').text
        self.assertEqual(html.count('data-position-signal'),9)
        self.assertIn('signal-stop-loss is-selected',html)
        self.assertIn('STOP LOSS · BUY TO COVER SHORT',html)
        self.assertTrue(all(x['status'] == 'open' for x in self.positions.snapshot()['positions']))
        self.assertFalse(self.positions.snapshot()['outbox'])


class NeuralPositionTests(unittest.TestCase):
    def test_signal_actions_target_override_and_stale_quotes(self):
        p = dict(side='long', target_mode='nn', nn_target_mode='smc',take_profit={'price':110})
        quote = {'bid':105,'ask':105.01,'asof_ms':NOW}
        reading = {'signal':{'label':'HOLD','signal_end':NOW-1000},'expires_ms':NOW+H4}
        self.assertEqual(combined_advice(p,reading,quote,NOW)['action'],'HOLD')
        quote.update(bid=111,ask=111.01)
        advice = combined_advice(p,reading,quote,NOW)
        self.assertEqual((advice['action'],advice['target'],advice['signal']),('SELL',110,'HOLD'))
        self.assertEqual(combined_advice(p,reading,quote,NOW+31000)['action'],'WAIT')
        p.update(side='short',take_profit={'price':90})
        quote.update(bid=88,ask=89,asof_ms=NOW)
        self.assertEqual(combined_advice(p,reading,quote,NOW)['action'],'BUY')
        quote.update(bid=104,ask=105)
        reading['signal']['label']='BUY'
        self.assertEqual(combined_advice(p,reading,quote,NOW)['action_label'],'STOP LOSS · BUY TO COVER SHORT')

    def test_adverse_signal_is_stop_loss_for_both_sides_and_target_takes_priority(self):
        for side, adverse, action, direction, target, target_quote in [
                ('long','SELL','SELL','SELL TO EXIT LONG',110,{'bid':110,'ask':111,'asof_ms':NOW}),
                ('short','BUY','BUY','BUY TO COVER SHORT',90,{'bid':89,'ask':90,'asof_ms':NOW})]:
            p = dict(side=side,target_mode='nn',nn_target_mode='smc',take_profit={'price':target})
            for label in ('BUY','HOLD','SELL'):
                with self.subTest(side=side,label=label):
                    reading = {'signal':{'label':label,'signal_end':NOW-1000},'expires_ms':NOW+H4}
                    advice = combined_advice(p,reading,{'bid':100,'ask':101,'asof_ms':NOW},NOW)
                    self.assertEqual(advice['action'],action if label == adverse else 'HOLD')
                    self.assertEqual(advice['action_label'],'STOP LOSS · '+direction if label == adverse else 'HOLD')
                    hit = combined_advice(p,reading,target_quote,NOW)
                    self.assertEqual(hit['action'],action)
                    self.assertEqual(hit['action_label'],'TAKE PROFIT · '+direction)
                    self.assertTrue(hit['target_hit'])
            p['take_profit'] = None
            reading['signal']['label'] = adverse
            self.assertEqual(combined_advice(p,reading,{'bid':100,'ask':101,'asof_ms':NOW},NOW)['action_label'],
                             'STOP LOSS · '+direction)

    def test_stop_loss_requires_current_nn_and_quote_but_tp_only_needs_quote(self):
        p = dict(side='long',target_mode='nn',nn_target_mode='smc',take_profit={'price':110})
        quote = {'bid':100,'ask':101,'asof_ms':NOW}
        reading = {'signal':{'label':'SELL','signal_end':NOW-1000},'expires_ms':NOW+H4}
        for invalid in ({**reading,'expires_ms':NOW}, {**reading,'error':'Model unavailable'},
                        {'signal':None,'expires_ms':NOW+H4}):
            with self.subTest(reading=invalid):
                self.assertEqual(combined_advice(p,invalid,quote,NOW)['action_label'],'WAIT')
                self.assertEqual(combined_advice(p,invalid,{**quote,'bid':110,'ask':111},NOW)['action_label'],
                                 'TAKE PROFIT · SELL TO EXIT LONG')
        for invalid_quote in (None,{**quote,'asof_ms':NOW-30001}):
            self.assertEqual(combined_advice(p,reading,invalid_quote,NOW)['action_label'],'WAIT')

    def test_reader_never_uses_old_candles_or_requires_active_paper_nn(self):
        model = FakeModel()
        with patch('adaptive_crypto.neural.NeuralModel', return_value=model):
            reader = PositionNeuralReader()
            self.assertIsNone(reader.read(bars(),'BTC',Rules(),None,NOW)['error'])
            self.assertIsNotNone(reader.read(bars(),'BTC',Rules(),None,NOW+H4)['error'])
            self.assertEqual(model.calls,1)

    def test_closed_and_inactive_paper_models_are_kept_in_records(self):
        runtime = Mock()
        runtime.rules = Rules(strategy_model='neural_network')
        runtime.positions.snapshot.return_value = {}
        runtime.assets = ASSETS
        def document(status, identity):
            return {'assets':{'BTC':{'trades':[{'id':identity,'status':status,'opened_ms':1}]}}}
        runtime.store._backend.database.documents.return_value = {'smc':document('target','closed-smc'),'legacy':document('active','live-legacy')}
        runtime.store.snapshot.return_value = document('sold','closed-nn')
        result = paper_records(runtime)
        self.assertEqual({t['id'] for t in result['closed']},{'closed-smc','closed-nn'})
        self.assertEqual(result['active'][0]['id'],'live-legacy')
        self.assertFalse(result['active'][0]['model_active'])


if __name__ == '__main__':
    unittest.main()
