"""Offline regressions for the September code review."""
import copy
import time
from unittest.mock import patch

import adaptive_crypto_dashboard as d
from test_dashboard import ASSETS, TemporaryEngine, bars, momentum_fixture, reclaim_fixture
from test_live_charts import FakeProvider


class ReviewEngineTests(TemporaryEngine):
    def replay_fixture(self):
        self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        start = (trade["opened_ms"]//d.H4+1)*d.H4
        return trade, start

    def test_incomplete_15m_fragments_cannot_award_targets_before_4h_stop(self):
        trade, start = self.replay_fixture()
        c4 = [d.Candle(start, trade["entry"], trade["tp2"]+1,
                       trade["stop"]-1, trade["entry"], 100, d.H4)]
        c15 = bars(4, d.M15, start, trade["tp2"]+1)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, c4, c15, c4[-1].end+2))
        actual = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["status"], "stopped")
        self.assertFalse(actual["tp1_hit"])

    def test_complete_15m_history_keeps_known_target_then_stop_order(self):
        trade, start = self.replay_fixture()
        c15 = bars(16, d.M15, start, trade["entry"])
        c15[0] = d.Candle(start, trade["entry"], trade["tp1"]+1,
                         trade["entry"]-1, trade["entry"], 100, d.M15)
        c15[1] = d.Candle(start+d.M15, trade["entry"], trade["entry"]+1,
                         trade["stop"]-1, trade["entry"], 100, d.M15)
        c4 = [d.Candle(start, trade["entry"], trade["tp1"]+1,
                       trade["stop"]-1, trade["entry"], 1600, d.H4)]
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, c4, c15, c4[-1].end+2))
        actual = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["status"], "stopped")
        self.assertTrue(actual["tp1_hit"])
        self.assertEqual(actual["closed_ms"], c15[1].end)
        self.assertAlmostEqual(actual["realized_pnl"]/actual["initial_risk_usd"], .5)

    def test_partial_entry_4h_wick_does_not_stop_trade(self):
        trade, _ = self.replay_fixture()
        start = (trade["opened_ms"]//d.H4)*d.H4
        bar = d.Candle(start, trade["entry"], trade["tp2"]+1,
                       trade["stop"]-1, trade["entry"], 100, d.H4)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [bar], [], bar.end+2))
        actual = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["status"], "active")
        self.assertFalse(actual["tp1_hit"])

    def test_partial_entry_4h_close_can_establish_a_later_stop(self):
        trade, _ = self.replay_fixture()
        start = (trade["opened_ms"]//d.H4)*d.H4
        bar = d.Candle(start, trade["entry"], trade["entry"]+1,
                       trade["stop"]-2, trade["stop"]-1, 100, d.H4)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [bar], [], bar.end+2))
        actual = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["status"], "stopped")
        self.assertEqual(actual["closed_ms"], bar.end)

    def test_4h_target_is_not_replayed_when_15m_history_recovers(self):
        trade, start = self.replay_fixture()
        bar = d.Candle(start, trade["entry"], trade["tp1"]+1,
                       trade["entry"]-1, trade["entry"], 100, d.H4)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [bar], [], bar.end+2))
        before = self.store.snapshot()
        c15 = bars(16, d.M15, start, trade["tp1"])
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [bar], c15, bar.end+2))
        self.assertEqual(self.store.snapshot(), before)

    def test_trailing_stop_is_not_applied_to_earlier_4h_lows(self):
        c4, quote, now = momentum_fixture()
        self.engine.evaluate("BTC", quote, c4, [], now)
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        start = (now//d.H4+1)*d.H4
        first = d.Candle(start, trade["entry"], trade["tp1"]+1,
                         trade["entry"]-1, trade["tp1"], 100, d.M15)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [], [first], first.end+2))
        raised = self.store.snapshot()["assets"]["BTC"]["trades"][0]["stop"]
        self.assertGreater(raised, trade["entry"])
        # The full bar contains the pre-TP1 low; it cannot prove a later
        # breach of the stop that was raised after that low.
        full = d.Candle(start, first.o, first.h, first.l, first.c, 1600, d.H4)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [full], [], full.end+2))
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"][0]["status"], "active")
        next_bar = d.Candle(full.end+1, raised+1, raised+2, raised-.1, raised+1, 100, d.M15)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [], [next_bar], next_bar.end+2))
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"][0]["status"], "stopped")

    def test_4h_close_below_already_raised_stop_is_observed(self):
        c4, quote, now = momentum_fixture()
        self.engine.evaluate("BTC", quote, c4, [], now)
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        start = (now//d.H4+1)*d.H4
        first = d.Candle(start, trade["entry"], trade["tp1"]+1,
                         trade["entry"]-1, trade["tp1"], 100, d.M15)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [], [first], first.end+2))
        raised = self.store.snapshot()["assets"]["BTC"]["trades"][0]["stop"]
        full = d.Candle(start, first.o, first.h, min(first.l, raised-1), raised-1, 1600, d.H4)
        self.store.transaction(lambda s: d.monitor_positions(s, "BTC", None, [full], [], full.end+2))
        actual = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["status"], "stopped")
        self.assertEqual(actual["closed_ms"], full.end)

    def test_earlier_4h_invalidation_precedes_later_15m_targets(self):
        c4, _, _, _ = self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        invalidation = d.Candle(c4[-1].end+1, 111, 112, 98, 99, 100, d.H4)
        c4.append(invalidation)
        c15 = bars(4, d.M15, invalidation.end+1, trade["tp2"]+1)
        self.engine.evaluate("BTC", None, c4, c15, c15[-1].end+2)
        state = self.store.snapshot()
        actual = state["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["status"], "invalidated")
        self.assertEqual(actual["closed_ms"], invalidation.end)
        self.assertLess(actual["realized_pnl"], 0)
        self.assertFalse(actual["tp1_hit"])
        self.assertEqual([e["id"].split(":")[-1] for e in state["outbox"]
                          if e["kind"] == "telegram"], ["entry", "4H INVALIDATION"])

    def test_active_momentum_uses_full_post_entry_4h_stop_without_15m(self):
        c4, quote, now = momentum_fixture()
        self.engine.evaluate("BTC", quote, c4, [], now)
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        c4.append(d.Candle(c4[-1].end+1, 112.3, 113, 111, 112.3, 100, d.H4))
        breach = d.Candle(c4[-1].end+1, 112.3, 113, trade["stop"]-1, 112.3, 100, d.H4)
        c4.append(breach)
        now = breach.end+2
        self.engine.evaluate("BTC", {**quote, "asof_ms": now}, c4, [], now)
        actual = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(actual["status"], "stopped")
        self.assertEqual(actual["closed_ms"], breach.end)
        self.assertTrue(actual["tracking_gap"])
        self.assertAlmostEqual(actual["realized_pnl"], -actual["initial_risk_usd"])
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        before = restarted.snapshot()
        d.Engine(ASSETS, self.rules, restarted).evaluate("BTC", None, c4, [], now)
        self.assertEqual(restarted.snapshot()["outbox"], before["outbox"])

    def test_newer_reclaim_from_same_sweep_supersedes_pending(self):
        c4, _, _, now = reclaim_fixture()
        old = d.reclaim_scan(c4, self.rules, now)["setup"]
        self.store.transaction(lambda s: s["assets"]["BTC"].update(pending=copy.deepcopy(old)))
        c4.append(d.Candle(c4[-1].end+1, 111, 120, 110, 119, 200, d.H4))
        now = c4[-1].end+2
        self.engine.evaluate("BTC", None, c4, [], now)
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertEqual(record["pending"]["reclaim_end"], c4[-1].end)
        self.assertIn(old["key"], record["consumed"])
        self.assertEqual(record["pending"]["sweep_ms"], old["sweep_ms"])
        self.assertNotEqual(record["pending"]["stop"], old["stop"])
        self.assertEqual(record["trades"], [])
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        self.assertEqual(restarted.snapshot()["assets"]["BTC"]["pending"], record["pending"])


class ReviewChartTests(TemporaryEngine):
    def test_healthy_ohlc_cache_switches_to_current_quote_during_outage(self):
        provider = FakeProvider()
        closed = d.parse_kraken_rows(provider.rows, d.H4, provider.observed)
        fallback = {"candles": closed, "asof_ms": provider.observed+15000, "quote": {"last": 108}}
        with patch.object(time, "monotonic", return_value=100) as clock, \
                patch.object(time, "time", return_value=fallback["asof_ms"]/1000):
            charts = d.LiveCharts(ASSETS, provider, fallback=lambda name: fallback)
            self.assertFalse(charts.get("BTC")["degraded"])
            provider.error = RuntimeError("OHLC offline")
            clock.return_value = 115
            chart = charts.get("BTC")
        self.assertTrue(chart["degraded"])
        self.assertFalse(chart["stale"])
        self.assertEqual(chart["candles"][-1]["c"], 108)

    def test_fallback_can_appear_after_first_empty_chart_failure(self):
        provider = FakeProvider()
        closed = d.parse_kraken_rows(provider.rows, d.H4, provider.observed)
        provider.error = RuntimeError("OHLC offline")
        fallback = {}
        with patch.object(time, "monotonic", return_value=100) as clock:
            charts = d.LiveCharts(ASSETS, provider, fallback=lambda name: fallback)
            self.assertEqual(charts.get("BTC")["candles"], [])
            fallback.update(candles=closed, asof_ms=provider.observed)
            clock.return_value = 115
            self.assertTrue(charts.get("BTC")["candles"])

    def test_repeated_ohlc_failures_refresh_quotes_and_detect_quote_loss(self):
        provider = FakeProvider()
        closed = d.parse_kraken_rows(provider.rows, d.H4, provider.observed)
        provider.error = RuntimeError("OHLC offline")
        fallback = {"candles": closed, "asof_ms": provider.observed, "quote": {"last": 103}}
        runtime = d.DashboardRuntime(ASSETS, self.rules, self.store)
        runtime.chart_fallback["BTC"] = fallback
        client = d.create_app(runtime, chart_provider=provider).test_client()
        with patch.object(time, "monotonic", return_value=100) as clock, \
                patch.object(time, "time", return_value=provider.observed/1000) as wall:
            first = client.get("/api/chart/BTC")
            self.assertEqual(first.status_code, 200)
            fallback.update(asof_ms=provider.observed+60000, quote={"last": 108})
            clock.return_value = 160
            wall.return_value += 60
            second = client.get("/api/chart/BTC")
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json["candles"][-1]["c"], 108)
            self.assertEqual(second.json["asof_ms"], fallback["asof_ms"])
            fallback.update(asof_ms=provider.observed+120000, quote=None)
            clock.return_value = 220
            wall.return_value += 60
            third = client.get("/api/chart/BTC")
            self.assertEqual(third.status_code, 503)
            self.assertTrue(third.json["stale"])

    def test_fallback_quote_expires_even_if_runtime_stops_updating(self):
        provider = FakeProvider()
        closed = d.parse_kraken_rows(provider.rows, d.H4, provider.observed)
        provider.error = RuntimeError("OHLC offline")
        fallback = {"candles": closed, "asof_ms": provider.observed, "quote": {"last": 103}}
        with patch.object(time, "time", return_value=provider.observed/1000+60):
            chart = d.LiveCharts(ASSETS, provider, fallback=lambda name: fallback).get("BTC")
        self.assertTrue(chart["stale"])
        self.assertFalse(chart["degraded"])
