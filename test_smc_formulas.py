"""Deterministic closed-candle examples for the video's mirrored SMC rules."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from adaptive_crypto.core import Candle, M5, M30, Rules, check
from adaptive_crypto.smc import (
    fair_value_gaps, higher_setups, lower_entries, opposing_liquidity,
    scan, structure_readings, swings,
)


def bars(rows, interval=M5):
    return [Candle(i * interval, o, h, l, c, 100, interval)
            for i, (o, h, l, c) in enumerate(rows)]


def mirror(candles, axis=250):
    return [replace(b, o=axis-b.o, h=axis-b.l, l=axis-b.h, c=axis-b.c)
            for b in candles]


def high_fixture():
    return bars([
        (110, 112, 108, 110), (110, 115, 109, 112),
        (112, 113, 100, 105), (105, 110, 104, 109),
        (109, 114, 106, 112), (112, 113, 105, 106),
        (106, 108, 98, 101), (110, 116, 109, 115),
    ], M30)


def low_fixture(aggressive=False):
    rows = [
        (120, 121, 119, 120), (120, 123, 119, 122),
        (122, 122, 115, 116), (116, 118, 111, 112),
        (112, 114, 107, 108), (108, 113, 108, 112),
        (112, 116, 110, 114),
    ]
    if aggressive:
        rows += [(114, 115, 111, 112), (107, 108, 100, 102),
                 (102, 112, 101, 110), (110, 112, 109, 111)]
    else:
        rows += [(114, 114, 100, 101), (101, 118, 101, 117),
                 (118, 121, 117, 120)]
    return bars(rows)


def setup(side="long"):
    lo, hi = (100, 105) if side == "long" else (145, 150)
    return {"key": "fixture:"+side, "side": side, "bos_end": 6*M5-1,
            "bos_ms": 5*M5, "ob_ms": 0, "zone_low": lo, "zone_high": hi,
            "checks": [check("sweep", "Sweep", True), check("bos", "BOS", True),
                       check("ob", "Order block", True)]}


def full_fixture(side="long"):
    """Matching 30M/5M OHLC with a live conservative limit and a $140 target."""
    high = bars([(120, 125, 119, 122), (122, 140, 120, 125),
                 (125, 126, 110, 115)], M30) + high_fixture()
    high = [replace(b, t=i*M30) for i, b in enumerate(high)]
    low = [Candle(b.t+j*M5, b.o, b.h, b.l, b.c, b.v/6, M5)
           for b in high[:-1] for j in range(6)]
    tail = bars([(110, 112, 109, 111), (111, 113, 110, 112),
                 (112, 115, 111, 114), (114, 116, 112, 113),
                 (113, 114, 110, 111), (111, 115, 111, 115),
                 (115, 116, 110, 114), (114, 114, 100, 101),
                 (101, 118, 101, 117), (118, 121, 117, 120)])
    low.extend(replace(b, t=high[-1].t+b.t) for b in tail)
    return (high, low) if side == "long" else (mirror(high), mirror(low))


class SMCFormulaTests(unittest.TestCase):
    def setUp(self):
        self.rules = Rules(strategy_model="smc_video", smc_pivot_strength=1,
                           smc_stop_buffer_bps=10)

    def test_confirmed_pivots_only_become_available_after_the_right_candle(self):
        candles = low_fixture()
        point = next(p for p in swings(candles, 1) if p["index"] == 6 and p["high"])
        self.assertEqual(point["known_ms"], candles[7].end)
        self.assertFalse(any(p["index"] == 6 for p in swings(candles[:7], 1)))
        readings = structure_readings(candles, 1)
        self.assertEqual(next(r for r in readings if r["bar_ms"] == candles[8].t)["event"]["level"], 116)

    def test_future_bars_cannot_change_previous_structure_readings(self):
        candles = low_fixture()
        full = structure_readings(candles, 1)
        for count in range(4, len(candles)+1):
            self.assertEqual(structure_readings(candles[:count], 1),
                             [r for r in full if r["bar_ms"] <= candles[count-1].t])

    def test_structure_uses_strict_closes_and_persists_between_breaks(self):
        candles = low_fixture()
        candles[8] = replace(candles[8], c=116)
        readings = structure_readings(candles, 1)
        self.assertIsNone(next(r for r in readings if r["bar_ms"] == candles[8].t)["event"])
        self.assertEqual(readings[-1]["direction"], "bullish")
        candles.append(Candle(10*M5, 120, 121, 118, 119, 100, M5))
        self.assertEqual(structure_readings(candles, 1)[-1]["direction"], "bullish")
        self.assertIsNone(structure_readings(candles, 1)[-1]["event"])

    def test_directional_structure_breaks_are_mirrored(self):
        bullish = structure_readings(low_fixture(), 1)[-1]
        bearish = structure_readings(mirror(low_fixture()), 1)[-1]
        self.assertEqual((bullish["direction"], bearish["direction"]), ("bullish", "bearish"))
        self.assertEqual(bullish["break_level"]+bearish["break_level"], 250)
        self.assertEqual(bullish["break_ms"], bearish["break_ms"])

    def test_fvg_uses_first_and_third_wicks_with_a_strict_gap(self):
        candles = bars([(101, 103, 100, 102), (102, 108, 101, 107), (106, 109, 105, 108)])
        self.assertEqual([(g["low"], g["high"], g["direction"]) for g in fair_value_gaps(candles)],
                         [(103, 105, "bullish")])
        self.assertEqual(fair_value_gaps([*candles[:2], replace(candles[2], l=103)]), [])
        inverse = fair_value_gaps(mirror(candles))[0]
        self.assertEqual((inverse["low"], inverse["high"], inverse["direction"]), (145, 147, "bearish"))

    def test_higher_setup_sweep_return_bos_order_block_and_symmetry(self):
        bullish = higher_setups(high_fixture(), self.rules, "long")
        bearish = higher_setups(mirror(high_fixture()), self.rules, "short")
        self.assertEqual((len(bullish), len(bearish)), (1, 1))
        long, short = bullish[0], bearish[0]
        self.assertEqual((long["zone_low"], long["zone_high"]), (98, 108))
        self.assertEqual((short["zone_low"], short["zone_high"]), (142, 152))
        self.assertEqual(long["bos_end"], high_fixture()[-1].end)
        self.assertEqual(long["liquidity"]+short["liquidity"], 250)

    def test_equal_liquidity_touch_is_not_a_sweep(self):
        candles = high_fixture()
        candles[6] = replace(candles[6], l=100)
        self.assertEqual(higher_setups(candles, self.rules, "long"), [])

    def test_return_may_take_two_subsequent_setup_candles(self):
        candles = high_fixture()[:6] + bars([
            (106, 108, 98, 99), (99, 103, 97, 99),
            (99, 109, 98, 105), (110, 116, 109, 115),
        ], M30)
        candles = [replace(b, t=i*M30) for i, b in enumerate(candles)]
        self.assertEqual(len(higher_setups(candles, self.rules, "long")), 1)
        self.assertEqual(higher_setups(candles, replace(self.rules, smc_reversal_bars=1), "long"), [])
        self.assertEqual(len(higher_setups(mirror(candles), self.rules, "short")), 1)

    def test_bos_requires_close_beyond_structure_not_wick_or_equal_close(self):
        candles = high_fixture()
        for close in (113, 114):
            candles[7] = replace(candles[7], c=close)
            self.assertEqual(higher_setups(candles, self.rules, "long"), [])

    def test_order_block_mitigated_before_bos_is_not_fresh(self):
        candles = high_fixture()[:7] + bars([
            (108, 113, 106, 109), (109, 112, 107, 111), (111, 116, 110, 115),
        ], M30)
        candles = [replace(b, t=i*M30) for i, b in enumerate(candles)]
        self.assertEqual(higher_setups(candles, self.rules, "long"), [])
        self.assertEqual(higher_setups(mirror(candles), self.rules, "short"), [])

    def test_deeper_extreme_after_return_invalidates_that_sweep(self):
        candles = high_fixture()
        candles[7] = replace(candles[7], l=97)
        self.assertEqual(higher_setups(candles, self.rules, "long"), [])

    def test_structure_already_closed_through_before_sweep_cannot_confirm_bos(self):
        candles = high_fixture()
        candles.insert(6, Candle(0, 112, 116, 106, 115, 100, M30))
        candles = [replace(b, t=i*M30) for i, b in enumerate(candles)]
        self.assertEqual(higher_setups(candles, self.rules, "long"), [])
        self.assertEqual(higher_setups(mirror(candles), self.rules, "short"), [])

    def test_first_post_bos_revisit_is_required(self):
        result = lower_entries(low_fixture()[:7], setup(), self.rules)
        self.assertIn("ORDER-BLOCK REVISIT", result["status"])
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["checks"][-1]["status"], "wait")

    def test_failed_first_visit_cannot_restart_on_second_touch_and_conservative_confirmation(self):
        # 7 touches; 8 still overlaps; 9 lies wholly outside the block; 10
        # overlaps again. Its equal low preserves the OLD origin index, so
        # checking only the MSS origin would incorrectly allow this retry.
        candles = low_fixture()[:8] + bars([
            (101, 112, 101, 110), (110, 113, 109, 112),
            (112, 114, 100, 101), (101, 118, 101, 117),
            (118, 121, 117, 120),
        ])
        candles = [replace(b, t=i*M5) for i, b in enumerate(candles)]
        rules = replace(self.rules, smc_entry_method="conservative")
        for side, rows in (("long", candles), ("short", mirror(candles))):
            result = lower_entries(rows, setup(side), rules)
            self.assertEqual(result["entries"], [])
            self.assertIn("FIRST VISIT CONFIRMATION MISSED", result["status"])

    def test_second_visit_cannot_late_invert_a_gap_left_by_the_first_visit(self):
        candles = low_fixture(True)[:10] + bars([
            (108, 109, 106, 108),  # Entire range departs; opposing gap not inverted.
            (105, 106, 99, 101), (101, 113, 100, 112),
        ])
        candles = [replace(b, t=i*M5) for i, b in enumerate(candles)]
        rules = replace(self.rules, smc_entry_method="aggressive")
        for side, rows in (("long", candles), ("short", mirror(candles))):
            result = lower_entries(rows, setup(side), rules)
            self.assertEqual(result["entries"], [])
            self.assertIn("FIRST VISIT CONFIRMATION MISSED", result["status"])

    def test_first_visit_may_span_several_overlapping_candles_before_confirmation(self):
        candles = low_fixture()[:8] + bars([
            (101, 111, 100.5, 110), (110, 112, 101, 111),
            (111, 118, 102, 117), (118, 121, 117, 120),
        ])
        candles = [replace(b, t=i*M5) for i, b in enumerate(candles)]
        rules = replace(self.rules, smc_entry_method="conservative")
        for side, rows in (("long", candles), ("short", mirror(candles))):
            entry, = lower_entries(rows, setup(side), rules)["entries"]
            self.assertEqual(entry["signal_ms"], candles[-1].t)

    def test_second_overlap_keeps_an_already_confirmed_untouched_limit(self):
        candles = low_fixture()
        candles.append(Candle(10*M5, 120, 120, 115.75, 119, 100, M5))
        # A wider OB is revisited at 115.75, but its already confirmed 115.5
        # limit is untouched. The normal midpoint check owns its freshness.
        for side, rows, zone in (("long", candles, (100, 116)),
                                  ("short", mirror(candles), (134, 150))):
            poi = {**setup(side), "zone_low": zone[0], "zone_high": zone[1]}
            entry, = lower_entries(rows, poi, replace(self.rules, smc_entry_method="conservative"))["entries"]
            self.assertEqual(entry["signal_ms"], candles[9].t)

    def test_incomplete_history_cannot_guess_the_first_revisit(self):
        result = lower_entries(low_fixture()[7:], setup(), self.rules)
        self.assertIn("COMPLETE ENTRY-TIMEFRAME HISTORY", result["status"])
        self.assertEqual(result["entries"], [])

    def test_conservative_entry_requires_mss_and_uses_gap_midpoint(self):
        rules = replace(self.rules, smc_entry_method="conservative")
        self.assertEqual(lower_entries(low_fixture()[:9], setup(), rules)["entries"], [])
        entry, = lower_entries(low_fixture(), setup(), rules)["entries"]
        self.assertEqual((entry["method"], entry["limit"], entry["swing"]), ("conservative", 115.5, 100))
        self.assertEqual(entry["signal_end"], low_fixture()[-1].end)
        self.assertEqual(next(c for c in entry["checks"] if c["key"] == "smc_mss")["status"], "pass")

    def test_conservative_entry_is_symmetric(self):
        rules = replace(self.rules, smc_entry_method="conservative")
        long, = lower_entries(low_fixture(), setup(), rules)["entries"]
        short, = lower_entries(mirror(low_fixture()), setup("short"), rules)["entries"]
        self.assertEqual(long["limit"]+short["limit"], 250)
        self.assertEqual(long["swing"]+short["swing"], 250)
        self.assertEqual(long["signal_end"], short["signal_end"])

    def test_gap_retested_before_mss_cannot_be_filled_retroactively(self):
        candles = low_fixture()
        candles[6] = replace(candles[6], h=125)
        candles += bars([(120, 121, 115, 119), (119, 127, 119, 126)])
        candles = [replace(b, t=i*M5) for i, b in enumerate(candles)]
        rules = replace(self.rules, smc_entry_method="conservative")
        self.assertEqual(lower_entries(candles, setup(), rules)["entries"], [])

    def test_multiple_structure_breaks_cannot_duplicate_one_gap_entry(self):
        candles = low_fixture() + bars([
            (120, 120, 117, 118), (118, 122, 118, 121), (121, 126, 122, 125),
        ])
        # Keep bounds valid on the displacement candle.
        candles[-1] = replace(candles[-1], o=123)
        candles = [replace(b, t=i*M5) for i, b in enumerate(candles)]
        entries = lower_entries(candles, setup(), self.rules)["entries"]
        self.assertEqual(len(entries), len({e["key"] for e in entries}))
        newest = next(e for e in entries if e["signal_ms"] == candles[-1].t)
        mss = next(c for c in newest["checks"] if c["key"] == "smc_mss")
        self.assertEqual(mss["candle_ms"], candles[-1].t)

    def test_opposite_structure_break_separates_later_gap_from_original_mss_leg(self):
        candles = low_fixture() + bars([
            (120, 120, 116.8, 118), (118, 126, 118, 124),
            (124, 125, 116.5, 116.7), (117, 125, 117, 123),
            (125.5, 126, 125.5, 125.8),
        ])
        candles = [replace(b, t=i*M5) for i, b in enumerate(candles)]
        readings = structure_readings(candles, 1)
        opposite = next(r for r in readings if r["bar_ms"] == candles[12].t)
        self.assertEqual(opposite["event"]["direction"], "bearish")
        entries = lower_entries(candles, setup(), replace(self.rules, smc_entry_method="conservative"))["entries"]
        self.assertTrue(entries)  # The first, still untouched gap remains valid.
        self.assertFalse(any(e["signal_ms"] == candles[14].t for e in entries))

    def test_reversal_origin_breach_separates_later_gap_from_original_mss_leg(self):
        candles = low_fixture() + bars([(120, 120.5, 99, 119), (123, 124, 122, 123)])
        candles = [replace(b, t=i*M5) for i, b in enumerate(candles)]
        self.assertEqual(lower_entries(candles, setup(), replace(self.rules, smc_entry_method="conservative"))["entries"], [])

    def test_aggressive_ifvg_requires_far_edge_close_and_mss_is_optional(self):
        rules = replace(self.rules, smc_entry_method="aggressive")
        candles = low_fixture(aggressive=True)
        self.assertEqual(lower_entries(candles[:10], setup(), rules)["entries"], [])
        entry, = lower_entries(candles, setup(), rules)["entries"]
        self.assertEqual((entry["method"], entry["limit"], entry["swing"]), ("aggressive", 109, 100))
        confluence = next(c for c in entry["checks"] if c["key"] == "smc_mss_context")
        self.assertEqual(confluence["status"], "context")
        self.assertFalse(confluence["measured"])

    def test_aggressive_entry_is_symmetric(self):
        rules = replace(self.rules, smc_entry_method="aggressive")
        long, = lower_entries(low_fixture(True), setup(), rules)["entries"]
        short, = lower_entries(mirror(low_fixture(True)), setup("short"), rules)["entries"]
        self.assertEqual(long["limit"]+short["limit"], 250)
        self.assertEqual(long["swing"]+short["swing"], 250)

    def test_aggressive_entry_reports_simultaneous_mss_as_confluence(self):
        candles = low_fixture(True)
        candles[-1] = replace(candles[-1], h=118, c=117)
        entry, = lower_entries(candles, setup(), replace(self.rules, smc_entry_method="aggressive"))["entries"]
        context = next(c for c in entry["checks"] if c["key"] == "smc_mss_context")
        self.assertTrue(context["measured"])
        self.assertEqual(context["status"], "context")

    def test_missed_midpoint_limit_is_not_backdated(self):
        for aggressive in (False, True):
            candles = low_fixture(aggressive)
            midpoint = 109 if aggressive else 115.5
            candles.append(Candle(len(candles)*M5, midpoint+1, midpoint+2, midpoint,
                                  midpoint+1, 100, M5))
            method = "aggressive" if aggressive else "conservative"
            self.assertEqual(lower_entries(candles, setup(), replace(self.rules, smc_entry_method=method))["entries"], [])

    def test_invalidated_reversal_cannot_offer_a_limit(self):
        candles = low_fixture()
        candles.append(Candle(len(candles)*M5, 120, 121, 99, 119, 100, M5))
        self.assertEqual(lower_entries(candles, setup(), self.rules)["entries"], [])

    def test_target_is_nearest_untaken_confirmed_opposing_swing(self):
        candles = bars([
            (119, 120, 118, 119), (128, 140, 125, 130), (125, 130, 120, 125),
            (126, 135, 121, 128), (124, 128, 120, 124), (125, 130, 122, 126),
            (123, 125, 121, 123), (123, 130, 122, 124), (123, 124, 121, 122),
        ], M30)
        self.assertEqual(opposing_liquidity(candles, 1, "long", 110, candles[6].end)["price"], 130)
        self.assertEqual(opposing_liquidity(candles, 1, "long", 110, candles[7].end)["price"], 135)
        self.assertEqual(opposing_liquidity(mirror(candles), 1, "short", 140, candles[7].end)["price"], 115)
        self.assertIsNone(opposing_liquidity(candles, 1, "long", 141, candles[-1].end))

    def test_lower_candles_can_consume_target_before_enclosing_higher_bar_closes(self):
        candles = bars([
            (119, 120, 118, 119), (128, 140, 125, 130), (125, 130, 120, 125),
            (126, 135, 121, 128), (124, 128, 120, 124), (125, 130, 122, 126),
            (123, 125, 121, 123),
        ], M30)
        lower = [Candle(42*M5, 124, 131, 123, 125, 100, M5)]
        self.assertEqual(opposing_liquidity(candles, 1, "long", 110, lower[-1].end, lower)["price"], 135)
        # A still-forming lower candle must not consume anything yet.
        self.assertEqual(opposing_liquidity(candles, 1, "long", 110, lower[-1].t, lower)["price"], 130)
        self.assertEqual(opposing_liquidity(mirror(candles), 1, "short", 140,
                                           lower[-1].end, mirror(lower))["price"], 115)

    def test_full_synchronized_scan_qualifies_both_directions(self):
        for side, target in (("long", 140), ("short", 110)):
            high, low = full_fixture(side)
            result = scan(high, low, self.rules, side)
            entry, = result["entries"]
            self.assertEqual(entry["target_liquidity_price"], target)
            self.assertAlmostEqual(entry["target"], target*(1.0001 if side == "long" else .9999))
            self.assertEqual(entry["method"], "conservative")
            self.assertIn("QUALIFIED", result["status"])

    def test_full_fixture_contains_matching_higher_and_lower_ohlc(self):
        high, low = full_fixture()
        for candle in high:
            children = [b for b in low if candle.t <= b.t <= candle.end]
            self.assertEqual(len(children), 6)
            self.assertEqual((children[0].o, max(b.h for b in children),
                              min(b.l for b in children), children[-1].c),
                             (candle.o, candle.h, candle.l, candle.c))

    def test_target_taken_after_confirmation_expires_an_unfilled_candidate(self):
        high, low = full_fixture()
        low.append(Candle(low[-1].end+1, 120, 140, 118, 120, 100, M5))
        result = scan(high, low, self.rules, "long")
        self.assertEqual(result["entries"], [])
        self.assertIn("TARGET LIQUIDITY ALREADY TAKEN", result["status"])

    def test_legacy_indicator_gates_do_not_change_video_qualification(self):
        high, low = full_fixture()
        stricter = replace(self.rules, sweep_atr=50, reclaim_rvol_min=100,
                           bearish_body_atr=100, reclaim_body_atr=100)
        self.assertEqual(scan(high, low, self.rules, "long"), scan(high, low, stricter, "long"))

    def test_future_setup_bars_cannot_change_an_already_observable_bos(self):
        high, _ = full_fixture()
        all_setups = higher_setups(high, self.rules, "long")
        for count in range(1, len(high)+1):
            self.assertEqual(higher_setups(high[:count], self.rules, "long"),
                             [s for s in all_setups if s["bos_end"] <= high[count-1].end])

    def test_scan_applies_selected_swing_buffer_and_liquidity_target(self):
        # Entry/target isolation verifies the stop arithmetic without a second
        # synthetic series pretending to be aggregated from the first one.
        for side, candles, target in (("long", low_fixture(), 130),
                                       ("short", mirror(low_fixture()), 120)):
            with patch("adaptive_crypto.smc.higher_setups", return_value=[setup(side)]), \
                 patch("adaptive_crypto.smc.opposing_liquidity", return_value={"price": target, "bar_ms": 0}):
                result = scan([], candles, self.rules, side)
            entry, = result["entries"]
            expected = 99.9 if side == "long" else 150.15
            self.assertAlmostEqual(entry["stop"], expected)
            self.assertEqual(entry["target_liquidity_price"], target)
            expected_tp = target*(1.0001 if side == "long" else .9999)
            self.assertAlmostEqual(entry["target"], expected_tp)
            self.assertAlmostEqual(entry["gross_r"], abs(expected_tp-entry["limit"])/abs(entry["limit"]-expected))

    def test_scan_waits_if_no_untaken_opposing_target_exists(self):
        with patch("adaptive_crypto.smc.higher_setups", return_value=[setup()]), \
             patch("adaptive_crypto.smc.opposing_liquidity", return_value=None):
            result = scan([], low_fixture(), self.rules, "long")
        self.assertEqual(result["entries"], [])
        self.assertIn("OPPOSING HIGHER-TIMEFRAME LIQUIDITY", result["status"])

    def test_consumed_order_block_cannot_offer_another_position(self):
        with patch("adaptive_crypto.smc.higher_setups", return_value=[setup()]):
            result = scan([], low_fixture(), self.rules, "long", consumed={setup()["key"]})
        self.assertEqual(result["entries"], [])


if __name__ == "__main__":
    unittest.main()
