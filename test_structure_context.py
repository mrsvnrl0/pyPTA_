"""Protected 4H structure: no hindsight, no wick exits, no position-alert replay."""
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adaptive_crypto.core import Candle, H4, Rules
from adaptive_crypto.notifications import dispatch_once
from adaptive_crypto.positions import PositionStore, validate_positions
from adaptive_crypto.position_structure import monitor_structure
from adaptive_crypto.structure_context import advance_structure, qualification_checks
from adaptive_crypto.state_paths import open_stores
import test_application_roles as application_tests

ASSETS = {"BTC": {"symbol": "BTC/USD", "allocation": 1.0}}
ROWS = [(98,96,97),(99,97,98),(100,98,99),(99,96,97),(98,95,96),
        (99,96,98),(100,97,99),(103,98,102),(102,98,100),(101,97,99),
        (102,98,101),(102,99,101),(100,94,96),(99,95,97),(98,94.5,95),
        (96,92,94),(95,91,93)]


def fixture(mirror=False):
    rows = [(200-l,200-h,200-c) for h,l,c in ROWS] if mirror else ROWS
    return [Candle((100+i)*H4,c,h,l,c,100,H4) for i,(h,l,c) in enumerate(rows)]


class StructureMathTests(unittest.TestCase):
    def reading(self, bars, previous=None):
        state, _ = advance_structure(bars, bars[-1].end+1, previous)
        self.assertIsNone(state.get("error"))
        return state

    def test_two_following_closed_bars_and_strict_wick_pivots(self):
        bars = fixture()
        initial = self.reading(bars[:5])
        self.assertEqual(initial["swing_high"]["price"],100)
        self.assertEqual(initial["swing_high"]["known_ms"],bars[4].end)
        self.assertIsNone(initial["swing_low"])
        self.assertIsNone(self.reading(bars[:6])["swing_low"])
        low = self.reading(bars[:7])["swing_low"]
        self.assertEqual((low["price"],low["known_ms"]),(95,bars[6].end))
        tied = [*bars[:4],replace(bars[4],h=100)]
        self.assertIsNone(self.reading(tied)["swing_high"])
        state,_=advance_structure(bars[:5],bars[4].end)
        self.assertIn("uncompleted",state["error"])

    def test_user_example_and_minor_pivots_do_not_replace_protected_low(self):
        bars=fixture()
        state=self.reading(bars[:8])
        self.assertEqual(state["last_break"]["level"],100)
        self.assertEqual(state["last_break"]["close"],102)
        self.assertEqual(state["protected_low"]["price"],95)
        state=self.reading(bars[:12],state)
        self.assertEqual(state["swing_low"]["price"],97)
        self.assertEqual(state["protected_low"]["price"],95)
        self.assertEqual(state["protected_low"]["protected_ms"],bars[7].end)

    def test_wick_sweep_and_equal_close_are_not_protected_breaks(self):
        bars=fixture()
        state=self.reading(bars[:13])
        self.assertEqual(state["last_break"]["direction"],"bearish")  # minor swing at 97
        self.assertIsNone(state["last_breach"])
        self.assertIsNone(state["protected_low"]["broken_ms"])
        self.assertIn({"kind":"protected_low","level":95},state["sweeps"])
        equal=self.reading(bars[:15],state)
        self.assertEqual(equal["close"],95)
        self.assertIsNone(equal["last_breach"])
        broken=self.reading(bars[:16],equal)
        self.assertEqual(broken["last_breach"]["level"],95)
        self.assertEqual(broken["last_breach"]["close"],94)

    def test_short_side_is_symmetric(self):
        bars=fixture(True)
        state=self.reading(bars[:12])
        self.assertEqual(state["protected_high"]["price"],105)
        self.assertEqual(state["swing_high"]["price"],103)
        self.assertIsNone(self.reading(bars[:15],state)["last_breach"])
        broken=self.reading(bars[:16],state)
        self.assertEqual(broken["last_breach"]["kind"],"protected_high")
        self.assertEqual(broken["last_breach"]["close"],106)

    def test_incremental_and_whole_history_results_match(self):
        bars=fixture()
        state=self.reading(bars[:5])
        for count in range(6,len(bars)+1):
            state=self.reading(bars[max(0,count-6):count],state)
            self.assertEqual(state,self.reading(bars[:count]))
        self.assertEqual(self.reading(bars[-6:],state),state)

    def test_protected_level_survives_origin_leaving_rolling_window(self):
        bars=fixture()[:12]
        state=self.reading(bars)
        flat=[Candle(bars[-1].t+(i+1)*H4,100,101,99,100,100,H4) for i in range(20)]
        stream=bars+flat
        for count in range(13,len(stream)+1):
            state=self.reading(stream[count-6:count],state)
        self.assertEqual(state["protected_low"]["price"],95)
        self.assertEqual(state["swing_low"]["price"],97)
        self.assertIsNone(state["last_breach"])

    def test_bad_feed_preserves_last_known_values_and_marks_unavailable(self):
        bars=fixture()
        old=self.reading(bars[:12])
        for invalid,now in [(bars[:12],bars[12].end+1),
                            (bars[:10]+bars[11:13],bars[12].end+1),
                            (bars[:13],bars[12].t+1000),
                            ([replace(b,interval=300000) for b in bars[:12]],bars[11].end+1)]:
            result,_=advance_structure(invalid,now,old)
            self.assertTrue(result["error"])
            self.assertEqual(result["protected_low"],old["protected_low"])
            self.assertEqual(result["expires_ms"],now)

    def test_context_qualifications_report_actual_levels_and_breach(self):
        before=self.reading(fixture()[:12])
        checks=qualification_checks(before,"long")
        self.assertEqual([c["status"] for c in checks],["pass","pass"])
        self.assertEqual(checks[1]["measured"]["protected_level"],95)
        after=self.reading(fixture()[:16])
        self.assertEqual(qualification_checks(after,"long")[1]["status"],"wait")
        self.assertTrue(all("does not change" in c["note"] for c in checks))


class StructureAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=PositionStore(Path(self.temp.name)/"positions.json",market_mode="margin")
        self.bars=fixture()
        self.now=self.bars[11].end+1
        self.long,_=self.store.open_position(ASSETS,"BTC","long",100,1,uuid.uuid4().hex,self.now,
                                           position_type="margin_long",target_mode="nn")
        self.short,_=self.store.open_position(ASSETS,"BTC","short",100,1,uuid.uuid4().hex,self.now,
                                             position_type="margin_short")
        self.monitor(self.bars[:12])

    def monitor(self,bars,**kwargs):
        return self.store.transaction(lambda doc:monitor_structure(doc,"BTC","BTC/USD",bars,bars[-1].end+1,"margin",**kwargs))

    def events(self):
        return [e for e in self.store.snapshot()["outbox"] if e.get("alert_type")=="position_structure"]

    def test_only_adverse_position_alert_not_minor_structure_break_or_wick(self):
        self.monitor(self.bars[:13])
        self.monitor(self.bars[:15])
        self.assertEqual(self.events(),[])
        self.monitor(self.bars[:16])
        events=self.events()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["position_ids"],[self.long["id"]])
        self.assertIn("Recorded entry price: $100.00",events[0]["text"])
        self.assertIn("Protected level: $95.00",events[0]["text"])
        self.assertIn("Completed 4H close: $94.00",events[0]["text"])
        self.assertEqual(events[0]["payload"]["action"],"review")
        self.assertEqual([p["status"] for p in self.store.snapshot()["positions"]],["open","open"])
        validate_positions(self.store.snapshot())

    def test_repeated_scans_and_restart_do_not_replay(self):
        self.monitor(self.bars[:16])
        self.monitor(self.bars[:16])
        self.store=PositionStore(self.store.path,market_mode="margin")
        self.monitor(self.bars[:16],allow_alerts=False)
        self.monitor(self.bars[:17])
        self.assertEqual(len(self.events()),1)
        self.assertEqual(self.events()[0]["status"],"cancelled")
        self.assertEqual(self.store.snapshot()["market_structure"]["BTC"]["protected_low"]["broken_ms"],self.bars[15].end)

    def test_no_history_alerts_when_added_after_break_or_monitor_first_starts(self):
        self.store.transaction(lambda doc: doc.pop("market_structure"))
        self.monitor(self.bars[:16])
        self.assertEqual(self.events(),[])
        self.store.open_position(ASSETS,"BTC","long",94,1,uuid.uuid4().hex,self.bars[15].end+1)
        self.monitor(self.bars[:17])
        self.assertEqual(self.events(),[])

    def test_newly_recorded_position_does_not_inherit_an_earlier_break(self):
        self.store.set_alerts(self.long["id"],False,True,self.now)
        late,_=self.store.open_position(ASSETS,"BTC","long",94,1,uuid.uuid4().hex,self.bars[15].end+1)
        self.monitor(self.bars[:16])
        self.assertEqual(self.events(),[])

    def test_off_or_closed_positions_and_reenabled_after_break_do_not_alert(self):
        self.store.set_alerts(self.long["id"],False,True,self.now)
        self.monitor(self.bars[:16])
        self.store.set_alerts(self.long["id"],True,True,self.bars[15].end+2)
        self.monitor(self.bars[:16])
        self.assertEqual(self.events(),[])

    def test_queued_alert_is_cancelled_when_preference_changes(self):
        self.monitor(self.bars[:16])
        self.store.set_alerts(self.long["id"],False,True,self.bars[15].end+2)
        self.assertEqual(self.events()[0]["status"],"cancelled")

    def test_close_cancels_queued_alert(self):
        self.monitor(self.bars[:16])
        self.store.close_position(self.long["id"],94,self.bars[15].end+2)
        self.assertEqual(self.events()[0]["status"],"cancelled")

    def test_gap_or_clock_failure_never_emits_false_exit(self):
        self.monitor(self.bars[:16],error="Clock unavailable")
        self.assertEqual(self.events(),[])
        far=[replace(b,t=b.t+100*H4) for b in self.bars]
        self.monitor(far)
        self.assertEqual(self.events(),[])

    def test_startup_baseline_suppresses_break_during_downtime(self):
        self.monitor(self.bars[:16],allow_alerts=False)
        self.monitor(self.bars[:16])
        self.assertEqual(self.events(),[])

    def test_delivery_once_and_expiry_use_mock_sender(self):
        self.monitor(self.bars[:16])
        sent=[]
        now=self.bars[15].end+1
        sender=lambda event: (sent.append(event["id"]) or {"status":"sent"})
        self.assertTrue(dispatch_once(self.store,"telegram",sender,now))
        self.assertFalse(dispatch_once(self.store,"telegram",sender,now+1000))
        self.assertEqual(len(sent),1)

    def test_expired_event_is_not_delivered(self):
        self.monitor(self.bars[:16])
        def forbidden(event):
            raise AssertionError("Expired alert must not be sent")
        self.assertFalse(dispatch_once(self.store,"telegram",forbidden,self.bars[15].end+300002))
        self.assertEqual(self.events()[0]["status"],"cancelled")

    def test_favourable_break_never_alerts_short_but_adverse_high_break_does(self):
        self.store.transaction(lambda doc:doc.pop("market_structure"))
        self.bars=fixture(True)
        self.monitor(self.bars[:12])
        self.monitor(self.bars[:13])
        self.assertEqual(self.events(),[])
        self.monitor(self.bars[:16])
        self.assertEqual(len(self.events()),1)
        self.assertEqual(self.events()[0]["position_ids"],[self.short["id"]])

    def test_sqlite_preserves_structure_across_reopen(self):
        root=Path(self.temp.name)/"sqlite.json"
        rules=Rules(strategy_model="neural_network")
        with open_stores(root,ASSETS,rules,"sqlite") as (_,positions):
            positions.transaction(lambda doc:monitor_structure(doc,"BTC","BTC/USD",self.bars[:12],self.now))
            expected=positions.snapshot()["market_structure"]
        with open_stores(root,ASSETS,rules,"sqlite") as (_,positions):
            self.assertEqual(positions.snapshot()["market_structure"],expected)


class StructureRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture_case=application_tests.ApplicationRolesTests()
        fixture_case.setUp()
        self.addCleanup(fixture_case.doCleanups)
        self.case=fixture_case
        self.runtime=fixture_case.runtime
        self.bars=fixture()
        self.runtime.paused=True

    def scan(self,count):
        bars=self.bars[:count]
        self.case.now=bars[-1].end+1
        self.runtime.provider.now.return_value=self.case.now
        self.runtime.provider.candles.side_effect=lambda symbol,interval,now:bars if interval==H4 else []
        self.runtime.scan_once()

    def test_4h_survives_smc_feed_failure_and_does_not_run_paper(self):
        self.scan(12)
        context=self.runtime.market["BTC"]["structure"]
        self.assertEqual(context["protected_low"]["price"],95)
        self.assertEqual(context["swing_low"]["price"],97)
        self.assertTrue(self.runtime.market["BTC"]["error"])
        self.assertEqual(self.runtime.position_errors,{})
        with patch("time.time",return_value=self.case.now/1000):
            html=self.case.client.get("/").text
        self.assertIn("4H swing structure",html)
        self.assertIn("Latest confirmed swing high",html)
        self.assertIn("Protected low",html)
        self.assertIn("4H protected low intact",html)
        self.assertIn("Qualification measurements",html)

    def test_active_runtime_only_alerts_protected_break_for_recorded_position(self):
        self.scan(12)
        p,_=self.runtime.positions.open_position(ASSETS,"BTC","long",100,1,uuid.uuid4().hex,self.case.now)
        self.scan(13)
        self.assertFalse(self.runtime.positions.snapshot()["outbox"])
        self.scan(16)
        events=self.runtime.positions.snapshot()["outbox"]
        self.assertEqual([e["alert_type"] for e in events],["position_structure"])
        self.assertEqual(events[0]["position_ids"],[p["id"]])
        with patch("time.time",return_value=self.case.now/1000):
            html=self.case.client.get("/positions").text
        self.assertIn("4H protected low",html)
        self.assertIn("BROKEN BY A COMPLETED CLOSE",html)
        self.assertIn("Structure / NN alerts",html)


if __name__=="__main__":
    unittest.main()
