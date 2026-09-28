"""Position-specific actions, executable-side prices, and delivery freshness."""
import copy
from dataclasses import replace
from unittest.mock import Mock, patch

from adaptive_crypto.notifications import dispatch_once, telegram_send
from adaptive_crypto.position_alerts import exit_action, momentum_alert, price, target_reached_alert
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.smc_ledger import SMCStore
from adaptive_crypto.web import create_app
from test_manual_positions import PositionCase, append_price, ASSETS
from test_take_profit_alerts import AlertCase


class MomentumActionTests(PositionCase):
    def test_current_signal_refreshes_price_after_expiry_without_resetting_rate_limit(self):
        self.open(side='short')
        self.monitor()
        self.now = append_price(self.candles, 120)
        self.store.monitor('BTC', 'BTC/USD', self.candles, self.rules, self.now,
                           quote={'bid':119, 'ask':120, 'asof_ms':self.now})
        original = self.store.snapshot()['outbox'][0]
        self.store.transaction(lambda doc: doc['outbox'][0].update(retry_ms=self.now+60000, attempts=1))
        sender = Mock(return_value={'status':'sent'})
        self.assertFalse(dispatch_once(self.store, 'telegram', sender, self.now+31000))
        self.store.monitor('BTC', 'BTC/USD', self.candles, self.rules, self.now+45000,
                           quote={'bid':121, 'ask':122, 'asof_ms':self.now+45000})
        refreshed, = self.store.snapshot()['outbox']
        self.assertEqual(refreshed['id'], original['id'])
        self.assertEqual(refreshed['created_ms'], original['created_ms'])
        self.assertEqual(refreshed['payload']['exit_price'], 122)
        self.assertEqual(refreshed['retry_ms'], self.now+60000)
        self.assertEqual(refreshed['attempts'], 1)
        self.assertFalse(dispatch_once(self.store, 'telegram', sender, self.now+45000))
        self.assertTrue(dispatch_once(self.store, 'telegram', sender, self.now+60000))
        self.store.monitor('BTC', 'BTC/USD', self.candles, self.rules, self.now+65000)
        self.assertEqual(self.store.snapshot()['outbox'][0]['status'], 'sent')
        self.assertEqual(sender.call_count, 1)

    def test_restart_requires_revalidation_and_feed_failure_cancels_current_signal(self):
        self.open(side='short')
        self.monitor()
        self.swing(120)
        self.store = PositionStore(self.path, market_mode="margin")
        sender = Mock()
        self.assertFalse(dispatch_once(self.store, 'telegram', sender, self.now))
        self.monitor(error='5M clock unavailable')
        self.assertEqual(self.store.snapshot()['outbox'][0]['status'], 'cancelled')
        self.monitor()
        self.assertEqual(self.store.snapshot()['outbox'][0]['status'], 'queued')
        self.monitor(error='Feed unavailable')
        self.assertEqual(self.store.snapshot()['outbox'][0]['status'], 'cancelled')
        sender.assert_not_called()

    def test_formula_change_and_neutral_reading_retire_undelivered_actions(self):
        self.open(side='short')
        self.monitor()
        self.swing(120)
        self.monitor(rules=replace(self.rules, momentum_roc_period=6))
        self.assertEqual(self.store.snapshot()['outbox'][0]['status'], 'cancelled')
        self.monitor()
        self.assertEqual(self.store.snapshot()['outbox'][0]['status'], 'cancelled')
        self.swing(80)
        self.swing(100)
        self.assertEqual(self.store.snapshot()['watches']['BTC']['reading']['direction'], 'neutral')
        self.assertTrue(all(e['status'] == 'cancelled' for e in self.store.snapshot()['outbox']))

    def test_fresh_quote_cannot_extend_signal_beyond_next_candle(self):
        self.open(side='short')
        self.monitor()
        self.swing(120)
        expiry = self.candles[-1].end+self.candles[-1].interval
        now = expiry-1000
        self.store.monitor('BTC', 'BTC/USD', self.candles, self.rules, now,
                           quote={'bid':119, 'ask':120, 'asof_ms':now})
        self.assertEqual(self.store.snapshot()['outbox'][0]['expires_ms'], expiry)
        sender = Mock()
        self.assertFalse(dispatch_once(self.store, 'telegram', sender, expiry))
        sender.assert_not_called()

    def test_long_exit_uses_bid_short_exit_uses_ask_and_each_has_own_entry(self):
        long = self.open(entry=101, quantity=2)
        short = self.open(side='short', entry=103, quantity=3)
        self.monitor()
        self.now = append_price(self.candles, 120)
        quote = {'bid':119.5, 'ask':120.5, 'asof_ms':self.now}
        self.store.monitor('BTC','BTC/USD',self.candles,self.rules,self.now,quote=quote)
        hold, cover = self.store.snapshot()['outbox']
        self.assertEqual(hold['payload']['action'],'hold')
        self.assertIsNone(hold['payload']['exit_price'])
        self.assertEqual(cover['payload']['exit_price'],120.5)
        self.assertEqual(cover['payload']['price_kind'],'live_ask')
        self.assertIn('BUY TO COVER SHORT',cover['title'])
        self.assertIn('Recorded entry price: $103.00',cover['text'])
        self.assertIn('Quantity: 3 BTC',cover['text'])
        self.assertIn(short['id'][:8], cover['title'])
        self.assertNotIn(long['id'][:8], cover['text'])
        self.now = append_price(self.candles, 80)
        self.store.monitor('BTC','BTC/USD',self.candles,self.rules,self.now,
                           quote={'bid':79.5,'ask':80.5,'asof_ms':self.now})
        sale = self.store.snapshot()['outbox'][-2]
        self.assertEqual(sale['payload']['exit_price'],79.5)
        self.assertEqual(sale['payload']['price_kind'],'live_bid')
        self.assertIn('SELL TO EXIT LONG',sale['title'])
        self.assertIn('Recorded entry price: $101.00',sale['text'])
        self.assertEqual(sale['expires_ms'],self.now+30000)
        self.assertEqual(sale['position_ids'],[long['id']])

    def test_bad_quotes_are_never_presented_as_live_exit_prices(self):
        self.open(side='short')
        self.monitor()
        baseline = self.store.snapshot()
        self.now = append_price(self.candles, 120)
        for quote in (None, {'bid':1,'ask':2,'asof_ms':self.now-30001},
                      {'bid':3,'ask':2,'asof_ms':self.now}, {'bid':1,'ask':float('nan'),'asof_ms':self.now}):
            self.store.data = copy.deepcopy(baseline)
            self.store.monitor('BTC','BTC/USD',self.candles,self.rules,self.now,quote=quote)
            event = self.store.snapshot()['outbox'][-1]
            self.assertEqual(event['payload']['exit_price'],120)
            self.assertEqual(event['payload']['price_kind'],'candle_reference')
            self.assertIn('Signal exit reference (completed candle): $120.00',event['text'])
            self.assertIn('Reference price only',event['text'])

    def test_expired_quote_alert_never_reaches_telegram(self):
        self.open(side='short')
        self.monitor()
        self.now = append_price(self.candles,120)
        self.store.monitor('BTC','BTC/USD',self.candles,self.rules,self.now,
                           quote={'bid':119,'ask':120,'asof_ms':self.now})
        sender = Mock()
        self.assertFalse(dispatch_once(self.store,'telegram',sender,self.now+30000))
        sender.assert_not_called()
        self.assertEqual(self.store.snapshot()['outbox'][0]['status'],'cancelled')

    def test_catchup_cancels_historical_signals_and_only_latest_transition_is_deliverable(self):
        self.open(side='short')
        self.monitor()
        append_price(self.candles,120)
        self.now = append_price(self.candles,80)
        self.monitor()
        events = self.store.snapshot()['outbox']
        self.assertEqual([e['status'] for e in events],['cancelled','queued'])
        self.assertEqual(events[0]['payload']['price_kind'],'candle_reference')
        sender = Mock(return_value={'status':'sent'})
        self.assertTrue(dispatch_once(self.store,'telegram',sender,self.now))
        self.assertEqual(sender.call_args.args[0]['action_label'],'HOLD SHORT')

    def test_telegram_message_contains_one_position_action_and_both_prices(self):
        p = self.open(side='short')
        self.monitor()
        self.swing(120)
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {'ok':True,'result':{'message_id':1}}
        with patch('adaptive_crypto.notifications.requests.sessions.Session.request',return_value=response) as post, \
                patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'123:test-token','TELEGRAM_CHAT_ID':'12345'}):
            dispatch_once(self.store,'telegram',telegram_send,self.now)
        body = post.call_args.kwargs['json']['text']
        self.assertIn('BUY TO COVER SHORT',body)
        self.assertIn(p['id'][:8],body)
        self.assertIn('Recorded entry price: $100.00',body)
        self.assertIn('Signal exit reference (completed candle): $120.00',body)


