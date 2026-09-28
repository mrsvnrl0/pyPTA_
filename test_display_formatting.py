"""Offline regressions for human-readable dashboard qualification evidence.

Run alongside the engine suite with: python -m unittest -v
"""
import copy
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
import unittest
from unittest.mock import Mock, patch

import requests

import adaptive_crypto_dashboard as d
from test_dashboard import ASSETS, TemporaryEngine, reclaim_fixture


EXAMPLE_MEASURED = {
    "body_atr": 1.78348,
    "close_location": 0.29032,
    "close": 79421.1,
    "rvol": 2.71342,
}
EXAMPLE_REQUIRED = {
    "body_atr_min": 0.8,
    "close_location_max": 0.3,
    "close_below": 80650.1,
    "rvol_min": "context only",
}
CANDLE_MS = int(datetime(2026, 9, 4, 12, tzinfo=timezone.utc).timestamp() * 1000)


def example_check():
    return d.check("block", "Bearish close through last bullish block", True,
                   copy.deepcopy(EXAMPLE_MEASURED), copy.deepcopy(EXAMPLE_REQUIRED), CANDLE_MS)


def compact(text):
    return re.sub(r"\s+", "", text)


class DashboardHTML(HTMLParser):
    """Check nesting and collect visible text without CSS or script content."""

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []
        self.tags = []
        self.visible = []
        self.checks = []
        self.feed(html)
        self.close()
        if self.stack:
            self.errors.append("Unclosed tags: " + repr(self.stack))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, attrs.get("class", "").split()))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            self.errors.append("Unexpected closing tag: " + tag)
        else:
            self.stack.pop()

    def handle_data(self, data):
        if not any(tag in {"style", "script"} for tag, _ in self.stack):
            self.visible.append(data)
            if any("checks" in classes for _, classes in self.stack):
                self.checks.append(data)

    @property
    def text(self):
        return " ".join(self.visible)

    @property
    def check_text(self):
        return " ".join(self.checks)


