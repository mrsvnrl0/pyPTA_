"""Offline regressions for reclaim lifecycle and completed-candle alignment."""
import copy
from dataclasses import replace
import json
from unittest.mock import patch

import requests

import adaptive_crypto_dashboard as d
from test_dashboard import ASSETS, TemporaryEngine, bars, momentum_fixture, reclaim_fixture, setbar


class QualificationSafetyTests(TemporaryEngine):
    def setUp(self):
        super().setUp()
        network = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Network prohibited during tests"))
        network.start()
        self.addCleanup(network.stop)

    def test_live_stop_breach_on_discovery_is_terminal_after_recovery_and_restart(self):
        c4, c15, quote, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        bid = setup["stop"] - .1
        first = self.engine.evaluate("BTC", {**quote, "bid": bid, "ask": bid+.02}, c4, c15, now)
        self.assertEqual(first["reclaim"]["status"], "SETUP STOP BREACHED")
        self.assertIsNone(first["reclaim"]["setup"])
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertIsNone(record["pending"])
        self.assertEqual(record["consumed"], [setup["key"]])
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        engine = d.Engine(ASSETS, self.rules, restarted)
        for delay in (15000, 30000):
            engine.evaluate("BTC", {**quote, "asof_ms": now+delay}, c4, c15, now+delay)
        state = restarted.snapshot()
        self.assertEqual(state["assets"]["BTC"]["trades"], [])
        self.assertEqual(state["assets"]["BTC"]["consumed"], [setup["key"]])
        self.assertEqual(state["outbox"], [])

    def test_first_stop_check_does_not_require_a_trigger_or_15m_feed(self):
        c4, c15, quote, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        quote = {**quote, "bid": setup["stop"], "ask": setup["stop"]+.02}
        row = self.engine.evaluate("BTC", quote, c4, [], now)
        self.assertEqual(row["reclaim"]["status"], "SETUP STOP BREACHED")
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertIsNone(record["pending"])
        self.assertIn(setup["key"], record["consumed"])

    def boundary_fixture(self):
        c4, _, quote, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        start = c4[-1].end+1
        c15 = bars(24, d.M15, start-8*d.M15, 109)
        setbar(c15, 8, 109, 110, 105, 106)
        setbar(c15, 9, 106, 107, 101, 102)
        setbar(c15, 10, 102, 104, 101, 103)
        for i in range(11, 23):
            setbar(c15, i, 98, 98.5, 97.5, 98.1)
        setbar(c15, 23, 98.1, 99.2, 97.9, 99)
        now = c15[-1].end+1001
        quote = {**quote, "ask": 99.01, "bid": 98.99, "asof_ms": now}
        latest4 = d.Candle(start, c15[8].o, max(b.h for b in c15[8:]),
                           min(b.l for b in c15[8:]), c15[-1].c,
                           sum(b.v for b in c15[8:]), d.H4)
        return c4, c15, quote, now, setup, latest4

    def test_new_15m_trigger_waits_for_4h_boundary_invalidation(self):
        c4, c15, quote, now, setup, latest4 = self.boundary_fixture()
        self.store.transaction(lambda state: state["assets"]["BTC"].update(pending=copy.deepcopy(setup)))
        for offset in (0, 1000, 4999):
            observed = latest4.end+1+offset
            row = self.engine.evaluate("BTC", {**quote, "asof_ms": observed}, c4, c15, observed)
            self.assertEqual(row["reclaim"]["status"], "WAITING FOR LATEST COMPLETED 4H CANDLE")
            self.assertIn("4h", row["errors"])
            self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])
        row = self.engine.evaluate("BTC", quote, c4+[latest4], c15, now)
        state = self.store.snapshot()
        self.assertEqual(state["assets"]["BTC"]["trades"], [])
        self.assertIn(setup["key"], state["assets"]["BTC"]["consumed"])
        self.assertNotIn("4h", row["errors"])
        self.assertEqual(state["outbox"], [])

    def test_missing_4h_boundary_does_not_arm_an_old_setup(self):
        c4, c15, quote, now, setup, latest4 = self.boundary_fixture()
        row = self.engine.evaluate("BTC", quote, c4, c15, now)
        self.assertIsNone(self.store.snapshot()["assets"]["BTC"]["pending"])
        self.assertEqual(row["momentum"]["status"], "WAITING FOR LATEST COMPLETED 4H CANDLE")

    def test_missing_15m_boundary_blocks_entry_until_latest_bar_arrives(self):
        c4, c15, quote, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        now = c15[-1].end+d.M15+1001
        quote = {**quote, "asof_ms": now}
        first = self.engine.evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(first["reclaim"]["status"], "WAITING FOR LATEST COMPLETED 15M CANDLE")
        self.assertIn("15m", first["errors"])
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"], [])
        c15.append(d.Candle(c15[-1].end+1, 111.5, 112, setup["stop"]-.1, 111.5, 100, d.M15))
        self.engine.evaluate("BTC", quote, c4, c15, now)
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertIsNone(record["pending"])
        self.assertIn(setup["key"], record["consumed"])
        self.assertEqual(record["trades"], [])

    def test_complete_15m_feed_recovers_without_losing_a_valid_setup(self):
        c4, c15, quote, now = reclaim_fixture()
        now = c15[-1].end+d.M15+1001
        quote = {**quote, "asof_ms": now}
        self.engine.evaluate("BTC", quote, c4, c15, now)
        c15.append(d.Candle(c15[-1].end+1, 111.5, 112, 111, 111.6, 100, d.M15))
        row = self.engine.evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(row["reclaim"]["status"], "ACTIVE PAPER TRADE")
        self.assertNotIn("15m", row["errors"])

    def test_available_candles_still_close_positions_during_4h_boundary_wait(self):
        c4, c15, quote, now = self.qualify()
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        boundary = c4[-1].end+1+d.H4
        while c15[-1].end+1 < boundary:
            c15.append(d.Candle(c15[-1].end+1, 111, 112, 110, 111, 100, d.M15))
        c15[-1] = replace(c15[-1], l=trade["stop"]-.1)
        now = boundary+1000
        row = self.engine.evaluate("BTC", None, c4, c15, now)
        self.assertIn("4h", row["errors"])
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(trade["status"], "stopped")
        self.assertEqual(trade["remaining"], 0)

    def test_consumed_failure_allows_a_distinct_later_reclaim_from_same_sweep(self):
        c4, _, _, now = reclaim_fixture()
        original = d.reclaim_scan(c4, self.rules, now)["setup"]
        self.store.transaction(lambda state: state["assets"]["BTC"].update(pending=copy.deepcopy(original)))
        c4.append(d.Candle(c4[-1].end+1, 111, 111.5, 98, 99, 100, d.H4))
        c4.append(d.Candle(c4[-1].end+1, 99, 114, 98, 113, 180, d.H4))
        now = c4[-1].end+2
        expected = d.reclaim_scan(c4, self.rules, now)["setup"]
        row = self.engine.evaluate("BTC", None, c4, [], now)
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertNotEqual(original["key"], expected["key"])
        self.assertEqual(record["pending"]["key"], expected["key"])
        self.assertIn(original["key"], record["consumed"])
        self.assertEqual(row["reclaim"]["status"], "WAITING FOR VALID 15M DATA")

    def test_exact_consumed_identity_cannot_requalify(self):
        c4, _, _, now = reclaim_fixture()
        original = d.reclaim_scan(c4, self.rules, now)["setup"]
        self.assertIsNone(d.reclaim_scan(c4, self.rules, now, [original["key"]])["setup"])