class TargetActionTests(AlertCase):
    def hits(self):
        return [e for e in self.positions.snapshot()['outbox'] if e.get('alert_type')=='target_reached']

    def test_supporting_momentum_does_not_say_hold_at_a_saved_exit_target(self):
        for side in ('long', 'short'):
            self.holding(side)
            self.monitor_holding(self.target, 1000)
            now = self.now+1000
            reading = {'direction':'bullish' if side=='long' else 'bearish', 'close':self.target,
                       'bar_ms':(now//300000-1)*300000, 'end_ms':now//300000*300000-1, 'interval_ms':300000}
            def update(doc):
                momentum_alert(doc, doc['positions'][0], reading, 'neutral', self.quote(self.target,1000),
                               now, 'supporting-signal')
            self.positions.transaction(update)
            self.assertFalse(any(e.get('alert_type')=='position_momentum' for e in self.positions.snapshot()['outbox']))
            self.assertEqual(self.hits()[0]['action_label'], exit_action(side))

    def test_historical_target_with_retraced_live_price_remains_a_review_action(self):
        self.holding('short')
        def update(doc):
            p = doc['positions'][0]
            p['take_profit'].update(state='reached', reached_ms=self.now)
            target_reached_alert(doc, p, self.quote(self.near()), self.now, True)
        self.positions.transaction(update)
        event, = self.hits()
        self.assertEqual(event['action_label'], 'REVIEW EXIT')
        self.assertEqual(event['payload']['action_label'], 'REVIEW EXIT')
        self.assertEqual(event['payload']['action'], 'review')
        self.assertEqual(event['payload']['price_kind'], 'live_ask')

    def test_near_and_hit_have_distinct_actions_and_no_automatic_close(self):
        for side in ('long','short'):
            with self.subTest(side=side):
                self.holding(side)
                self.monitor_holding(self.near(),1000)
                near = self.alerts(self.positions)[0]
                self.assertIn('PREPARE',near['title'])
                self.assertIn(exit_action(side),near['title'])
                self.assertIn('Target not reached yet',near['text'])
                self.assertEqual(near['payload']['entry_price'],self.position['entry'])
                self.monitor_holding(self.target,2000)
                event, = self.hits()
                self.assertIn('TAKE-PROFIT HIT',event['title'])
                self.assertIn(exit_action(side),event['title'])
                self.assertEqual(event['payload']['exit_price'],self.target)
                self.assertEqual(event['payload']['price_kind'],'live_bid' if side=='long' else 'live_ask')
                self.assertIn('Recorded entry price:',event['text'])
                self.assertEqual(self.alerts(self.positions)[0]['status'],'cancelled')
                sender = Mock(return_value={'status':'sent'})
                self.assertTrue(dispatch_once(self.positions,'telegram',sender,self.now+2001))
                self.positions = PositionStore(self.positions.path, market_mode="margin")
                self.monitor_holding(self.target,3000)
                self.assertFalse(dispatch_once(self.positions,'telegram',sender,self.now+3001))
                self.assertEqual(self.positions.snapshot()['positions'][0]['status'],'open')

    def test_jump_past_target_alerts_even_without_advance_warning(self):
        self.holding('long')
        self.monitor_holding(self.target+1,1000)
        self.assertEqual(self.alerts(self.positions),[])
        self.assertEqual(self.hits()[0]['payload']['exit_price'],self.target+1)

    def test_retraced_target_cancels_pending_exit_and_fresh_recross_refreshes_it(self):
        self.holding('long')
        self.monitor_holding(self.target,1000)
        self.monitor_holding(self.near(),2000)
        self.assertEqual(self.hits()[0]['status'],'cancelled')
        self.monitor_holding(self.target,3000)
        self.assertEqual(len(self.hits()),1)
        self.assertEqual(self.hits()[0]['status'],'queued')
        self.positions.close_position(self.position['id'],self.target,self.now+3001)
        self.assertEqual(self.hits()[0]['status'],'cancelled')

    def test_old_reached_target_without_event_does_not_replay_after_upgrade(self):
        self.holding('long')
        self.positions.transaction(lambda doc:doc['positions'][0]['take_profit'].update(state='reached',reached_ms=self.now))
        self.monitor_holding(self.target,1000)
        self.assertEqual(self.hits(),[])

    def test_historical_wick_is_reference_only_not_a_current_exit_quote(self):
        self.holding('long')
        def update(doc):
            p = doc['positions'][0]
            p['take_profit'].update(state='reached',reached_ms=self.now)
            target_reached_alert(doc,p,None,self.now+40000,True)
        self.positions.transaction(update)
        event, = self.hits()
        self.assertIn('TARGET TOUCHED EARLIER',event['title'])
        self.assertEqual(event['action_label'],'REVIEW EXIT')
        self.assertEqual(event['payload']['price_kind'],'target_reference')
        self.assertEqual(event['payload']['action'], 'review')
        self.assertEqual(event['payload']['action_label'], event['action_label'])
        self.assertNotIn('Exit action: SELL', event['text'])
        sender = Mock()
        self.assertFalse(dispatch_once(self.positions,'telegram',sender,self.now+40000))
        sender.assert_not_called()

    def test_target_recross_becomes_latest_alert_and_cancellation_does_not_resurrect_older_one(self):
        self.holding('long')
        self.monitor_holding(self.near(), 1000)
        self.monitor_holding(self.target, 2000)
        self.positions.transaction(lambda doc: doc['outbox'].append({
            **copy.deepcopy(doc['outbox'][0]), 'id':'later-momentum', 'created_ms':self.now+3000,
            'observed_ms':self.now+3000, 'alert_type':'position_momentum', 'status':'sent',
            'action_label':'HOLD LONG'}))
        self.monitor_holding(self.target, 4000)
        store = SMCStore(self.directory/'latest.smc.json', ASSETS, self.rules)
        runtime = DashboardRuntime(ASSETS, self.rules, store, provider=Mock(), position_store=self.positions)
        with patch('time.time', return_value=(self.now+4001)/1000):
            latest = runtime.snapshot()['holdings']['positions'][0]['latest_alert']
        self.assertEqual(latest['alert_type'], 'target_reached')
        self.assertTrue(latest['is_current'])
        self.monitor_holding(self.near(), 5000)
        client = create_app(runtime).test_client()
        with patch('time.time', return_value=(self.now+5001)/1000):
            snapshot = runtime.snapshot()
            page = client.get('/positions').get_data(as_text=True)
        latest = snapshot['holdings']['positions'][0]['latest_alert']
        self.assertEqual(latest['status'], 'cancelled')
        self.assertFalse(latest['is_current'])
        self.assertIn('Cancelled alert · no current instruction', page)

    def test_dashboard_rejects_invalid_and_clock_unverified_marks(self):
        self.holding('long')
        store = SMCStore(self.directory/'marks.smc.json', ASSETS, self.rules)
        runtime = DashboardRuntime(ASSETS, self.rules, store, provider=Mock(), position_store=self.positions)
        for quote, errors in (({'bid':150,'ask':140,'asof_ms':self.now}, {}),
                              ({'bid':140,'ask':141,'asof_ms':self.now}, {'clock':'offline'}),
                              ({'bid':float('nan'),'ask':141,'asof_ms':self.now}, {})):
            runtime.data['BTC'] = {'quote':quote, 'errors':errors}
            with patch('time.time', return_value=self.now/1000):
                p = runtime.snapshot()['holdings']['positions'][0]
            self.assertIsNone(p['mark_price'])
            self.assertNotIn('Live', p['mark_source'])

    def test_rate_limit_retry_deadline_survives_target_refresh(self):
        self.holding('short')
        self.monitor_holding(self.target,1000)
        self.positions.transaction(lambda doc: doc['outbox'][-1].update(retry_ms=self.now+90000))
        self.monitor_holding(self.target,2000)
        self.assertEqual(self.hits()[0]['retry_ms'],self.now+90000)

    def test_paper_entry_and_exit_messages_spell_out_transaction_and_price(self):
        for side in ('long','short'):
            with self.subTest(side=side):
                self.paper(side)
                events = self.store.snapshot()['outbox']
                entry = 'BUY TO OPEN LONG' if side=='long' else 'SELL TO OPEN SHORT'
                self.assertIn(entry,events[0]['text'])
                self.assertIn('Entry limit price:',events[0]['text'])
                filled = next(e for e in events if e['id'].endswith(':filled'))
                self.assertIn(entry,filled['text'])
                self.assertIn('Entry price:',filled['text'])
                self.paper_quote(self.target)
                closed = next(e for e in self.store.snapshot()['outbox'] if e['id'].endswith(':closed'))
                self.assertIn(exit_action(side),closed['text'])
                self.assertIn('Modeled exit price:',closed['text'])

    def test_dashboard_displays_position_identity_action_and_expiry(self):
        self.holding('short')
        self.monitor_holding(self.target,1000)
        store = SMCStore(self.directory/'render.smc.json',ASSETS,self.rules)
        runtime = DashboardRuntime(ASSETS,self.rules,store,provider=Mock(),position_store=self.positions)
        client = create_app(runtime).test_client()
        with patch('time.time',return_value=(self.now+1001)/1000):
            page = client.get('/positions').get_data(as_text=True)
        self.assertIn('BUY TO COVER SHORT',page)
        self.assertIn(self.position['id'][:8],page)
        self.assertIn('Latest signal',page)
        with patch('time.time',return_value=(self.now+32000)/1000):
            page = client.get('/positions').get_data(as_text=True)
        self.assertIn('Past alert · not a current quote',page)
        self.assertEqual(price(.00000000001),'$1e-11')
