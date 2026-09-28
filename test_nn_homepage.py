"""NN market panels use display feeds without changing positions or decisions."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from adaptive_crypto.core import H4, Rules
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.web import create_app
from test_display_formatting import DashboardHTML
from test_gex import chain, NOW
from test_neural_strategy import FakeModel, bars

ASSETS = {name:{'symbol':name+'/USD','price_decimals':digits}
          for name,digits in [('BTC',2),('ETH',2),('SOL',3),('DOGE',5)]}


class NeuralHomepageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.owner = open_stores(Path(self.temp.name)/'state.json',ASSETS,Rules(strategy_model='neural_network'),'sqlite')
        self.store,self.positions = self.owner.__enter__()
        self.addCleanup(self.owner.__exit__,None,None,None)
        with patch('adaptive_crypto.neural_engine.NeuralModel',return_value=FakeModel('HOLD')):
            self.runtime = DashboardRuntime(ASSETS,Rules(strategy_model='neural_network'),self.store,position_store=self.positions)
        self.chart_provider = Mock()
        self.chart_provider.now.return_value = NOW
        self.chart_provider.get.side_effect = lambda endpoint,**params: {
            params['pair']:[[((NOW//H4-15+i)*H4)//1000,'100','105','99','102','101','10',2] for i in range(16)],'last':0}
        self.options_provider = Mock(return_value=chain())
        self.client = create_app(self.runtime,chart_provider=self.chart_provider,gex_provider=self.options_provider).test_client()

    def test_all_configured_assets_have_both_graphs_and_candles_before_first_scan(self):
        html = self.client.get('/').text
        parsed = DashboardHTML(html)
        self.assertFalse(parsed.errors)
        for asset in ASSETS:
            self.assertIn(f'aria-label="{asset} live 4H candlestick chart"',html)
            self.assertIn(f'aria-label="{asset} Naive GEX and SMC map"',html)
        self.assertEqual(html.count('class="gex-map"'),len(ASSETS))
        self.assertEqual(html.count('class="gex-curve"'),len(ASSETS))
        self.assertEqual(html.count('class="gex-curve-panel"'),len(ASSETS))
        self.assertIn('gex-poi-table',html)
        self.assertNotIn('data-panel="gex:',html)
        self.assertIn('/static/gex.js',html)
        self.assertIn('Waiting for price',parsed.text)
        for state in ('buy','hold','sell'):
            self.assertEqual(html.count(f'class="signal-light signal-{state}"'),len(ASSETS))
        self.assertNotIn('STOP LOSS',parsed.text)
        self.assertNotIn('TAKE PROFIT',parsed.text)
        self.options_provider.assert_not_called()

    def test_quote_age_is_distinct_from_unchanged_nn_classification(self):
        signal = FakeModel('HOLD').predict(bars(now=NOW),'BTC')
        self.runtime.data['BTC'] = {'symbol':'BTC/USD','asof_ms':NOW,
            'quote':{'last':65432.19,'bid':65432,'ask':65433,'asof_ms':NOW},
            'errors':{},'neural':{'signal':signal,'status':'WAITING'}}
        self.runtime.market['BTC'] = {'asof_ms':NOW,'quote':self.runtime.data['BTC']['quote'],
            'nn':{'signal':signal,'expires_ms':NOW+H4}}
        with patch('time.time',return_value=NOW/1000):
            html = self.client.get('/').text
        self.assertIn('$65,432.19',html)
        self.assertIn('Live price · USD',html)
        self.assertIn('36 input measurements',html)
        with patch('time.time',return_value=(NOW+45001)/1000):
            stale = self.client.get('/').text
        self.assertIn('$65,432.19',stale)
        self.assertNotIn('Live price · USD',stale)
        self.assertIn('Last price · USD',stale)
        self.assertEqual(self.runtime.data['BTC']['neural']['signal'],signal)

    def test_nn_chart_and_gex_endpoints_preserve_trading_state(self):
        self.runtime.data['BTC'] = {'asof_ms':NOW,'quote':{'last':100,'bid':99.9,'ask':100.1,'asof_ms':NOW},'errors':{}}
        before = (self.store.snapshot(),self.positions.snapshot(),copy.deepcopy(self.runtime.engine.cache))
        with patch('time.time',return_value=NOW/1000):
            chart = self.client.get('/api/chart/BTC')
            self.assertEqual(chart.status_code,200)
            self.assertEqual(chart.json['interval_ms'],H4)
            self.assertTrue(chart.json['candles'][-1]['current'])
            gamma = self.client.get('/api/gex/BTC')
            self.assertEqual(gamma.status_code,200)
            self.assertTrue(gamma.json['strikes'])
            self.assertTrue(gamma.json['curve'])
            self.assertEqual(gamma.json['zones'],[])
            self.assertEqual(self.client.get('/api/gex/DOGE').json['status'],'unsupported')
        self.options_provider.assert_called_once_with('BTC','BTC')
        self.assertEqual((self.store.snapshot(),self.positions.snapshot(),self.runtime.engine.cache),before)

    def test_nn_display_keeps_last_trade_and_chart_quote_fallback(self):
        now = NOW
        observed = {'bid':100,'ask':100.01,'last':100.005,'asof_ms':now}
        original = copy.deepcopy(observed)
        self.runtime.provider = Mock()
        self.runtime.provider.now.return_value = now
        self.runtime.provider.quotes.return_value = {cfg['symbol']:copy.deepcopy(observed) for cfg in ASSETS.values()}
        self.runtime.provider.candles.side_effect = lambda symbol,interval,stamp: bars(now,160,interval)
        self.chart_provider.get.side_effect = RuntimeError('OHLC unavailable')
        with patch('time.time',return_value=now/1000):
            self.runtime.scan_once()
            self.assertEqual(self.runtime.data['BTC']['quote'],observed)
            self.assertEqual(self.runtime.chart_fallback['BTC']['quote'],observed)
            response = self.client.get('/api/chart/BTC')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['degraded'])
        self.assertTrue(response.json['candles'][-1]['current'])
        self.assertEqual(response.json['candles'][-1]['c'],observed['last'])
        self.assertEqual(observed,original)
        self.assertFalse(self.store.snapshot()['assets']['BTC']['trades'])

    def test_invalid_last_trade_does_not_change_executable_quote_or_nn_action(self):
        for last in [None,0,True,float('nan')]:
            row = self.runtime.engine.evaluate('BTC',{'bid':100,'ask':100.01,'last':last,'asof_ms':NOW},
                bars(NOW),bars(NOW,160,300000),NOW)
            self.assertNotIn('last',row['quote'])
            self.assertEqual((row['quote']['bid'],row['quote']['ask']),(100,100.01))
            self.assertEqual(row['neural']['signal']['label'],'HOLD')


if __name__ == '__main__':
    unittest.main()