class StrictQualificationTests(TemporaryEngine):
    def newer_reclaim_fixture(self, breach_new_stop=False):
        c4, _, quote, now = reclaim_fixture()
        old = d.reclaim_scan(c4, self.rules, now)["setup"]
        c4 += bars(12, d.H4, c4[-1].end+1, 109)
        setbar(c4, 64, 109, 110, 104, 109.1)
        setbar(c4, 68, 109, 113, 108, 112)
        setbar(c4, 69, 112, 112.5, 105, 105.4)
        setbar(c4, 70, 105.5, 108, 102, 105)
        setbar(c4, 71, 105.1, 107, 104, 106.5)
        setbar(c4, 72, 105.5, 120, 104, 119, 200)
        candidate = d.reclaim_scan(c4, self.rules, c4[-1].end+2)["setup"]
        c15 = []
        for b in c4[60:]:
            c15.append(d.Candle(b.t, b.o, b.h, b.l, b.c, b.v/16, d.M15))
            c15 += [d.Candle(b.t+i*d.M15, b.c, b.c, b.c, b.c, b.v/16, d.M15) for i in range(1, 16)]
        low = candidate["stop"]-.1 if breach_new_stop else 102
        tail = [(119, 120, 115, 116), (116, 117, 111, 112),
                (112, 113, low, 102), (102, 104, low, 103),
                (103, 112, 102, 111.5), (111.5, 114, 110, 113.5)]
        start = c4[-1].end+1
        c15 += [d.Candle(start+i*d.M15, *ohlc, 100, d.M15) for i, ohlc in enumerate(tail)]
        now = c15[-1].end+2
        quote = {**quote, "bid": 113.49, "ask": 113.51, "asof_ms": now}
        return c4, c15, quote, now, old, candidate

    def test_newer_reclaim_replaces_all_old_levels_and_confirmation_evidence(self):
        c4, c15, quote, now, old, candidate = self.newer_reclaim_fixture()
        old["confirmation_reset_ms"] = candidate["reclaim_end"]+d.M15
        self.store.transaction(lambda state: state["assets"]["BTC"].update(pending=copy.deepcopy(old)))
        row = self.engine.evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(row["reclaim"]["status"], "ACTIVE PAPER TRADE", row["reclaim"])
        record = self.store.snapshot()["assets"]["BTC"]
        trade = record["trades"][0]
        self.assertIn(old["key"], record["consumed"])
        self.assertTrue(trade["signal_key"].startswith(candidate["key"]+":"))
        self.assertEqual(trade["stop"], candidate["stop"])
        self.assertNotEqual(trade["stop"], old["stop"])
        self.assertEqual(record["reclaim_floor_ms"], candidate["reclaim_end"]+1)
        retest = next(check for check in trade["evidence"] if check["key"] == "retest")
        self.assertGreater(retest["candle_ms"], candidate["reclaim_end"])
        self.assertEqual(retest["required"]["zone"], [candidate["zone_low"], candidate["zone_high"]])
        plan = d.entry_plan(quote["ask"], quote["bid"], c15[-1].c, candidate["stop"], candidate["atr"],
                            self.rules, 1000, 1000, 0, c4)
        self.assertEqual((trade["tp1"], trade["tp2"]), (plan["tp1"], plan["tp2"]))

    def test_superseded_setup_cannot_return_after_new_setup_stops_and_restarts(self):
        c4, c15, quote, now, old, candidate = self.newer_reclaim_fixture(breach_new_stop=True)
        self.store.transaction(lambda state: state["assets"]["BTC"].update(pending=copy.deepcopy(old)))
        row = self.engine.evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(row["reclaim"]["status"], "SETUP STOP BREACHED")
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertIn(old["key"], record["consumed"])
        self.assertIn(candidate["key"], record["consumed"])
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        d.Engine(ASSETS, self.rules, restarted).evaluate("BTC", {**quote, "asof_ms": now+15000}, c4, c15, now+15000)
        self.assertEqual(restarted.snapshot()["assets"]["BTC"]["trades"], [])
        self.assertIsNone(restarted.snapshot()["assets"]["BTC"]["pending"])

    def test_reclaim_chronology_also_blocks_older_unconsumed_candidates(self):
        c4, c15, quote, now, old, candidate = self.newer_reclaim_fixture()
        older = d.reclaim_scan(c4[:-1], self.rules, now)["setup"]
        self.assertIsNotNone(older)
        self.assertLess(older["reclaim_end"], candidate["reclaim_end"])
        self.store.transaction(lambda state: state["assets"]["BTC"].update(
            consumed=[candidate["key"]], reclaim_floor_ms=candidate["reclaim_end"]+1))
        self.engine.evaluate("BTC", quote, c4, c15, now)
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertEqual(record["trades"], [])
        self.assertIsNone(record["pending"])

    def test_new_setup_waits_for_its_own_post_reclaim_retest(self):
        c4, c15, quote, now, old, candidate = self.newer_reclaim_fixture()
        c15 = c15[:-6] + bars(4, d.M15, candidate["reclaim_end"]+1, 116)
        now = c15[-1].end+2
        quote = {**quote, "bid": 116.09, "ask": 116.11, "asof_ms": now}
        self.store.transaction(lambda state: state["assets"]["BTC"].update(pending=copy.deepcopy(old)))
        row = self.engine.evaluate("BTC", quote, c4, c15, now)
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertEqual(record["pending"]["key"], candidate["key"])
        self.assertEqual(row["reclaim"]["status"], "WAITING FOR 15M RETEST")
        self.assertEqual(record["trades"], [])

    def test_completed_failed_break_cannot_revive_without_a_new_confirmation(self):
        c4, c15, quote, now = reclaim_fixture()
        old_trigger = c15[-1].t
        self.engine.evaluate("BTC", {**quote, "bid": 113.99, "ask": 114}, c4, c15, now)
        c15.append(d.Candle(c15[-1].end+1, 111.5, 111.6, 101, 102, 100, d.M15))
        now = c15[-1].end+2
        row = self.engine.evaluate("BTC", {**quote, "bid": 101.99, "ask": 102.01, "asof_ms": now}, c4, c15, now)
        self.assertIsNone(row["reclaim"]["trigger"])
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        engine = d.Engine(ASSETS, self.rules, restarted)
        engine.evaluate("BTC", {**quote, "asof_ms": now+15000}, c4, c15, now+15000)
        self.assertEqual(restarted.snapshot()["assets"]["BTC"]["trades"], [])
        for ohlc in [(102, 104, 101.5, 103), (103, 105, 102, 104), (104, 113, 103, 112.5)]:
            c15.append(d.Candle(c15[-1].end+1, *ohlc, 100, d.M15))
        now = c15[-1].end+2
        row = engine.evaluate("BTC", {**quote, "bid": 112.49, "ask": 112.51, "asof_ms": now}, c4, c15, now)
        self.assertEqual(row["reclaim"]["status"], "ACTIVE PAPER TRADE")
        trade = restarted.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertTrue(trade["signal_key"].endswith(":"+str(c15[-1].t)))
        self.assertFalse(trade["signal_key"].endswith(":"+str(old_trigger)))

    def test_live_failed_break_reset_survives_restart_and_ignores_partial_bar(self):
        c4, c15, quote, now = reclaim_fixture()
        row = self.engine.evaluate("BTC", {**quote, "bid": 101.99, "ask": 102.01}, c4, c15, now)
        self.assertEqual(row["reclaim"]["status"], "WAITING FOR FRESH 15M CONFIRMATION")
        reset = self.store.snapshot()["assets"]["BTC"]["pending"]["confirmation_reset_ms"]
        self.assertEqual(reset, now)
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        engine = d.Engine(ASSETS, self.rules, restarted)
        engine.evaluate("BTC", {**quote, "asof_ms": now+15000}, c4, c15, now+15000)
        self.assertEqual(restarted.snapshot()["assets"]["BTC"]["trades"], [])
        # This candle started before the observed failure, so its apparent
        # touch and break cannot establish a new full post-failure sequence.
        c15.append(d.Candle(c15[-1].end+1, 103, 113, 102, 112.5, 100, d.M15))
        self.assertLess(c15[-1].t, reset)
        now = c15[-1].end+2
        engine.evaluate("BTC", {**quote, "bid": 112.49, "ask": 112.51, "asof_ms": now}, c4, c15, now)
        self.assertEqual(restarted.snapshot()["assets"]["BTC"]["trades"], [])
        for ohlc in [(112.5, 113, 101, 102), (102, 104, 101, 103), (103, 115, 102, 114)]:
            c15.append(d.Candle(c15[-1].end+1, *ohlc, 100, d.M15))
        now = c15[-1].end+2
        row = engine.evaluate("BTC", {**quote, "bid": 113.99, "ask": 114.01, "asof_ms": now}, c4, c15, now)
        self.assertEqual(row["reclaim"]["status"], "ACTIVE PAPER TRADE")

    def test_momentum_post_signal_stop_breach_is_consumed_after_recovery(self):
        c4, quote, now = momentum_fixture()
        source = d.momentum_scan(c4, self.rules)["signal"]
        c15 = bars(5, d.M15, source["end_ms"]+1-4*d.M15, 112)
        setbar(c15, 4, 112.3, 112.8, source["stop"]-1, 112.3)
        now = c15[-1].end+2
        row = self.engine.evaluate("BTC", {**quote, "asof_ms": now}, c4, c15, now)
        self.assertEqual(row["momentum"]["status"], "MOMENTUM SIGNAL INVALIDATED BY STOP")
        self.assertIn(source["key"], self.store.snapshot()["assets"]["BTC"]["consumed"])
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        d.Engine(ASSETS, self.rules, restarted).evaluate("BTC", {**quote, "asof_ms": now+15000}, c4, [], now+15000)
        self.assertEqual(restarted.snapshot()["assets"]["BTC"]["trades"], [])
        self.assertEqual(restarted.snapshot()["outbox"], [])

    def test_momentum_live_stop_breach_is_terminal_without_a_15m_feed(self):
        c4, quote, now = momentum_fixture()
        source = d.momentum_scan(c4, self.rules)["signal"]
        bid = source["stop"]-.1
        self.engine.evaluate("BTC", {**quote, "bid": bid, "ask": bid+.02}, c4, [], now)
        self.engine.evaluate("BTC", {**quote, "asof_ms": now+15000}, c4, [], now+15000)
        record = self.store.snapshot()["assets"]["BTC"]
        self.assertIn(source["key"], record["consumed"])
        self.assertEqual(record["trades"], [])

    def test_momentum_watch_survives_restart_and_observes_stop_during_4h_outage(self):
        c4, quote, now = momentum_fixture()
        source = d.momentum_scan(c4, self.rules)["signal"]
        self.engine.evaluate("BTC", {**quote, "ask": 114}, c4, [], now)
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["momentum_watch"]["key"], source["key"])
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        engine = d.Engine(ASSETS, self.rules, restarted)
        bid = source["stop"]-.1
        engine.evaluate("BTC", {**quote, "bid": bid, "ask": bid+.02, "asof_ms": now+15000}, [], [], now+15000)
        engine.evaluate("BTC", {**quote, "asof_ms": now+30000}, c4, [], now+30000)
        record = restarted.snapshot()["assets"]["BTC"]
        self.assertIn(source["key"], record["consumed"])
        self.assertIsNone(record["momentum_watch"])
        self.assertEqual(record["trades"], [])

    def test_momentum_does_not_count_pre_confirmation_lows_as_stop_breaches(self):
        c4, quote, now = momentum_fixture()
        source = d.momentum_scan(c4, self.rules)["signal"]
        c15 = bars(4, d.M15, source["end_ms"]+1-4*d.M15, 112)
        setbar(c15, 3, 112, 113, source["stop"]-1, 112.3)
        row = self.engine.evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(row["momentum"]["status"], "ACTIVE PAPER TRADE")


