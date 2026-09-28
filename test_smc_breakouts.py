"""Breakout execution and the actual 11 September BTC spike, entirely offline."""
import json
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_crypto.core import Candle, DataError, Rules
from adaptive_crypto.smc import higher_setups, scan
from adaptive_crypto.smc_breakouts import breakout_entries
from adaptive_crypto.smc_engine import SMCEngine
from adaptive_crypto.smc_ledger import SMCStore, close_trade, validate
from test_smc_formulas import high_fixture, mirror

SIGNAL_END = 1789130099999  # 12:34:59.999 UTC / 13:34:59.999 BST
NOW = SIGNAL_END+1001
ASSETS = {a: {"symbol": a+"/USD", "price_decimals": 2} for a in ("BTC", "ETH", "SOL")}


class BreakoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raw = json.loads((Path(__file__).parent/'test_data/btc_spike_20260911.json').read_text())
        self.all_high = [Candle(**b) for b in raw['high']]
        self.all_low = [Candle(**b) for b in raw['low']]
        self.high = [b for b in self.all_high if b.end < NOW]
        self.low = [b for b in self.all_low if b.end < NOW]
        self.rules = Rules(strategy_model="smc_video", smc_breakout_entry=True,
                           smc_pivot_strength=4, smc_stop_buffer_bps=40,
                           smc_tp_sweep_buffer_bps=10, fee_rate=.002,
                           slippage_rate=.0005, max_allocation=.75, max_spread_bps=10)
        self.quote = {"bid": 77108.0, "ask": 77108.1, "asof_ms": NOW}
        self.count = 0
        offline = patch('requests.sessions.Session.request', side_effect=AssertionError('Offline tests'))
        offline.start()
        self.addCleanup(offline.stop)

    def make_engine(self, rules=None, asset='BTC'):
        self.count += 1
        rules = rules or self.rules
        store = SMCStore(Path(self.tmp.name)/f'{self.count}.json', {asset: ASSETS[asset]}, rules)
        return SMCEngine({asset: ASSETS[asset]}, rules, store), store

    def evaluate(self, engine, quote=None, now=NOW, low=None, high=None, errors=None, asset='BTC'):
        return engine.evaluate(asset, self.quote if quote is None else quote,
                               self.high if high is None else high,
                               self.low if low is None else low, now, errors)

    def test_same_candle_sweep_and_bos_are_recognized_in_both_directions(self):
        high = high_fixture()[:7]
        high[-1] = replace(high[-1], h=116, c=115)
        rules = replace(self.rules, smc_pivot_strength=1)
        for side, candles in [('long', high), ('short', mirror(high))]:
            with self.subTest(side=side):
                self.assertEqual(higher_setups(candles[:-1], rules, side), [])
                setups = higher_setups(candles, rules, side)
                self.assertTrue(setups)
                self.assertEqual(setups[-1]['sweep_ms'], setups[-1]['bos_ms'])

    def test_spike_fills_after_5m_confirmation_at_quote_plus_slippage(self):
        engine, store = self.make_engine()
        result = self.evaluate(engine)
        self.assertEqual(result['smc_long']['status'], 'ACTIVE PAPER TRADE')
        record = store.snapshot()['assets']['BTC']
        self.assertIsNone(record['pending'])
        trade, = record['trades']
        self.assertEqual(trade['method'], 'breakout')
        self.assertAlmostEqual(trade['entry'], 77146.65405)
        self.assertEqual(trade['signal_end'], SIGNAL_END)
        self.assertEqual(trade['opened_ms'], NOW)
        self.assertGreater(trade['signal_end'], self.high[-1].end)
        self.assertEqual(trade['stop'], 75696)
        self.assertAlmostEqual(trade['initial_risk_usd'], 5)
        telegram = [e for e in store.snapshot()['outbox'] if e['kind']=='telegram']
        self.assertEqual(len(telegram), 1)
        self.assertIn('breakout filled', telegram[0]['text'])
        validate(store.snapshot())

    def test_disabled_breakout_still_requires_the_pullback(self):
        rules = replace(self.rules, smc_breakout_entry=False)
        engine, store = self.make_engine(rules)
        self.evaluate(engine)
        self.assertEqual(store.snapshot()['assets']['BTC']['trades'], [])
        self.assertEqual(breakout_entries(self.high, self.low, rules, 'long'), [])

    def test_no_entry_before_confirmation_or_from_a_forming_candle(self):
        self.assertEqual(breakout_entries(self.high, self.low[:-1], self.rules, 'long'), [])
        engine, store = self.make_engine()
        forming = replace(self.low[-1], t=NOW//300000*300000)
        result = self.evaluate(engine, low=self.low+[forming])
        self.assertIn('5m', result['errors'])
        self.assertEqual(store.snapshot()['assets']['BTC']['trades'], [])

    def test_execution_rejects_old_quotes_old_signals_chasing_and_lost_breakout(self):
        cases = [({**self.quote, 'asof_ms': SIGNAL_END-1000}, NOW, 'confirmation'),
                 ({**self.quote, 'asof_ms': NOW+61000}, NOW+61000, 'expired'),
                 ({**self.quote, 'bid':77400, 'ask':77400.1}, NOW, 'chase'),
                 ({**self.quote, 'bid':77082, 'ask':77082.1}, NOW, 'invalidated'),
                 ({**self.quote, 'bid':77090, 'ask':77200}, NOW, 'Spread')]
        for quote, now, message in cases:
            with self.subTest(message=message):
                engine, store = self.make_engine()
                result = self.evaluate(engine, quote, now)
                self.assertIn(message, result['smc_long']['entry_error'])
                self.assertEqual(store.snapshot()['assets']['BTC']['trades'], [])
                self.assertIsNone(store.snapshot()['assets']['BTC']['pending'])

    def test_rejected_chase_cannot_reenter_on_price_recovery(self):
        engine, store = self.make_engine()
        self.evaluate(engine, {**self.quote, 'bid':77400, 'ask':77400.1})
        self.evaluate(engine)
        self.assertEqual(store.snapshot()['assets']['BTC']['trades'], [])

    def test_costs_capacity_and_clock_still_gate_execution(self):
        for rules, errors, expected in [(replace(self.rules, fee_rate=.02), None, 'costs'),
                                         (replace(self.rules, paper_floor=999.999), None, 'risk budget'),
                                         (self.rules, {'clock':'unverified'}, None)]:
            with self.subTest(expected=expected):
                engine, store = self.make_engine(rules)
                result = self.evaluate(engine, errors=errors)
                if expected:
                    self.assertIn(expected, result['smc_long']['entry_error'])
                self.assertEqual(store.snapshot()['assets']['BTC']['trades'], [])

    def test_filled_breakout_survives_restart_and_does_not_repeat(self):
        engine, store = self.make_engine()
        self.evaluate(engine)
        original = store.snapshot()['assets']['BTC']['trades'][0]
        restarted = SMCStore(Path(self.tmp.name)/'1.json', {'BTC':ASSETS['BTC']}, self.rules)
        again = SMCEngine({'BTC':ASSETS['BTC']}, self.rules, restarted)
        self.evaluate(again)
        self.assertEqual(len(restarted.snapshot()['assets']['BTC']['trades']), 1)
        def close(document):
            trade = document['assets']['BTC']['trades'][0]
            close_trade(document, 'BTC', trade, trade['target'], 'target', NOW)
        restarted.transaction(close)
        self.evaluate(again)
        self.assertEqual(len(restarted.snapshot()['assets']['BTC']['trades']), 1)
        # Later HTF sweep/BOS cannot buy the same event again after an exit.
        later = SIGNAL_END+25*60000+1001
        hi = [b for b in self.all_high if b.end<later]
        lo = [b for b in self.all_low if b.end<later]
        result = scan(hi, lo, self.rules, 'long', restarted.snapshot()['assets']['BTC']['consumed'])
        self.assertEqual(result['entries'], [])
        self.assertEqual(original['opened_ms'], NOW)

    def test_later_candles_do_not_refresh_the_breakout_signal(self):
        low = [b for b in self.all_low if b.end < NOW+300000]
        self.assertEqual(breakout_entries(self.high, low, self.rules, 'long'), [])

    def test_identical_formula_for_every_asset(self):
        plans = []
        for asset in ASSETS:
            engine, store = self.make_engine(asset=asset)
            self.evaluate(engine, asset=asset)
            trade, = store.snapshot()['assets'][asset]['trades']
            plans.append(tuple(trade[k] for k in ('entry','stop','target','quantity','initial_risk_usd')))
        self.assertTrue(all(p == plans[0] for p in plans))

    def test_mirrored_margin_breakout_and_spot_short_exclusion(self):
        hi, lo = mirror(self.high, axis=160000), mirror(self.low, axis=160000)
        self.assertEqual(breakout_entries(hi, lo, self.rules, 'short'), [])
        engine, store = self.make_engine(replace(self.rules, market_mode='margin'))
        quote = {'bid':160000-self.quote['ask'], 'ask':160000-self.quote['bid'], 'asof_ms':NOW}
        self.evaluate(engine, quote=quote, high=hi, low=lo)
        trade, = store.snapshot()['assets']['BTC']['trades']
        self.assertEqual(trade['side'], 'short')
        self.assertAlmostEqual(trade['entry'], quote['bid']*(1-self.rules.slippage_rate))

    def test_settings_validation(self):
        for changes in [{'smc_breakout_entry':1}, {'smc_breakout_window_bars':0},
                        {'smc_breakout_max_chase_bps':0}, {'smc_breakout_max_chase_bps':101}]:
            with self.subTest(changes=changes), self.assertRaises(DataError):
                replace(self.rules, **changes).validate()

    def test_saved_market_entry_requires_its_original_execution_evidence(self):
        engine, store = self.make_engine()
        self.evaluate(engine)
        for changes in [{'entry_quote':1}, {'entry_quote_ms':SIGNAL_END}, {'max_chase_bps':101},
                        {'opened_ms':NOW+61000,'last_bar_end':NOW+61000}]:
            with self.subTest(changes=changes):
                document = copy.deepcopy(store.snapshot())
                document['assets']['BTC']['trades'][0].update(changes)
                with self.assertRaises(DataError):
                    validate(document)


if __name__ == '__main__':
    unittest.main()
