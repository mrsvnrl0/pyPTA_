"""Offline numerical, feed-failure and SMC integration checks for Naive GEX."""
import copy
from dataclasses import replace
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.gex import NaiveGEX, build_profile, exposure, map_smc, YEAR_MS
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.smc_ledger import SMCStore
from adaptive_crypto.web import create_app

NOW = 1788793200000
ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


def chain(specs=((110, "call", 30), (90, "put", 30)), base="BTC"):
    instruments, books = [], []
    for i, (strike, kind, oi) in enumerate(specs):
        name = f"{base}-{i}"
        instruments.append({"instrument_name": name, "base_currency": base, "kind": "option",
                            "is_active": True, "expiration_timestamp": NOW + YEAR_MS*.25,
                            "strike": strike, "option_type": kind, "contract_size": 100})
        books.append({"instrument_name": name, "creation_timestamp": NOW,
                      "open_interest": oi, "mark_iv": 50})
    return instruments, books, 100


def profile(specs=((110, "call", 30), (90, "put", 30))):
    instruments, books, spot = chain(specs)
    return build_profile(instruments, books, "BTC", spot, NOW)


class GammaMathTests(unittest.TestCase):
    def test_duplicate_feed_rows_are_rejected_instead_of_double_counting_exposure(self):
        instruments, books, spot = chain()
        with self.assertRaisesRegex(DataError, 'duplicate active instrument'):
            build_profile(instruments+[instruments[0]], books, 'BTC', spot, NOW)
        with self.assertRaisesRegex(DataError, 'duplicate instrument summary'):
            build_profile(instruments, books+[books[0]], 'BTC', spot, NOW)

    def test_gamma_units_and_sign_match_analytic_atm_value(self):
        option = {"strike": 100, "iv": .5, "years": .25, "oi": 30, "sign": 1}
        expected = math.exp(-.125**2/2)/(math.sqrt(2*math.pi)*100*.25)*30*100**2*.01
        self.assertAlmostEqual(exposure(option, 100), expected)
        self.assertAlmostEqual(exposure({**option, "sign": -1}, 100), -expected)

    def test_wall_uses_net_strike_exposure_and_never_double_multiplies_oi(self):
        result = profile(((100, "call", 100), (100, "put", 99), (110, "call", 30), (90, "put", 30)))
        self.assertEqual(result["call_wall"]["strike"], 110)
        self.assertEqual(result["put_wall"]["strike"], 90)
        atm = next(r for r in result["strikes"] if r["strike"] == 100)
        self.assertAlmostEqual(atm["net_gex"], exposure({"strike":100,"iv":.5,"years":.25,"oi":1,"sign":1},100))

    def test_flip_matches_analytic_two_strike_solution(self):
        # Same OI/IV/expiry: equal gamma at geometric mean(K) * exp(-sigma²T/2).
        expected = math.sqrt(90*110)*math.exp(-.5*.5**2*.25)
        result = profile()
        self.assertAlmostEqual(result["gamma_flip"], expected, places=7)
        self.assertEqual(len(result["flip_candidates"]), 1)

    def test_multiple_flips_nearest_to_index_is_selected(self):
        result = profile(((80,"call",30),(100,"put",50),(120,"call",30)))
        self.assertEqual(len(result["flip_candidates"]), 2)
        self.assertEqual(result["gamma_flip"], min(result["flip_candidates"], key=lambda p: abs(p-100)))

    def test_one_sided_and_balanced_chains_do_not_invent_walls_or_flips(self):
        for kind in ("call", "put"):
            result = profile(((100, kind, 30),))
            self.assertIsNone(result["gamma_flip"])
            self.assertIsNone(result["put_wall" if kind == "call" else "call_wall"])
        result = profile(((100,"call",30),(100,"put",30)))
        self.assertEqual(result["regime"], "neutral")
        self.assertIsNone(result["gamma_flip"])
        self.assertIsNone(result["call_wall"])
        self.assertIsNone(result["put_wall"])

    def test_other_assets_expired_and_zero_oi_are_excluded(self):
        instruments, books, spot = chain()
        instruments += [{**instruments[0], "base_currency":"ETH"},
                        {**instruments[0], "expiration_timestamp": NOW}]
        zero, summary, _ = chain(((150,"call",0),))
        zero[0]["instrument_name"] = summary[0]["instrument_name"] = "zero"
        summary[0]["mark_iv"] = None
        result = build_profile(instruments+zero, books+summary, "BTC", spot, NOW)
        self.assertEqual(result["option_count"], 2)
        self.assertEqual(result["eligible_count"], 3)

    def test_incomplete_stale_future_and_invalid_positive_oi_fail_closed(self):
        for field, value in (("mark_iv", None), ("mark_iv", 0), ("mark_iv", float('nan')),
                             ("open_interest", -1), ("open_interest", True),
                             ("creation_timestamp", NOW-300001), ("creation_timestamp", NOW+2001)):
            with self.subTest(field=field, value=value):
                inst, books, spot = chain()
                books[0][field] = value
                with self.assertRaises(DataError):
                    build_profile(inst, books, "BTC", spot, NOW)
        inst, books, spot = chain()
        with self.assertRaises(DataError):
            build_profile(inst, books[:1], "BTC", spot, NOW)
        with self.assertRaises(DataError):
            profile(((100,"call",0),))


class GammaCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('adaptive_crypto.gex.time.time', return_value=NOW/1000).start()
        self.mono = patch('adaptive_crypto.gex.time.monotonic', return_value=0).start()
        self.addCleanup(patch.stopall)
        self.provider = Mock(return_value=chain())
        self.feed = NaiveGEX(ASSETS, self.provider)

    def test_cache_throttles_and_returns_independent_copies(self):
        first = self.feed.get('BTC')
        first['strikes'].clear()
        self.assertEqual(len(self.feed.get('BTC')['strikes']), 2)
        self.provider.assert_called_once_with('BTC','BTC')

    def test_failure_preserves_last_timestamp_retries_and_recovers(self):
        first = self.feed.get('BTC')
        self.mono.return_value = 120
        self.provider.side_effect = RuntimeError('offline')
        result = self.feed.get('BTC')
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(result['asof_ms'], first['asof_ms'])
        self.assertEqual(result['strikes'], first['strikes'])
        self.feed.get('BTC')
        self.assertEqual(self.provider.call_count, 2)
        self.mono.return_value = 240
        self.provider.side_effect = None
        self.assertEqual(self.feed.get('BTC')['status'], 'ready')

    def test_first_outage_never_invents_data(self):
        self.provider.side_effect = RuntimeError('offline')
        result = self.feed.get('BTC')
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['asof_ms'])
        self.assertNotIn('call_wall', result)

    def test_cache_cannot_outlive_expiry_or_freshness(self):
        inst, books, spot = chain()
        inst[0]['expiration_timestamp'] = NOW+1000
        self.provider.return_value = inst, books, spot
        self.feed.get('BTC')
        self.clock.return_value = (NOW+1001)/1000
        self.assertTrue(self.feed.get('BTC')['stale'])
        self.clock.return_value = (NOW+300001)/1000
        self.assertTrue(self.feed.get('BTC')['stale'])

    def test_unknown_and_unsupported_assets_do_not_fetch(self):
        with self.assertRaises(KeyError):
            self.feed.get('ETH')
        feed = NaiveGEX({'DOGE': {'symbol':'DOGE/USD'}}, self.provider)
        self.assertEqual(feed.get('DOGE')['status'], 'unsupported')
        self.provider.assert_not_called()

    def test_sol_uses_usdc_and_filters_other_base_assets(self):
        self.provider.return_value = chain(base='SOL')
        feed = NaiveGEX({'Solana': {'symbol':'SOL/USD'}}, self.provider)
        result = feed.get('Solana')
        self.assertEqual(result['base'], 'SOL')
        self.assertEqual(result['quote_currency'], 'USDC')
        self.provider.assert_called_once_with('SOL','USDC')


class GammaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.row = {'asof_ms':NOW, 'quote':{'last':100,'bid':99.9,'ask':100.1,'volume24_base':100,'asof_ms':NOW}, 'errors':{},
                    'smc_short':{'status':'WAITING','checks':[], 'setup':{'zone_low':110,'zone_high':112,'bos_end':NOW}},
                    'smc_long':{'status':'WAITING','checks':[], 'setup':{'zone_low':89,'zone_high':91,'bos_end':NOW}}}

    def test_exact_boundary_and_inside_overlap_without_mutation(self):
        p, row = profile(), copy.deepcopy(self.row)
        result = map_smc(p, row, NOW)
        self.assertTrue(all(z['confluence'] for z in result['zones']))
        self.assertNotIn('zones', p)
        self.assertEqual(row, self.row)
        row['smc_short']['setup']['zone_low'] = 110.01
        self.assertFalse(map_smc(p,row,NOW)['zones'][0]['confluence'])

    def test_stale_or_failed_smc_never_has_current_confluence(self):
        for change in ({'asof_ms':NOW-45001}, {'errors':{'5m':'offline'}}, {'scan_error':'offline'},
                       {'quote':{'last':100,'asof_ms':NOW-45001}}):
            result = map_smc(profile(), {**self.row, **change}, NOW)
            self.assertEqual(result['zones'], [])
            self.assertFalse(result['smc_fresh'])
            self.assertEqual(result['price'], None if 'quote' in change else 100)
        result = map_smc({**profile(),'status':'stale','stale':True}, self.row, NOW)
        self.assertFalse(any(z['confluence'] for z in result['zones']))

    def test_wall_distances_zone_relationships_and_venue_basis_use_actual_price(self):
        result = map_smc(profile(), self.row, NOW)
        call, flip, put = result['levels']
        self.assertAlmostEqual(call['distance_percent'], 10)
        self.assertEqual(call['relation'], 'above')
        self.assertAlmostEqual(put['distance_percent'], -10)
        self.assertEqual(put['relation'], 'below')
        self.assertEqual(result['basis_percent'], 0)
        self.assertEqual(result['zones'][0]['relation'], 'above')
        self.assertAlmostEqual(result['zones'][1]['distance_percent'], -9)
        row = copy.deepcopy(self.row)
        row['quote'].update(bid=109.9, ask=110.1, last=110)
        result = map_smc(profile(), row, NOW)
        self.assertEqual(result['levels'][0]['relation'], 'at')
        self.assertEqual(result['zones'][0]['relation'], 'inside')
        self.assertEqual(result['zones'][0]['distance_percent'], 0)
        self.assertAlmostEqual(result['basis_percent'], (100/110-1)*100)
        row['quote'].update(bid=114.9, ask=115.1, last=115)
        self.assertEqual(map_smc(profile(), row, NOW)['levels'][0]['relation'], 'below')

    def test_missing_inverted_nonfinite_or_future_quotes_suppress_live_distances(self):
        for change in ({'last':None}, {'last':True}, {'last':float('nan')}, {'bid':102, 'ask':101},
                       {'bid':None}, {'asof_ms':NOW+2001}, {'asof_ms':NOW-45001}):
            row = copy.deepcopy(self.row)
            row['quote'].update(change)
            result = map_smc(profile(), row, NOW)
            self.assertIsNone(result['price'])
            self.assertEqual(result['zones'], [])
            self.assertTrue(all(level['distance_percent'] is None for level in result['levels']))
        row = {**self.row, 'errors':{'clock':'offline'}}
        self.assertIsNone(map_smc(profile(), row, NOW)['price'])

    def test_malformed_or_future_block_does_not_break_the_other_side(self):
        for change in ({'zone_low':120}, {'zone_high':float('nan')}, {'zone_low':0}, {'bos_end':NOW+1}):
            row = copy.deepcopy(self.row)
            row['smc_short']['setup'].update(change)
            result = map_smc(profile(), row, NOW)
            self.assertEqual(len(result['zones']), 1)
            self.assertEqual(result['zones'][0]['side'], 'long')
            self.assertEqual(len(result['zone_errors']), 1)

    def test_expiry_deadlines_and_stale_profiles_never_report_live_wall_confluence(self):
        result = map_smc(profile(), self.row, NOW)
        self.assertEqual(result['options_valid_until_ms'], NOW+300000)
        self.assertEqual(result['price_valid_until_ms'], NOW+45000)
        self.assertEqual(result['smc_valid_until_ms'], NOW+45000)
        for change in ({'expires_ms':NOW}, {'asof_ms':NOW-300001}, {'asof_ms':NOW+2001}, {'stale':True}):
            result = map_smc({**profile(), **change}, self.row, NOW)
            self.assertEqual(result['status'], 'stale')
            self.assertFalse(any(z['confluence'] for z in result['zones']))
            self.assertTrue(all(level['distance_percent'] is None for level in result['levels']))

    def test_missed_retired_expired_and_incomplete_setups_are_reference_only(self):
        for status in ('FIRST VISIT CONFIRMATION MISSED', 'SETUP RETIRED', 'SETUP EXPIRED',
                       'WAITING FOR COMPLETE ENTRY-TIMEFRAME HISTORY'):
            row = copy.deepcopy(self.row)
            row['smc_short']['status'] = status
            result = map_smc(profile(), row, NOW)
            self.assertTrue(result['zones'][0]['reference_only'])
            self.assertFalse(result['zones'][0]['confluence'])

    def test_route_template_isolation_and_outage(self):
        with tempfile.TemporaryDirectory() as temp:
            rules = replace(Rules(), strategy_model='smc_video')
            store = SMCStore(Path(temp)/'paper.json', ASSETS, rules)
            runtime = DashboardRuntime(ASSETS, rules, store, provider=Mock())
            runtime.market['BTC'] = {**self.row,'symbol':'BTC/USD'}
            before = store.snapshot()
            provider = Mock(return_value=chain())
            client = create_app(runtime,gex_provider=provider).test_client()
            with patch('time.time', return_value=NOW/1000):
                response = client.get('/api/gex/BTC')
                self.assertEqual(response.status_code,200)
                self.assertTrue(response.json['zones'][0]['confluence'])
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                self.assertEqual(client.get('/api/gex/UNKNOWN').status_code,404)
                html = client.get('/')
                self.assertEqual(html.status_code,200)
                self.assertIn(b'Naive GEX',html.data)
                self.assertIn(b'/static/gex.js',html.data)
                self.assertIn(b'Gamma across index prices', html.data)
                self.assertIn(b'data-distance="call"', html.data)
            self.assertEqual(store.snapshot(),before)
            provider.side_effect = RuntimeError('offline')
            client = create_app(runtime,gex_provider=provider).test_client()
            self.assertEqual(client.get('/api/gex/BTC').status_code,503)
            self.assertEqual(store.snapshot(),before)


if __name__ == '__main__':
    unittest.main()