class UpgradeSafetyTests(TemporaryEngine):
    def write_previous_state(self, document):
        document["fingerprint"] = d.fingerprint(ASSETS, self.rules, d.LEGACY_ENGINE_VERSION)
        d.atomic_json(self.path, document)
        return self.path.read_bytes()

    def test_upgrade_rechecks_pending_setup_and_preserves_a_backup(self):
        c4, c15, quote, now = reclaim_fixture()
        early = c15[:9]
        observed = early[-1].end+2
        self.engine.evaluate("BTC", {**quote, "asof_ms": observed}, c4, early, observed)
        before = self.store.snapshot()
        self.assertIsNotNone(before["assets"]["BTC"]["pending"])
        original_bytes = self.write_previous_state(before)
        upgraded = d.StateStore(self.path, ASSETS, self.rules)
        state = upgraded.snapshot()
        self.assertIsNone(state["assets"]["BTC"]["pending"])
        self.assertEqual(state["cash"], before["cash"])
        self.assertEqual(state["assets"]["BTC"]["consumed"], before["assets"]["BTC"]["consumed"])
        self.assertEqual(state["fingerprint"], d.fingerprint(ASSETS, self.rules))
        from zipfile import ZipFile
        with ZipFile(self.path.parent/"backup"/"latest.zip") as backup:
            self.assertEqual(backup.read("state/"+self.path.name), original_bytes)
        # Requalification must use the new engine; another restart is a no-op.
        row = d.Engine(ASSETS, self.rules, upgraded).evaluate("BTC", quote, c4, c15, now)
        self.assertEqual(row["reclaim"]["status"], "ACTIVE PAPER TRADE")
        self.assertEqual(upgraded.snapshot()["assets"]["BTC"]["trades"][0]["engine"], d.ENGINE_VERSION)
        restarted = d.StateStore(self.path, ASSETS, self.rules)
        self.assertEqual(restarted.snapshot(), upgraded.snapshot())
        self.assertEqual(len(list((self.path.parent/"backup").iterdir())), 1)

    def test_upgrade_preserves_cash_trades_targets_and_delivery_queue(self):
        self.qualify()
        before = self.store.snapshot()
        for trade in before["assets"]["BTC"]["trades"]:
            trade.pop("engine")
        self.write_previous_state(before)
        upgraded = d.StateStore(self.path, ASSETS, self.rules).snapshot()
        for key in ("cash", "realized_pnl", "outbox"):
            self.assertEqual(upgraded[key], before[key])
        expected = copy.deepcopy(before["assets"]["BTC"])
        expected["trades"][0]["engine"] = d.LEGACY_ENGINE_VERSION
        self.assertEqual(upgraded["assets"]["BTC"], expected)

    def test_upgrade_rejects_malformed_pending_instead_of_silently_clearing_it(self):
        before = self.store.snapshot()
        before["assets"]["BTC"]["pending"] = {"key": "missing fields"}
        self.write_previous_state(before)
        upgraded = d.StateStore(self.path, ASSETS, self.rules).snapshot()
        self.assertIn("Previous state archived", upgraded["warnings"][0])
        self.assertEqual(len(list(self.path.parent.glob("*.pre-v9.3-*.json"))), 0)

    def test_changed_rules_do_not_enter_the_compatible_upgrade_path(self):
        self.qualify()
        self.write_previous_state(self.store.snapshot())
        updated_rules = replace(self.rules, reclaim_rvol_min=1.3)
        upgraded = d.StateStore(self.path, ASSETS, updated_rules).snapshot()
        self.assertIn("Previous state archived", upgraded["warnings"][0])
        self.assertEqual(len(list(self.path.parent.glob("*.pre-v9.3-*.json"))), 0)

    def test_failed_upgrade_write_keeps_original_ledger_intact(self):
        original_bytes = self.write_previous_state(self.store.snapshot())
        with patch("adaptive_crypto.ledger.atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                d.StateStore(self.path, ASSETS, self.rules)
        self.assertEqual(self.path.read_bytes(), original_bytes)