class MeasurementFormattingTests(unittest.TestCase):
    def rows(self, check, required=False):
        result = d.measurement_rows(check, required=required, places=2)
        self.assertIsInstance(result, list)
        for label, value in result:
            self.assertIsInstance(label, str)
            self.assertIsInstance(value, str)
            self.assertNotIn("_", label)
        return result

    def assert_values(self, rows, expected):
        self.assertEqual([compact(value) for _, value in rows], [compact(value) for value in expected])

    def test_reported_block_displays_units_and_comparisons(self):
        check = example_check()
        original = copy.deepcopy(check)
        self.assert_values(self.rows(check), ["1.78 ATR", "29.03%", "$79,421.10", "2.71×"])
        required = self.rows(check, required=True)
        self.assert_values(required[:3], ["≥ 0.80 ATR", "≤ 30.00%", "< $80,650.10"])
        self.assertEqual(required[3][1].casefold(), "context only")
        self.assertEqual(check, original, "Display formatting must preserve source evidence")

    def test_required_dictionary_suffixes_match_the_actual_inequalities(self):
        check = d.check("sweep", "Liquidity sweep", True,
                        {"low": 78000.25, "close": 79421.1},
                        {"low_below": 79000.5, "close_above": 79100.5})
        self.assert_values(self.rows(check, required=True), ["< $79,000.50", "> $79,100.50"])

    def test_btc_sweep_displays_prices_and_depth_in_matching_units(self):
        check = d.check("sweep", "Liquidity sweep", False,
                        {"low": 79762.6, "close": 79919.4, "sweep_depth_atr": -4.949087107752897,
                         "liquidity_low": 76236.9, "previous_atr": 712.3940078720567},
                        {"low_below": 75880.70299606396, "close_above": 76236.9,
                         "sweep_depth_atr_above": 0.5})
        self.assertEqual(self.rows(check), [
            ("Low price", "$79,762.60"), ("Close price", "$79,919.40"),
            ("Sweep depth", "-4.95 ATR"), ("Liquidity low", "$76,236.90"),
            ("Previous 4H ATR", "$712.39")])
        self.assertEqual(self.rows(check, required=True), [
            ("Low price", "< $75,880.70"), ("Close price", "> $76,236.90"),
            ("Sweep depth", "> 0.50 ATR")])

    def test_scalar_checks_retain_their_units_and_threshold_semantics(self):
        cases = [
            ("body", 1.78348, 0.8, "1.78 ATR", "≥ 0.80 ATR"),
            ("close", 0.79032, 0.7, "79.03%", "≥ 70.00%"),
            ("volume", 2.71342, 1.2, "2.71×", "≥ 1.20×"),
            ("break", 79421.1, 79000, "$79,421.10", "> $79,000.00"),
            ("history", 280, 220, "280 bars", "≥ 220 bars"),
            ("cross", 2, 3, "2 bars", "< 3 bars"),
            ("trend", False, True, "No", "Yes"),
        ]
        for key, measured, required, measured_text, required_text in cases:
            with self.subTest(key=key):
                check = d.check(key, "Qualification", False, measured, required)
                self.assert_values(self.rows(check), [measured_text])
                self.assert_values(self.rows(check, required=True), [required_text])

    def test_rsi_range_is_readable_without_percentage_or_json_units(self):
        check = d.check("rsi", "RSI", True, 55.25, [40, 70])
        measured = " ".join(value for _, value in self.rows(check))
        required = " ".join(value for _, value in self.rows(check, required=True))
        self.assertIn("55.25", measured)
        self.assertIn("40", required)
        self.assertIn("70", required)
        for token in ("[", "]", "%", "$"):
            self.assertNotIn(token, measured + required)

    def test_discount_zone_displays_both_price_bounds(self):
        check = d.check("discount", "Discount zone", True, [79000.5, 79421.1], "positive width")
        measured = " ".join(value for _, value in self.rows(check))
        self.assertIn("$79,000.50", measured)
        self.assertIn("$79,421.10", measured)
        self.assertNotIn("[", measured)
        self.assertNotIn("]", measured)

    def test_missing_measurements_have_no_dangling_unit(self):
        for key in ("body", "close", "volume", "break", "cross", "history", "trend", "rsi"):
            with self.subTest(key=key):
                self.assert_values(self.rows(d.check(key, "Unavailable", None)), ["—"])
        check = d.check("block", "Unavailable", None, {key: None for key in EXAMPLE_MEASURED})
        self.assert_values(self.rows(check), ["—"] * len(EXAMPLE_MEASURED))

    def test_general_values_are_human_readable(self):
        self.assertEqual(d.measurement(None), "—")
        self.assertEqual(d.measurement(True), "Yes")
        self.assertEqual(d.measurement(False), "No")
        text = d.measurement({"sample_value": 1.25, "passed": True, "bounds": [10, 20]})
        self.assertNotIn("sample_value", text)
        for token in ("{", "}", "[", "]", '"'):
            self.assertNotIn(token, text)
        self.assertIn("1.25", text)
        self.assertIn("Yes", text)

    def test_currency_and_multiple_helpers_handle_missing_and_negative_values(self):
        self.assertEqual(d.multiple(None), "—")
        self.assertEqual(d.multiple(1.2), "1.20×")
        self.assertEqual(d.money(None), "—")
        self.assertEqual(d.money(-12.5), "-$12.50")
        self.assertEqual(d.money(1234.5), "$1,234.50")
        self.assertEqual(d.number(1234.5678), "1,234.568")
        self.assertEqual(d.number(None), "—")


