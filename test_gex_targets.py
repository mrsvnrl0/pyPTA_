"""Offline GEX target selection, freshness, execution and saved-level regression tests."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import requests

from adaptive_crypto.core import Candle, DataError, M5, M30, Rules
from adaptive_crypto.gex import NaiveGEX, map_smc
from adaptive_crypto.gex_targets import context, select_target
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.smc import scan
from adaptive_crypto.smc_engine import SMCEngine
from adaptive_crypto.smc_ledger import SMCStore
from test_smc_formulas import bars, full_fixture, mirror

ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


def fixture(side="long"):
    high, low = full_fixture()
    prefix = bars([(120, 125, 119, 122), (122, 160, 120, 125), (125, 126, 118, 120)], M30)
    lower = [Candle(b.t+j*M5, b.o, b.h, b.l, b.c, 100, M5) for b in prefix for j in range(6)]
    high = prefix+[replace(b, t=b.t+3*M30) for b in high]
    low = lower+[replace(b, t=b.t+3*M30) for b in low]
    return (high, low) if side == "long" else (mirror(high, 300), mirror(low, 300))


class GEXTargetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rules = Rules(strategy_model="smc_video", smc_gex_targets=True, smc_pivot_strength=1,
                           smc_stop_buffer_bps=10, smc_tp_sweep_buffer_bps=5, fee_rate=.001)
        offline = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Offline tests"))
        offline.start()
        self.addCleanup(offline.stop)
        self.start()

    def start(self, side="long"):
        self.side = side
        self.high, self.low = fixture(side)
        self.now = self.low[-1].end+1
        price = self.low[-1].c
        self.quote = {"bid": price-.00001, "ask": price, "last": price, "asof_ms": self.now, "volume24_base": 100}
        self.profile = {"status": "ready", "stale": False, "base": "BTC", "spot": price,
            "asof_ms": self.now-1000, "calculated_ms": self.now-1000, "expires_ms": self.now+1000000,
            "regime": "positive", "call_wall": {"strike": 160 if side == "long" else 200},
            "put_wall": {"strike": 100 if side == "long" else 140}, "gamma_flip": None}

    def ctx(self, profile=None, rules=None):
        return context(self.high, self.low, self.quote, rules or self.rules,
                       self.profile if profile is None else profile, self.now, "BTC/USD")

    def selected(self, ctx=None):
        return select_target(self.high, self.low, self.rules, self.side, self.quote["ask"], self.now,
                             self.ctx() if ctx is None else ctx)

    def test_wall_selects_farther_aligned_liquidity_in_both_directions(self):
        for side, nearest, target in (("long", 140, 160), ("short", 160, 140)):
            self.start(side)
            chosen = self.selected()
            self.assertEqual(chosen["price"], target)
            selection = chosen["target_selection"]
            self.assertEqual(selection["nearest_smc_price"], nearest)
            self.assertEqual(selection["method"], "gex_smc")
            self.assertEqual(selection["matches"][0]["key"], "call" if side == "long" else "put")

    def test_invalid_stale_expired_future_mismatched_and_basis_data_fall_back(self):
        for change in ({"status": "stale"}, {"stale": True}, {"asof_ms": self.now-300000},
                       {"asof_ms": self.now+1}, {"calculated_ms": self.now+1},
                       {"expires_ms": self.now}, {"base": "ETH"}, {"spot": 130},
                       {"call_wall": {"strike": float("nan")}}, {"regime": "invented"}):
            with self.subTest(change=change):
                ctx = self.ctx({**self.profile, **change})
                self.assertFalse(ctx["ready"])
                self.assertEqual(self.selected(ctx)["price"], 140)
        self.assertEqual(self.selected(self.ctx({}))["price"], 140)

    def test_alignment_boundary_negative_regime_wrong_side_and_disabled(self):
        for wall, expected in ((160, 160), (160/1.001, 160), (159.8, 140), (100, 140)):
            ctx = self.ctx({**self.profile, "call_wall": {"strike": wall}})
            self.assertEqual(self.selected(ctx)["price"], expected)
        ctx = self.ctx({**self.profile, "regime": "negative"})
        self.assertTrue(ctx["matches"])
        self.assertFalse(ctx["matches"][0]["eligible"])
        self.assertEqual(self.selected(ctx)["price"], 140)
        disabled = replace(self.rules, smc_gex_targets=False)
        self.assertEqual(select_target(self.high, self.low, disabled, "long", 120, self.now, self.ctx())["price"], 140)

    def test_old_context_and_consumed_liquidity_cannot_select_a_target(self):
        self.assertEqual(self.selected({**self.ctx(), "selected_ms": self.now-1})["price"], 140)
        high = [replace(b, h=161) if i == len(self.high)-1 else b for i, b in enumerate(self.high)]
        self.assertIsNone(select_target(high, self.low, self.rules, "long", 120, self.now, self.ctx()))

    def test_premium_order_block_must_be_fresh_and_in_the_dealing_range(self):
        setup = {"zone_low": 139, "zone_high": 161, "ob_ms": self.high[-2].t, "bos_end": self.high[-1].end}
        with patch("adaptive_crypto.gex_targets.higher_setups", side_effect=lambda h,r,s: [setup] if s == "short" else []):
            ctx = self.ctx({**self.profile, "call_wall": {"strike": 150}})
        self.assertTrue(any(m["poi"]["kind"] == "premium_ob" for m in ctx["matches"]))
        self.assertEqual(self.selected(ctx)["target_selection"]["method"], "gex_smc")
        touched = {**setup, "zone_low": 110, "zone_high": 161, "bos_end": self.high[-2].end}
        with patch("adaptive_crypto.gex_targets.higher_setups", return_value=[touched]):
            self.assertFalse(any(p["kind"].endswith("ob") for p in self.ctx()["pois"]))

    def test_flip_requires_reversal_mss_and_close_cross_not_touch_or_bos(self):
        event = {"level": 119, "bar_ms": self.low[-1].t, "end_ms": self.low[-1].end}
        rows = [{"direction": "bearish"}, {"direction": "bullish", "event": event}]
        # Fixture prior close=117 and latest close=120 crosses 119.
        profile = {**self.profile, "call_wall": None, "gamma_flip": 119, "regime": "negative"}
        with patch("adaptive_crypto.gex_targets.structure_readings", return_value=rows):
            ctx = self.ctx(profile)
        self.assertTrue(ctx["matches"][0]["eligible"])
        self.assertEqual(self.selected(ctx)["target_selection"]["matches"][0]["key"], "flip")
        for previous in ("bullish", "neutral"):
            with patch("adaptive_crypto.gex_targets.structure_readings", return_value=[{"direction": previous}, rows[-1]]):
                self.assertEqual(self.ctx(profile)["matches"], [])
        rows[-1]["event"]["level"] = 120
        with patch("adaptive_crypto.gex_targets.structure_readings", return_value=rows):
            self.assertFalse(self.ctx({**profile, "gamma_flip": 120})["matches"][0]["eligible"])

    def test_paper_targets_use_gex_and_survive_restart_and_gex_setting_changes(self):
        for side in ("long", "short"):
            self.start(side)
            rules = replace(self.rules, market_mode="margin")
            store = SMCStore(self.root/(side+".json"), ASSETS, rules)
            engine = SMCEngine(ASSETS, rules, store)
            engine.evaluate("BTC", self.quote, self.high, self.low, self.now, gex_profile=self.profile)
            order = store.snapshot()["assets"]["BTC"]["pending"]
            self.assertIsNotNone(order)
            raw = 160 if side == "long" else 140
            self.assertEqual(order["target_liquidity_price"], raw)
            self.assertAlmostEqual(order["target"], raw*(1+(.0005 if side == "long" else -.0005)))
            self.assertEqual(order["target_selection"]["method"], "gex_smc")
            restarted = SMCStore(store.path, ASSETS, replace(rules, smc_gex_alignment_bps=20, smc_gex_targets=False))
            self.assertEqual(restarted.snapshot()["assets"]["BTC"]["pending"], order)

    def test_spot_bearish_buy_and_its_upper_exit_use_separate_selections(self):
        self.start("short")
        store = SMCStore(self.root/"spot.json", ASSETS, self.rules)
        engine = SMCEngine(ASSETS, self.rules, store)
        engine.evaluate("BTC", self.quote, self.high, self.low, self.now, gex_profile=self.profile)
        order = store.snapshot()["assets"]["BTC"]["pending"]
        self.assertEqual(order["side"], "long")
        self.assertAlmostEqual(order["limit"], 140*.9995)
        self.assertEqual(order["buy_target_selection"]["method"], "gex_smc")
        self.assertGreater(order["target"], order["limit"])
        self.assertEqual(order["target_selection"]["liquidity_price"], order["target_liquidity_price"])

    def test_holdings_watches_manual_overrides_and_saved_levels(self):
        positions = PositionStore(self.root/"positions.json")
        positions.open_position(ASSETS, "BTC", "long", 120, 1, uuid.uuid4().hex, self.now-1, target_mode="gex_smc")
        positions.monitor_take_profit("BTC", "BTC/USD", self.high, self.low, self.quote, self.rules, self.now, gex_context=self.ctx())
        target = positions.snapshot()["positions"][0]["take_profit"]
        self.assertAlmostEqual(target["price"], 160*1.0005)
        self.start("short")
        positions.open_buy_watch(ASSETS, "BTC", 180, None, 1, uuid.uuid4().hex, self.now-1)
        positions.open_buy_watch(ASSETS, "BTC", 180, 150, 1, uuid.uuid4().hex, self.now-1)
        positions.monitor_buy_watches("BTC", "BTC/USD", self.high, self.low, self.quote, self.rules, self.now, gex_context=self.ctx())
        watches = positions.snapshot()["buy_watches"]
        self.assertAlmostEqual(watches[0]["buy_price"], 160*.9995)
        self.assertEqual(watches[0]["target_selection"]["method"], "smc")
        self.assertEqual(watches[0]["target_selection"]["matches"], [])
        self.assertEqual(watches[1]["buy_price"], 150)
        restarted = PositionStore(positions.path)
        restarted.monitor_buy_watches("BTC", "BTC/USD", self.high, self.low, self.quote, self.rules, self.now, gex_context=None)
        self.assertEqual(restarted.snapshot()["buy_watches"][0]["buy_price"], watches[0]["buy_price"])
        self.assertEqual(restarted.snapshot()["positions"][0]["take_profit"], target)

    def test_saved_gex_watch_stays_fixed_and_readded_watch_uses_nearest_smc(self):
        self.start("short")
        positions = PositionStore(self.root/"saved-watch.json")
        positions.open_buy_watch(ASSETS, "BTC", 180, None, 1, uuid.uuid4().hex, self.now-1)
        old = self.selected()
        self.assertEqual(old["target_selection"]["method"], "gex_smc")
        positions.transaction(lambda d: d["buy_watches"][0].update(
            buy_price=old["price"]*.9995, target_source="sweep", liquidity_price=old["price"],
            pivot_ms=old["bar_ms"], sweep_buffer_bps=5, selected_ms=self.now,
            target_selection=old["target_selection"], armed=True))
        positions.transaction(lambda d: d["buy_watches"][0].pop("target_mode"))
        positions = PositionStore(positions.path)
        positions.monitor_buy_watches("BTC", "BTC/USD", self.high, self.low, self.quote,
                                      self.rules, self.now, gex_context=self.ctx())
        saved = positions.snapshot()["buy_watches"][0]
        self.assertAlmostEqual(saved["buy_price"], 140*.9995)
        self.assertEqual(saved["target_selection"], old["target_selection"])
        self.assertNotIn("target_mode", saved)
        positions.cancel_buy_watch(saved["id"])
        positions.open_buy_watch(ASSETS, "BTC", 180, None, 1, uuid.uuid4().hex, self.now)
        positions.monitor_buy_watches("BTC", "BTC/USD", self.high, self.low, self.quote,
                                      self.rules, self.now, gex_context=self.ctx())
        previous, current = positions.snapshot()["buy_watches"]
        self.assertEqual(previous["status"], "cancelled")
        self.assertAlmostEqual(current["buy_price"], 160*.9995)
        self.assertEqual(current["target_selection"]["method"], "smc")
        self.assertEqual(current["target_selection"]["reason"], "Nearest untaken SMC liquidity")

    def test_runtime_uses_same_snapshot_for_engine_holdings_and_watches(self):
        store = SMCStore(self.root/"runtime.json", ASSETS, self.rules)
        provider = Mock()
        provider.now.return_value = self.now
        provider.quotes.return_value = {"BTC/USD": self.quote}
        provider.candles.side_effect = lambda s,i,n: self.high if i == M30 else self.low
        runtime = DashboardRuntime(ASSETS, self.rules, store, provider=provider)
        runtime.gex = Mock()
        runtime.gex.snapshot.return_value = self.profile
        runtime.positions.open_position(ASSETS, "BTC", "long", 120, 1, uuid.uuid4().hex, self.now-1, target_mode="gex_smc")
        runtime.scan_once()
        self.assertEqual(runtime.position_errors, {})
        self.assertEqual(runtime.positions.snapshot()["positions"][0]["position_targets"]["gex_smc"]["selection"]["method"], "gex_smc")
        self.assertEqual(store.snapshot()["assets"]["BTC"]["pending"]["target_selection"]["method"], "gex_smc")
        self.assertTrue(runtime.data["BTC"]["gex_context"]["ready"])

    def test_personal_target_modes_override_global_preference_independently(self):
        for side in ("long", "short"):
            self.start(side)
            for enabled in (False, True):
                rules = replace(self.rules, smc_gex_targets=enabled)
                positions = PositionStore(self.root/f"modes-{side}-{enabled}.json")
                for mode in ("smc", "gex_smc"):
                    if side == "long":
                        positions.open_position(ASSETS, "BTC", "long", 120, 1,
                                                uuid.uuid4().hex, self.now-1, target_mode=mode)
                    else:
                        positions.open_buy_watch(ASSETS, "BTC", 180, None, 1,
                                                 uuid.uuid4().hex, self.now-1, target_mode=mode)
                positions.monitor_take_profit("BTC", "BTC/USD", self.high, self.low, self.quote,
                                              rules, self.now, gex_context=self.ctx())
                positions.monitor_buy_watches("BTC", "BTC/USD", self.high, self.low, self.quote,
                                              rules, self.now, gex_context=self.ctx())
                saved = positions.snapshot()
                if side == "long":
                    self.assertEqual([p["take_profit"]["liquidity_price"] for p in saved["positions"]], [140, 160])
                else:
                    self.assertEqual([w["liquidity_price"] for w in saved["buy_watches"]], [160, 140])
                    positions.open_buy_watch(ASSETS, "BTC", 180, 150, 1,
                                             uuid.uuid4().hex, self.now-1, target_mode="gex_smc")
                    positions.monitor_buy_watches("BTC", "BTC/USD", self.high, self.low, self.quote,
                                                  rules, self.now, gex_context=self.ctx())
                    self.assertEqual(positions.snapshot()["buy_watches"][-1]["buy_price"], 150)

    def test_runtime_fetches_personal_gex_with_paper_preference_disabled(self):
        for side in ("long", "short"):
            self.start(side)
            rules = replace(self.rules, smc_gex_targets=False)
            store = SMCStore(self.root/f"personal-{side}.json", ASSETS, rules)
            provider = Mock()
            provider.now.return_value = self.now
            provider.quotes.return_value = {"BTC/USD": self.quote}
            provider.candles.side_effect = lambda s,i,n: self.high if i == M30 else self.low
            runtime = DashboardRuntime(ASSETS, rules, store, provider=provider)
            runtime.gex = Mock()
            runtime.gex.snapshot.return_value = self.profile
            if side == "long":
                runtime.positions.open_position(ASSETS, "BTC", "long", 120, 1,
                                                uuid.uuid4().hex, self.now-1, target_mode="gex_smc")
            else:
                runtime.positions.open_buy_watch(ASSETS, "BTC", 180, None, 1,
                                                 uuid.uuid4().hex, self.now-1, target_mode="gex_smc")
            runtime.scan_once()
            runtime.gex.snapshot.assert_called_once_with("BTC")
            self.assertEqual(runtime.position_errors, {})
            self.assertFalse(runtime.data["BTC"]["gex_context"]["enabled"])
            saved = runtime.positions.snapshot()
            selection = (saved["positions"][0]["position_targets"]["gex_smc"]["selection"] if side == "long"
                         else saved["buy_watches"][0]["target_selection"])
            self.assertEqual(selection["method"], "gex_smc")

    def test_personal_gex_fallback_is_saved_and_does_not_retarget(self):
        self.start("short")
        positions = PositionStore(self.root/"fallback-watch.json")
        positions.open_buy_watch(ASSETS, "BTC", 180, None, 1,
                                 uuid.uuid4().hex, self.now-1, target_mode="gex_smc")
        positions.monitor_buy_watches("BTC", "BTC/USD", self.high, self.low, self.quote,
                                      self.rules, self.now, gex_context=None)
        original = positions.snapshot()["buy_watches"][0]
        self.assertEqual(original["target_mode"], "gex_smc")
        self.assertEqual(original["target_selection"]["method"], "smc")
        restarted = PositionStore(positions.path)
        restarted.monitor_buy_watches("BTC", "BTC/USD", self.high, self.low, self.quote,
                                      self.rules, self.now, gex_context=self.ctx())
        self.assertEqual(restarted.snapshot()["buy_watches"][0]["buy_price"], original["buy_price"])

    def test_options_refresh_does_not_block_a_scan_snapshot(self):
        feed = NaiveGEX(ASSETS)
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        def slow(_):
            started.set()
            release.wait(5)
            done.set()
        with patch.object(feed, "get", side_effect=slow) as fetch:
            try:
                self.assertIsNone(feed.snapshot("BTC"))
                self.assertTrue(started.wait(1))
                self.assertIsNone(feed.snapshot("BTC"))
                fetch.assert_called_once()
            finally:
                release.set()
                self.assertTrue(done.wait(1))

    def test_chart_never_combines_new_gex_with_old_target_confluence(self):
        row = {"asof_ms": self.now, "quote": self.quote, "gex_context": self.ctx()}
        mapped = map_smc(self.profile, row, self.now)
        self.assertTrue(mapped["target_confluence"]["ready"])
        changed = map_smc({**self.profile, "asof_ms": self.now}, row, self.now)
        self.assertFalse(changed["target_confluence"]["ready"])
        self.assertEqual(changed["target_confluence"]["matches"], [])
        stale = map_smc(self.profile, row, self.now+30001)
        self.assertFalse(stale["target_confluence"]["ready"])

    def test_new_settings_reject_invalid_json_values(self):
        with self.assertRaises(DataError):
            replace(self.rules, smc_gex_targets=1).validate()
        for key in ("smc_gex_alignment_bps", "smc_gex_max_basis_bps"):
            for value in (-1, float("nan"), float("inf"), float("-inf"), True, "200", None):
                with self.subTest(key=key, value=value), self.assertRaises(DataError):
                    replace(self.rules, **{key: value}).validate()

    def test_gex_tolerances_accept_values_above_100_bps(self):
        from adaptive_crypto.core import load_settings
        path = self.root/"settings.json"
        for value in (0, 100, 100.5, 200, 500, 10000, 25000):
            with self.subTest(value=value):
                path.write_text(json.dumps({"strategy": {
                    "smc_gex_alignment_bps": value, "smc_gex_max_basis_bps": value}}), encoding="utf-8")
                _, rules, _ = load_settings(path)
                self.assertEqual(rules.smc_gex_alignment_bps, value)
                self.assertEqual(rules.smc_gex_max_basis_bps, value)

    def test_saved_target_provenance_corruption_fails_without_overwriting(self):
        store = SMCStore(self.root/"corrupt.json", ASSETS, self.rules)
        SMCEngine(ASSETS, self.rules, store).evaluate("BTC", self.quote, self.high, self.low, self.now, gex_profile=self.profile)
        saved = store.snapshot()
        saved["assets"]["BTC"]["pending"]["target_selection"]["liquidity_price"] += 1
        store.path.write_text(json.dumps(saved), encoding="utf-8")
        before = store.path.read_bytes()
        with self.assertRaises(DataError):
            SMCStore(store.path, ASSETS, self.rules)
        self.assertEqual(store.path.read_bytes(), before)

    def test_no_match_retains_original_paper_target_and_legacy_settings_keep_pending(self):
        store = SMCStore(self.root/"fallback.json", ASSETS, self.rules)
        baseline, = scan(self.high, self.low, self.rules, "long")["entries"]
        SMCEngine(ASSETS, self.rules, store).evaluate("BTC", self.quote, self.high, self.low, self.now, gex_profile={})
        saved = store.snapshot()
        order = saved["assets"]["BTC"]["pending"]
        self.assertEqual(order["target"], baseline["target"])
        self.assertEqual(order["target_selection"]["method"], "smc")
        for key in list(saved["settings"]):
            if key.startswith("smc_gex_"):
                del saved["settings"][key]
        store.path.write_text(json.dumps(saved), encoding="utf-8")
        self.assertEqual(SMCStore(store.path, ASSETS, self.rules).snapshot()["assets"]["BTC"]["pending"], order)


if __name__ == "__main__":
    unittest.main()
