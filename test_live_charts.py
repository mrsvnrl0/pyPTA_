"""Offline chart regressions; no public API, signal workers, or state writes."""
import copy
import time
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import adaptive_crypto_dashboard as d
from adaptive_crypto_dashboard import LiveCharts


ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}
T0 = (1_780_000_000_000 // d.H4) * d.H4


def fixture_rows(count=20):
    return [[(T0 + i * d.H4) // 1000, "100", "104", "99", "102", "101", "10", 2]
            for i in range(count)]


class FakeProvider:
    def __init__(self):
        self.rows = fixture_rows()
        self.observed = T0 + 19 * d.H4 + 60_000
        self.calls = []
        self.clock_calls = 0
        self.error = None

    def get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if self.error:
            raise self.error
        return {"BTC/USD": copy.deepcopy(self.rows), "last": self.rows[-1][0]}

    def now(self):
        self.clock_calls += 1
        return self.observed


class LiveChartTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.monotonic = patch("adaptive_crypto.market_data.time.monotonic", return_value=100).start()
        self.addCleanup(patch.stopall)
        self.charts = LiveCharts(ASSETS, self.provider)

    def test_chart_includes_current_bar_but_signal_parser_excludes_it(self):
        result = self.charts.get("BTC")
        self.assertFalse(result["stale"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["symbol"], "BTC/USD")
        self.assertEqual(result["interval_ms"], d.H4)
        self.assertEqual(result["asof_ms"], self.provider.observed)
        self.assertEqual(len(result["candles"]), 16)
        self.assertEqual(result["candles"][0]["t"], T0 + 4 * d.H4)
        self.assertTrue(result["candles"][-1]["current"])
        self.assertEqual(sum(row["current"] for row in result["candles"]), 1)
        closed = d.parse_kraken_rows(self.provider.rows, d.H4, self.provider.observed)
        self.assertEqual(closed[-1].t, result["candles"][-2]["t"])
        self.assertEqual(self.provider.calls, [("OHLC", {"pair": "BTC/USD", "interval": 240, "assetVersion": 1})])

    def test_chart_accepts_kraken_legacy_internal_pair_key(self):
        self.provider.get = lambda endpoint, **params: {"XXBTZUSD": copy.deepcopy(self.provider.rows), "last": self.provider.rows[-1][0]}
        result = self.charts.get("BTC")
        self.assertFalse(result["stale"])
        self.assertEqual(result["symbol"], "BTC/USD")

    def test_full_history_plus_forming_candle_is_accepted(self):
        self.provider.rows = fixture_rows(count=721)
        self.provider.observed = T0+720*d.H4+60000
        result = self.charts.get('BTC')
        self.assertFalse(result['stale'])
        self.assertEqual(len(result['candles']),16)
        self.assertTrue(result['candles'][-1]['current'])
        self.assertEqual(result['candles'][-1]['t'],T0+720*d.H4)

    def test_chart_uses_local_clock_if_time_request_temporarily_fails(self):
        self.provider.now = lambda: (_ for _ in ()).throw(RuntimeError("time feed offline"))
        with patch("adaptive_crypto.market_data.time.time", return_value=self.provider.observed / 1000):
            result = self.charts.get("BTC")
        self.assertFalse(result["stale"])
        self.assertTrue(result["candles"][-1]["current"])

    def test_ttl_reuses_snapshot_then_refreshes_at_expiry(self):
        original = self.charts.get("BTC")
        self.provider.rows[-1][4] = "103"
        self.monotonic.return_value = 114.9
        self.assertEqual(self.charts.get("BTC"), original)
        self.assertEqual(len(self.provider.calls), 1)


        self.monotonic.return_value = 115
        self.assertEqual(self.charts.get("BTC")["candles"][-1]["c"], 103)
        self.assertEqual(len(self.provider.calls), 2)

    def test_failure_preserves_values_and_timestamp_and_throttles_retry(self):
        original = self.charts.get("BTC")
        self.monotonic.return_value = 115
        self.provider.error = RuntimeError("Chart service temporarily unavailable")
        failed = self.charts.get("BTC")
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["candles"], original["candles"])
        self.assertEqual(failed["asof_ms"], original["asof_ms"])
        self.assertIn("temporarily unavailable", failed["error"])
        self.monotonic.return_value = 129.9
        self.assertEqual(self.charts.get("BTC"), failed)
        self.assertEqual(len(self.provider.calls), 2)
        self.monotonic.return_value = 130
        self.provider.error = None
        self.assertFalse(self.charts.get("BTC")["stale"])
        self.assertEqual(len(self.provider.calls), 3)

    def test_first_failure_is_empty_with_no_invented_timestamp(self):
        self.provider.error = RuntimeError("offline")
        result = self.charts.get("BTC")
        self.assertTrue(result["stale"])
        self.assertEqual(result["candles"], [])
        self.assertIsNone(result["asof_ms"])
        self.assertEqual(self.charts.get("BTC"), result)
        self.assertEqual(len(self.provider.calls), 1)

    def test_failure_uses_completed_signal_candles_when_available(self):
        closed = d.parse_kraken_rows(self.provider.rows, d.H4, self.provider.observed)
        self.provider.error = RuntimeError("live chart offline")
        charts = LiveCharts(
            ASSETS, self.provider,
            fallback=lambda name: {"candles": closed, "asof_ms": self.provider.observed},
        )
        result = charts.get("BTC")
        self.assertTrue(result["stale"])
        self.assertEqual(len(result["candles"]), 16)
        self.assertFalse(any(bar["current"] for bar in result["candles"]))
        self.assertEqual(result["asof_ms"], self.provider.observed)
        self.assertIn("live chart offline", result["error"])

    def test_returned_values_cannot_mutate_cached_snapshot_or_provider(self):
        original_rows = copy.deepcopy(self.provider.rows)
        result = self.charts.get("BTC")
        result["candles"][-1]["c"] = 999
        result["candles"].clear()
        self.assertEqual(len(self.charts.get("BTC")["candles"]), 16)
        self.assertEqual(self.charts.get("BTC")["candles"][-1]["c"], 102)
        self.assertEqual(self.provider.rows, original_rows)

    def test_invalid_candles_never_publish_partial_history(self):
        mutations = {
            "zero price": lambda rows: rows[-1].__setitem__(1, "0"),
            "negative price": lambda rows: rows[-1].__setitem__(3, "-1"),
            "nonfinite price": lambda rows: rows[-1].__setitem__(2, "NaN"),
            "boolean price": lambda rows: rows[-1].__setitem__(4, True),
            "bounds": lambda rows: rows[-1].__setitem__(4, "105"),
            "fractional timestamp": lambda rows: rows[-1].__setitem__(0, rows[-1][0] + 0.5),
            "misaligned timestamp": lambda rows: rows[-1].__setitem__(0, rows[-1][0] + 1),
            "future candle": lambda rows: rows[-1].__setitem__(0, rows[-1][0] + d.H4 // 1000),
            "duplicate timestamp": lambda rows: rows[-2].__setitem__(0, rows[-1][0]),
            "missing candle": lambda rows: rows.pop(-2),
            "short row": lambda rows: rows.__setitem__(-1, [rows[-1][0]]),
            "stale current bar": lambda rows: rows.pop(),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                provider = FakeProvider()
                mutate(provider.rows)
                result = LiveCharts(ASSETS, provider).get("BTC")
                self.assertTrue(result["stale"])
                self.assertTrue(result["error"])
                self.assertEqual(result["candles"], [])

    def test_unknown_asset_never_contacts_provider(self):
        with self.assertRaises(KeyError):
            self.charts.get("ETH")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.provider.clock_calls, 0)

    def test_simultaneous_requests_coalesce_into_one_fetch(self):
        original_get = self.provider.get
        entered = threading.Event()
        release = threading.Event()

        def slow_get(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Test did not release the provider")
            return original_get(*args, **kwargs)

        self.provider.get = slow_get
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.charts.get, "BTC")
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.charts.get, "BTC")
            release.set()
            self.assertEqual(first.result(timeout=2), second.result(timeout=2))
        self.assertEqual(len(self.provider.calls), 1)


class DashboardChartRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = FakeProvider()
        store = d.StateStore(Path(self.temp.name) / "state.json", ASSETS, d.Rules())
        runtime = d.DashboardRuntime(ASSETS, d.Rules(), store, provider=self.provider)
        self.runtime = runtime
        self.client = d.create_app(runtime, chart_provider=self.provider).test_client()

    def test_chart_route_returns_current_candle_and_rejects_unknown_asset(self):
        response = self.client.get("/api/chart/BTC")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["candles"][-1]["current"])
        self.assertEqual(self.client.get("/api/chart/ETH").status_code, 404)

    def test_chart_route_reports_stale_feed(self):
        self.provider.error = RuntimeError("chart feed offline")
        response = self.client.get("/api/chart/BTC")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.get_json()["stale"])
        self.assertIn("chart feed offline", response.get_json()["error"])

    def test_chart_route_keeps_completed_fallback_visible(self):
        closed = d.parse_kraken_rows(self.provider.rows, d.H4, self.provider.observed)
        self.runtime.chart_fallback["BTC"] = {"candles": closed, "asof_ms": self.provider.observed}
        self.provider.error = RuntimeError("live chart offline")
        response = self.client.get("/api/chart/BTC")
        payload = response.get_json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(payload["candles"]), 16)
        self.assertFalse(any(bar["current"] for bar in payload["candles"]))

    def test_chart_route_uses_latest_quote_for_forming_fallback(self):
        closed = d.parse_kraken_rows(self.provider.rows, d.H4, self.provider.observed)
        self.runtime.chart_fallback["BTC"] = {
            "candles": closed, "asof_ms": self.provider.observed,
            "quote": {"last": 103},
        }
        self.provider.error = RuntimeError("live chart offline")
        with patch.object(time, "time", return_value=self.provider.observed / 1000):
            response = self.client.get("/api/chart/BTC")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["degraded"])
        self.assertFalse(payload["stale"])
        self.assertTrue(payload["candles"][-1]["current"])
        self.assertEqual(payload["candles"][-1]["c"], 103)
        self.assertEqual(payload["candles"][-1]["h"], 103)


if __name__ == "__main__":
    unittest.main()