class DashboardDisplayTests(TemporaryEngine):
    def setUp(self):
        super().setUp()
        network = patch.object(requests.sessions.Session, "request",
                               side_effect=AssertionError("Display tests must remain offline"))
        network.start()
        self.addCleanup(network.stop)
        self.runtime = d.DashboardRuntime(ASSETS, self.rules, self.store, provider=Mock())
        c4, c15, quote, now = reclaim_fixture()
        row = self.runtime.engine.evaluate("BTC", quote, c4, c15, now)
        row["reclaim"]["checks"] = [example_check()]
        row["momentum"] = None
        row["latest_4h_rvol"] = None
        self.runtime.data["BTC"] = row
        self.runtime.updated_ms = now
        self.client = d.create_app(self.runtime).test_client()

    def render(self):
        # Reuse historical numeric evidence to exercise the shared current measurement renderer.
        self.runtime.market["BTC"] = {"smc_long": self.runtime.data["BTC"]["reclaim"]}
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        parsed = DashboardHTML(html)
        self.assertFalse(parsed.errors, parsed.errors)
        return html, parsed

    def test_reported_example_renders_readable_evidence_and_labeled_timestamp(self):
        _, parsed = self.render()
        text = parsed.check_text
        self.assertIn("Measured", text)
        self.assertIn("Required", text)
        self.assertIn("1.78", text)
        self.assertIn("29.03%", text)
        self.assertIn("$79,421.10", text)
        self.assertIn("2.71×", text)
        self.assertIn("Candle opened", text)
        self.assertIn("2026-09-04 12:00:00 UTC", text)
        for key in (*EXAMPLE_MEASURED, *EXAMPLE_REQUIRED):
            if "_" in key:
                self.assertNotIn(key, text)
        self.assertNotIn("{", text)
        self.assertNotIn("}", text)
        self.assertNotIn("—×", parsed.text)
        self.assertNotIn("$—", parsed.text)

    def test_dynamic_labels_and_values_are_escaped(self):
        _, baseline = self.render()
        baseline_script_count = sum(tag == "script" for tag, _ in baseline.tags)
        attack_label = '<img src=x onerror="alert(1)">'
        attack_value = '<script>alert("value")</script>'
        attack_key = '<svg onload="alert(2)">'
        self.runtime.data["BTC"]["reclaim"]["checks"] = [
            d.check("custom", attack_label, None, {attack_key: attack_value}, attack_value, CANDLE_MS)
        ]
        html, parsed = self.render()
        self.assertNotIn(attack_label, html)
        self.assertNotIn(attack_value, html)
        self.assertNotIn(attack_key, html)
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn(attack_label, parsed.text)
        self.assertIn(attack_value, parsed.text)
        self.assertFalse(any(tag == "img" or "onload" in attrs or "onerror" in attrs for tag, attrs in parsed.tags))
        self.assertEqual(sum(tag == "script" for tag, _ in parsed.tags), baseline_script_count)

    def test_waiting_sweep_renders_measurements_explanation_and_timestamp(self):
        c4, _, _, _ = reclaim_fixture()
        c4 = c4[:58]  # Bearish break has closed; no subsequent sweep yet.
        result = d.reclaim_scan(c4, self.rules, c4[-1].end+2)
        self.runtime.data["BTC"]["reclaim"] = result
        _, parsed = self.render()
        text = parsed.check_text
        for label in ("Low price", "Close price", "Sweep depth", "Previous 4H ATR",
                      "Latest completed 4H candle", "Negative depth"):
            self.assertIn(label, text)
        self.assertIn(d.utc(c4[-1].t), text)
        self.assertIn("$97.00", text)
        self.assertIn("< $", text)
        self.assertIn("> $96.00", text)
        sweep = next(c for c in result["checks"] if c["key"] == "sweep")
        sweep["note"] = '<img src=x onerror="alert(1)">'
        html, _ = self.render()
        self.assertNotIn(sweep["note"], html)
        self.assertIn("&lt;img", html)

    def test_failed_15m_candidate_renders_all_price_conditions(self):
        from test_dashboard import setbar
        c4, c15, _, now = reclaim_fixture()
        setup = d.reclaim_scan(c4, self.rules, now)["setup"]
        setbar(c15, 11, 103, 125, 102, 111.5)
        self.runtime.data["BTC"]["reclaim"] = d.ltf_scan(c15, setup, self.rules, now)
        _, parsed = self.render()
        for text in ("Open price", "$103.00", "High price", "$125.00", "Close − open", "> $0.00",
                     "41.30%", "≥ 60.00%", "> $110.00", "Paired retest opened", "Paired retest age"):
            self.assertIn(text, parsed.check_text)
        self.assertNotIn("retest_opened_ms", parsed.check_text)

    def test_new_retest_and_old_break_are_distinguished_in_html(self):
        from test_qualification_consistency import paired_retest_fixture
        c15, setup, rules = paired_retest_fixture()
        self.runtime.data["BTC"]["reclaim"] = d.ltf_scan(c15, setup, rules, c15[-1].end+2)
        _, parsed = self.render()
        for text in ("newer retest needs its own structure break", "Paired retest age", "17 bars", "≤ 16 bars",
                     "Paired retest opened", d.utc(c15[0].t), d.utc(c15[16].t), d.utc(c15[17].t)):
            self.assertIn(text, parsed.check_text)

    def test_rendering_preserves_raw_numeric_evidence_in_json_api(self):
        self.render()
        before = self.client.get("/api/state").get_json()
        self.render()
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        after = response.get_json()
        self.assertEqual(after, before)
        check = after["data"]["BTC"]["reclaim"]["checks"][0]
        self.assertEqual(check["measured"], EXAMPLE_MEASURED)
        self.assertEqual(check["required"], EXAMPLE_REQUIRED)
        self.assertIsInstance(check["measured"]["close"], float)
        self.assertEqual(check["candle_ms"], CANDLE_MS)


if __name__ == "__main__":
    unittest.main()
