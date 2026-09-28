"""Diagnostics inspect cached observations and never initiate market requests."""
import copy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from adaptive_crypto.core import H4, Rules
from adaptive_crypto.feed_diagnostics import build_feed_status, record_feed_observation
from adaptive_crypto.gex import NaiveGEX
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.smc_ledger import SMCStore
from adaptive_crypto.web import create_app

NOW = 1790117500000
ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


def market(now=NOW):
    return {"asof_ms": now, "quote": {"bid": 99, "ask": 100, "asof_ms": now},
            "nn": {"signal": {"signal_end": now//H4*H4-1}, "expires_ms": (now//H4+1)*H4, "error": None},
            "smc_long": {"status": "WAITING FOR SETUP"},
            "smc_short": {"status": "WAITING FOR SETUP"}, "error": None}


def runtime():
    return SimpleNamespace(assets=copy.deepcopy(ASSETS), lock=threading.RLock(),
                           market={}, data={}, feed_observations={}, refresh=15,
                           updated_ms=None, error=None, paused=False,
                           gex=NaiveGEX(ASSETS, provider=Mock()), provider=Mock())


def channels(result):
    return {row["key"]: row for row in result["assets"][0]["feeds"]}


class FeedObservationTests(unittest.TestCase):
    def test_unknown_does_not_contact_providers_or_change_runtime(self):
        app = runtime()
        before = copy.deepcopy(app.feed_observations)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
            rows = channels(build_feed_status(app, NOW))
        self.assertTrue(all(row['status'] == 'unknown' for row in rows.values()))
        self.assertTrue(all(row['last_success_ms'] is None for row in rows.values()))
        self.assertEqual(app.feed_observations, before)
        app.provider.assert_not_called()
        app.gex.provider.assert_not_called()

    def test_waiting_for_setup_is_healthy_analysis_not_a_feed_failure(self):
        app = runtime()
        app.market['BTC'] = market()
        record_feed_observation(app, 'BTC', app.market['BTC'], {}, NOW)
        rows = channels(build_feed_status(app, NOW))
        for key in ('kraken', 'nn', 'smc'):
            self.assertEqual(rows[key]['status'], 'ready')
            self.assertEqual(rows[key]['last_success_ms'], NOW)
            self.assertIsNone(rows[key]['next_eligible_ms'])
        self.assertEqual(rows['nn']['asof_ms'], NOW//H4*H4-1)
        self.assertEqual(rows['kraken']['valid_until_ms'], NOW+30000)

    def test_concrete_failure_keeps_success_timestamp_and_recovers(self):
        app = runtime()
        record_feed_observation(app, 'BTC', market(), {}, NOW)
        failed = market(NOW+1000)
        failed.update(quote=None, error='Missing 15m candles')
        failed['nn'].update(error='Model file missing', signal=None)
        record_feed_observation(app, 'BTC', failed, {'quote': 'Kraken Ticker: EService:Unavailable'}, NOW+1000)
        rows = channels(build_feed_status(app, NOW+1000))
        self.assertIn('EService:Unavailable', rows['kraken']['error'])
        self.assertEqual(rows['nn']['error'], 'Model file missing')
        self.assertEqual(rows['smc']['error'], 'Missing 15m candles')
        for key in ('kraken', 'nn', 'smc'):
            self.assertEqual(rows[key]['status'], 'unavailable')
            self.assertEqual(rows[key]['last_success_ms'], NOW)
            self.assertEqual(rows[key]['last_failure_ms'], NOW+1000)
        record_feed_observation(app, 'BTC', market(NOW+2000), {}, NOW+2000)
        recovered = channels(build_feed_status(app, NOW+2000))
        for key in ('kraken', 'nn', 'smc'):
            self.assertEqual(recovered[key]['status'], 'ready')
            self.assertIsNone(recovered[key]['error'])
            self.assertEqual(recovered[key]['last_success_ms'], NOW+2000)
            self.assertEqual(recovered[key]['last_failure_ms'], NOW+1000)

    def test_elapsed_observations_become_stale_without_new_scan_or_mutation(self):
        app = runtime()
        record_feed_observation(app, 'BTC', market(), {}, NOW)
        saved = copy.deepcopy(app.feed_observations)
        first = channels(build_feed_status(app, NOW+30000))
        self.assertEqual(first['kraken']['status'], 'stale')
        self.assertEqual(first['smc']['status'], 'stale')
        self.assertEqual(first['nn']['status'], 'ready')
        later = channels(build_feed_status(app, NOW+60000))
        self.assertEqual(later['nn']['status'], 'stale')
        self.assertEqual(app.feed_observations, saved)

    def test_nn_signal_expiry_overrides_recent_scanner_timestamp(self):
        app = runtime()
        row = market()
        row['nn']['expires_ms'] = NOW+5000
        record_feed_observation(app, 'BTC', row, {}, NOW)
        self.assertEqual(channels(build_feed_status(app, NOW+5000))['nn']['status'], 'stale')

    def test_future_observation_is_not_current(self):
        app = runtime()
        record_feed_observation(app, 'BTC', market(NOW+3000), {}, NOW+3000)
        self.assertEqual(channels(build_feed_status(app, NOW))['nn']['status'], 'stale')

    def test_fallback_reports_missing_success_as_unknown_instead_of_now(self):
        app = runtime()
        app.market['BTC'] = {**market(), 'quote': None}
        rows = channels(build_feed_status(app, NOW))
        self.assertIsNone(rows['kraken']['last_success_ms'])
        self.assertEqual(rows['kraken']['status'], 'unavailable')


class GEXObservationTests(unittest.TestCase):
    def setUp(self):
        self.provider = Mock(return_value=([], [], 100))
        self.feed = NaiveGEX(ASSETS, self.provider, ttl=120)
        self.wall = patch('adaptive_crypto.gex.time.time', return_value=NOW/1000).start()
        self.mono = patch('adaptive_crypto.gex.time.monotonic', return_value=100).start()
        self.profile = patch('adaptive_crypto.gex.build_profile', side_effect=lambda *args: {
            'status': 'ready', 'stale': False, 'error': None, 'asof_ms': NOW, 'expires_ms': NOW+3600000}).start()
        self.addCleanup(patch.stopall)

    def test_retry_eligibility_and_error_recovery_keep_independent_timestamps(self):
        self.feed.get('BTC')
        first = self.feed.diagnostics(NOW)['BTC']
        self.assertEqual(first['last_attempt_ms'], NOW)
        self.assertEqual(first['last_success_ms'], NOW)
        self.assertEqual(first['next_eligible_ms'], NOW+120000)
        self.wall.return_value, self.mono.return_value = (NOW+121000)/1000, 221
        self.provider.side_effect = RuntimeError('Deribit timed out')
        self.feed.get('BTC')
        failed = self.feed.diagnostics(NOW+121000)['BTC']
        self.assertEqual(failed['status'], 'stale')
        self.assertIn('Deribit timed out', failed['error'])
        self.assertEqual(failed['last_success_ms'], NOW)
        self.assertEqual(failed['last_failure_ms'], NOW+121000)
        self.assertEqual(failed['next_eligible_ms'], NOW+241000)
        self.wall.return_value, self.mono.return_value = (NOW+242000)/1000, 342
        self.provider.side_effect = None
        self.feed.get('BTC')
        recovered = self.feed.diagnostics(NOW+242000)['BTC']
        self.assertEqual(recovered['status'], 'ready')
        self.assertIsNone(recovered['error'])
        self.assertEqual(recovered['last_success_ms'], NOW+242000)
        self.assertEqual(recovered['last_failure_ms'], NOW+121000)

    def test_cached_get_does_not_fabricate_a_new_success_time(self):
        self.feed.get('BTC')
        self.wall.return_value = (NOW+60000)/1000
        self.mono.return_value = 160
        self.feed.get('BTC')
        self.assertEqual(self.feed.diagnostics(NOW+60000)['BTC']['last_success_ms'], NOW)
        self.provider.assert_called_once()

    def test_cached_background_read_does_not_delay_http_cache_eligibility(self):
        self.feed.get('BTC')
        self.feed.next_refresh['BTC'] = 330
        self.mono.return_value = 221
        row = self.feed.diagnostics(NOW+121000)['BTC']
        self.assertEqual(row['next_eligible_ms'], NOW+121000)
        self.assertEqual(row['next_scan_eligible_ms'], NOW+230000)

    def test_diagnostics_ages_published_cache_without_fetching(self):
        self.feed.get('BTC')
        before = copy.deepcopy(self.feed.published)
        with patch.object(self.feed, 'get', side_effect=AssertionError('Must not fetch')):
            row = self.feed.diagnostics(NOW+300000)['BTC']
        self.assertEqual(row['status'], 'stale')
        self.assertEqual(self.feed.published, before)
        self.provider.assert_called_once()

    def test_diagnostics_does_not_wait_for_inflight_provider(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(5)
            return [], [], 100
        self.provider.side_effect = slow
        worker = threading.Thread(target=self.feed.get, args=('BTC',), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            start = time.perf_counter()
            row = self.feed.diagnostics(NOW)['BTC']
            self.assertLess(time.perf_counter()-start, .3)
            self.assertTrue(row['refreshing'])
            self.assertEqual(row['status'], 'unknown')
            self.assertIsNone(row['next_eligible_ms'])
        finally:
            release.set()
            worker.join(2)

    def test_unsupported_pair_never_claims_retry_or_fetches(self):
        feed = NaiveGEX({'DOGE': {'symbol': 'DOGE/USD'}}, self.provider)
        row = feed.diagnostics(NOW)['DOGE']
        self.assertEqual(row['status'], 'unsupported')
        self.assertIsNone(row['next_eligible_ms'])
        self.provider.assert_not_called()


class FeedRouteTests(unittest.TestCase):
    def test_route_is_read_only_and_provider_free_with_no_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            rules = Rules(strategy_model='smc_video')
            store = SMCStore(Path(temp)/'paper.json', ASSETS, rules)
            app = DashboardRuntime(ASSETS, rules, store, provider=Mock(), gex_provider=Mock())
            before = store.snapshot(), app.positions.snapshot()
            with patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
                response = create_app(app).test_client().get('/api/feeds')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(len(response.json['assets'][0]['feeds']), 4)
            self.assertEqual(before, (store.snapshot(), app.positions.snapshot()))
            app.provider.assert_not_called()
            app.gex.provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
