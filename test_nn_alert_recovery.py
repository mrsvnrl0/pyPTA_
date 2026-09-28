"""Offline regressions for the active independent position NN notification path."""
import copy
import json
import unittest

from adaptive_crypto.core import H4, Rules
from adaptive_crypto.position_guidance import monitor_position_guidance
from adaptive_crypto.positions import prepare_positions, validate_positions


NOW = 3*H4 + 1000
RULES = Rules(strategy_model="neural_network")


def reading(now, label):
    return {"signal": {"label": label, "signal_end": now//H4*H4-1,
                       "probabilities": {key: .8 if key == label else .1 for key in ("BUY", "HOLD", "SELL")}},
            "error": None, "expires_ms": now//H4*H4+H4-1}


def document(side="long"):
    return {"version": 1, "watches": {}, "outbox": [], "positions": [{
        "id": "a"*32, "request_id": "b"*32, "asset": "BTC", "symbol": "BTC/USD",
        "status": "open", "side": side, "position_type": "spot" if side == "long" else "margin_short",
        "entry": 100., "quantity": 1., "opened_ms": H4, "close": None, "closed_ms": None,
        "target_mode": "smc", "position_targets": {"smc": None, "gex_smc": None},
        "stop_price": None, "momentum_alerts": True, "tp_alerts": True}]}


def monitor(doc, label="SELL", *, now=NOW, quote=True, price=95., error=False):
    quote = {"bid": price, "ask": price, "asof_ms": now} if quote else None
    signal = {"signal": None, "error": "Temporary candle outage", "expires_ms": now} if error else reading(now, label)
    monitor_position_guidance(doc, "BTC", "BTC/USD", [], [], quote, signal, RULES, None, now)
    validate_positions(doc)
    return doc["positions"][0]["nn_guidance"]


def neural_events(doc):
    return [e for e in doc["outbox"] if e.get("alert_type") == "position_neural"]


class NeuralAlertRecoveryTests(unittest.TestCase):
    def baseline(self, side="long"):
        doc = document(side)
        monitor(doc, "HOLD", now=NOW-H4)
        return doc

    def test_quote_outage_does_not_consume_new_exit_for_either_side(self):
        for side, label, price, light in (("long", "SELL", 95, "STOP LOSS"),
                                          ("long", "SELL", 105, "TAKE PROFIT"),
                                          ("short", "BUY", 105, "STOP LOSS"),
                                          ("short", "BUY", 95, "TAKE PROFIT")):
            with self.subTest(side=side, light=light):
                doc = self.baseline(side)
                self.assertEqual(monitor(doc, label, quote=False)["light"], "WAIT")
                self.assertEqual(neural_events(doc), [])
                self.assertEqual(monitor(doc, label, now=NOW+15000, price=price)["light"], light)
                events = neural_events(doc)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["status"], "queued")
                self.assertEqual(doc["positions"][0]["status"], "open")

    def test_first_valid_classification_is_baseline_even_without_quote(self):
        doc = document()
        monitor(doc, error=True, quote=False)
        monitor(doc, "SELL", quote=False)
        monitor(doc, "SELL", now=NOW+15000)
        self.assertEqual(neural_events(doc), [])

    def test_quote_recovery_restores_undelivered_event_without_resetting_retry(self):
        doc = self.baseline()
        monitor(doc)
        event = neural_events(doc)[0]
        event.update(attempts=2, retry_ms=NOW+60000)
        identity = event["id"]
        monitor(doc, quote=False, now=NOW+15000)
        self.assertEqual(event["status"], "cancelled")
        self.assertFalse(event.get("retired"))
        monitor(doc, now=NOW+30000, price=105)
        self.assertEqual((event["id"], event["status"], event["attempts"], event["retry_ms"]),
                         (identity, "queued", 2, NOW+60000))
        self.assertIn("TAKE PROFIT", event["action_label"])
        self.assertEqual(len(neural_events(doc)), 1)

    def test_model_outage_preserves_pending_transition_and_queued_event(self):
        doc = self.baseline()
        monitor(doc, quote=False)
        monitor(doc, error=True, now=NOW+10000)
        monitor(doc, now=NOW+20000)
        event = neural_events(doc)[0]
        monitor(doc, error=True, now=NOW+30000)
        self.assertEqual(event["status"], "cancelled")
        self.assertFalse(event.get("retired"))
        monitor(doc, now=NOW+40000)
        self.assertEqual(event["status"], "queued")
        self.assertEqual(len(neural_events(doc)), 1)

    def test_sent_running_failed_or_uncertain_event_is_never_resent_after_outage(self):
        for status in ("sent", "running", "failed", "uncertain"):
            with self.subTest(status=status):
                doc = self.baseline()
                monitor(doc)
                event = neural_events(doc)[0]
                event["status"] = status
                monitor(doc, error=True, quote=False, now=NOW+10000)
                monitor(doc, now=NOW+20000)
                self.assertEqual(event["status"], status)
                self.assertEqual(len(neural_events(doc)), 1)
                monitor(doc, now=NOW+H4)
                self.assertEqual(len(neural_events(doc)), 1)

    def test_restart_can_revalidate_current_unsent_nn_alert(self):
        doc = self.baseline()
        monitor(doc)
        doc, _ = prepare_positions(json.dumps(doc).encode())
        self.assertEqual(neural_events(doc)[0]["status"], "cancelled")
        monitor(doc, now=NOW+15000)
        self.assertEqual(neural_events(doc)[0]["status"], "queued")
        self.assertEqual(len(neural_events(doc)), 1)

    def test_newly_opened_or_reenabled_position_does_not_backfill(self):
        for field in ("opened_ms", "momentum_enabled_ms"):
            with self.subTest(field=field):
                doc = self.baseline()
                doc["positions"][0][field] = NOW
                monitor(doc, quote=False)
                monitor(doc, now=NOW+15000)
                self.assertEqual(neural_events(doc), [])

    def test_disabled_closed_and_superseded_alerts_cannot_recover(self):
        for change in ("disabled", "closed", "hold", "new_candle"):
            with self.subTest(change=change):
                doc = self.baseline()
                monitor(doc)
                event = neural_events(doc)[0]
                monitor(doc, quote=False, now=NOW+5000)
                if change == "disabled":
                    doc["positions"][0]["momentum_alerts"] = False
                elif change == "closed":
                    doc["positions"][0].update(status="closed", close=95., closed_ms=NOW+6000)
                monitor(doc, "HOLD" if change == "hold" else "SELL",
                        now=NOW+H4 if change == "new_candle" else NOW+15000)
                self.assertEqual(event["status"], "cancelled")
                self.assertEqual(len(neural_events(doc)), 1)
                if change != "closed":
                    self.assertTrue(event.get("retired"))

    def test_stale_classification_never_reactivates_pending_alert(self):
        doc = self.baseline()
        monitor(doc)
        monitor(doc, quote=False)
        monitor_position_guidance(doc, "BTC", "BTC/USD", [], [],
                                  {"bid": 95., "ask": 95., "asof_ms": NOW+H4},
                                  reading(NOW, "SELL"), RULES, None, NOW+H4)
        self.assertEqual(doc["positions"][0]["nn_guidance"]["light"], "WAIT")
        self.assertEqual(neural_events(doc)[0]["status"], "cancelled")

    def test_supporting_signal_without_quote_still_resets_transition_baseline(self):
        doc = self.baseline()
        monitor(doc)
        neural_events(doc)[0]["status"] = "sent"
        monitor(doc, "HOLD", quote=False, now=NOW+H4)
        monitor(doc, "SELL", now=NOW+2*H4)
        self.assertEqual(len(neural_events(doc)), 2)
        self.assertEqual(neural_events(doc)[1]["status"], "queued")

    def test_existing_visible_guidance_seeds_notification_baseline(self):
        doc = document()
        doc["positions"][0]["nn_guidance"] = {"signal": "HOLD", "signal_end": H4-1}
        monitor(doc, quote=False)
        monitor(doc, now=NOW+15000)
        self.assertEqual(neural_events(doc)[0]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
