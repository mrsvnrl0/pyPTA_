"""Disposable study coverage for full restore, replay protection and crash recovery."""
from contextlib import ExitStack, closing
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from adaptive_crypto.backups import backup_runtime
from adaptive_crypto.core import DataError, H4, Rules
from adaptive_crypto.ledger import StateStore, atomic_json, queue_event
from adaptive_crypto.persistence import wal_version_supported
from adaptive_crypto.restore import confirm_restore, preview_restore, recover_pending_restore
from adaptive_crypto import restore
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.web import create_app

ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


@unittest.skipUnless(wal_version_supported(sqlite3.sqlite_version_info), "Use a WAL-fixed SQLite runtime")
class RestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root/"study.json"
        self.settings = self.root/"settings.json"
        self.rules = Rules(strategy_model="neural_network")
        self.saved = {"assets": [{"name": key, **value} for key, value in ASSETS.items()],
                      "strategy": asdict(self.rules), "refresh_seconds": 15}
        atomic_json(self.settings, self.saved)
        self.owners = ExitStack()
        self.addCleanup(self.owners.close)
        paper, positions = self.owners.enter_context(open_stores(self.base, ASSETS, self.rules, "sqlite", settings_path=self.settings))
        self.runtime = DashboardRuntime(ASSETS, self.rules, paper, position_store=positions, settings_path=self.settings)
        self.runtime.state_base = self.base
        self.database = paper._backend.database
        StateStore(self.base, ASSETS, replace(self.rules, strategy_model="legacy"), backend=self.database.namespace("legacy"))
        positions.open_position(ASSETS, "BTC", "long", 100, 1, "a"*32, 100)
        paper.transaction(lambda doc: queue_event(doc, "historical", "telegram", "Old event", 1))
        self.archived = self.database.documents()
        self.archive = backup_runtime(self.runtime)
        self.client = create_app(self.runtime).test_client()
        self.client.get("/settings")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]

    def preview(self):
        return preview_restore(self.runtime, self.runtime.generation)

    def confirm(self, preview):
        return confirm_restore(self.runtime, self.runtime.generation, preview["token"], "restore")

    def post(self, route, body=None):
        return self.client.post("/api/database/restore"+route, json=body or {}, headers={"X-CSRF-Token": self.csrf})

    def rewrite(self, transform, rehash=True):
        with ZipFile(self.archive) as zipped:
            entries = {name: zipped.read(name) for name in zipped.namelist()}
        transform(entries)
        if rehash:
            manifest = json.loads(entries["manifest.json"])
            manifest["files"] = {name: {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                                 for name, raw in entries.items() if name != "manifest.json"}
            entries["manifest.json"] = json.dumps(manifest).encode()
        with ZipFile(self.archive, "w") as zipped:
            for name, raw in entries.items():
                zipped.writestr(name, raw)

    def test_preview_is_read_only_and_lists_all_history_and_settings(self):
        before = self.database.documents(), self.settings.read_bytes(), self.archive.read_bytes()
        data = self.preview()
        self.assertEqual(set(data["records"]), {"legacy", "neural", "positions"})
        self.assertEqual(data["records"]["positions"]["positions"], 1)
        self.assertEqual(data["records"]["neural"]["queued_alerts"], 1)
        self.assertEqual(data["settings"], self.saved)
        self.assertEqual(before, (self.database.documents(), self.settings.read_bytes(), self.archive.read_bytes()))

    def test_restore_all_namespaces_settings_provenance_and_cancel_old_alerts(self):
        self.runtime.positions.open_position(ASSETS, "BTC", "long", 120, 2, "b"*32, 200)
        saved = {**self.saved, "refresh_seconds": 30}
        atomic_json(self.settings, saved)
        previous = self.database.documents()
        imports = self.database.information()["imports"]
        preview = self.preview()
        generation = self.runtime.generation
        result = self.confirm(preview)
        expected = self.archived
        expected["neural"]["outbox"][0].update(status="cancelled", retired=True, restore_suppressed=True,
                                               error="Historical undelivered alert retired during backup restore.")
        self.assertEqual(self.database.documents(), expected)
        self.assertEqual(json.loads(self.settings.read_bytes()), self.saved)
        self.assertEqual(self.database.information()["imports"], imports)
        self.assertNotEqual(self.runtime.generation, generation)
        self.assertEqual(self.runtime.refresh, 15)
        self.assertFalse(restore._journal_path(self.base).exists())
        with ZipFile(result["backup"]) as archive:
            self.assertEqual(json.loads(archive.read("metadata/documents.json"))["documents"], previous)
            self.assertEqual(json.loads(archive.read("settings/settings.json")), saved)

    def test_route_requires_csrf_explicit_confirmation_and_current_generation(self):
        self.assertEqual(self.client.post("/api/database/restore/preview", json={}).status_code, 400)
        preview = self.post("/preview").get_json()
        self.assertEqual(self.post("", {"token": preview["token"], "confirm": "wrong"}).status_code, 400)
        response = self.post("", {"token": preview["token"], "confirm": "restore"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.post("", {"token": preview["token"], "confirm": "restore"}).status_code, 400)

    def test_archive_and_settings_changes_invalidate_preview(self):
        preview = self.preview()
        self.settings.write_bytes(self.settings.read_bytes()+b"\n")
        with self.assertRaisesRegex(DataError, "changed after preview"):
            self.confirm(preview)
        preview = self.preview()
        backup_runtime(self.runtime, action="another-backup")
        with self.assertRaisesRegex(DataError, "changed after preview"):
            self.confirm(preview)

    def test_expired_preview_cannot_restore(self):
        preview = self.preview()
        self.runtime._restore_preview["expires"] = 0
        with self.assertRaisesRegex(DataError, "expired"):
            self.confirm(preview)

    def test_bad_hash_and_missing_settings_fail_before_any_changes(self):
        before = self.database.documents(), self.settings.read_bytes()
        original = self.archive.read_bytes()
        self.rewrite(lambda entries: entries.update({"settings/settings.json": b"{}"}), rehash=False)
        with self.assertRaisesRegex(DataError, "verification failed"):
            self.preview()
        self.archive.write_bytes(original)
        self.rewrite(lambda entries: entries.pop("settings/settings.json"))
        with self.assertRaisesRegex(DataError, "no saved settings"):
            self.preview()
        self.assertEqual(before, (self.database.documents(), self.settings.read_bytes()))

    def test_metadata_database_disagreement_is_rejected(self):
        self.rewrite(lambda entries: entries.update({"metadata/documents.json": b"{}"}))
        with self.assertRaisesRegex(DataError, "differs"):
            self.preview()

    def test_backup_failure_never_changes_settings_or_database(self):
        preview = self.preview()
        before = self.database.documents(), self.settings.read_bytes()
        with patch.object(restore, "backup_runtime", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.confirm(preview)
        self.assertEqual(before, (self.database.documents(), self.settings.read_bytes()))
        self.assertFalse(restore._journal_path(self.base).exists())

    def test_database_publish_failure_rolls_back_settings_and_fresh_records(self):
        preview = self.preview()
        self.runtime.positions.open_position(ASSETS, "BTC", "long", 125, 1, "c"*32, 300)
        before = self.database.documents(), self.settings.read_bytes()
        actual = restore._copy_database
        calls = 0
        def fail_once(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("failed database publication")
            return actual(*args)
        with patch.object(restore, "_copy_database", side_effect=fail_once), self.assertRaisesRegex(DataError, "were recovered"):
            self.confirm(preview)
        self.assertEqual(before, (self.database.documents(), self.settings.read_bytes()))
        self.assertFalse(self.runtime.stop.is_set())
        self.assertFalse(restore._journal_path(self.base).exists())

    def test_failed_rollback_stops_runtime_and_next_start_recovers(self):
        preview = self.preview()
        before = self.database.documents(), self.settings.read_bytes()
        with patch.object(restore, "_copy_database", side_effect=OSError("disk unavailable")), self.assertRaisesRegex(DataError, "Monitoring stopped"):
            self.confirm(preview)
        self.assertTrue(self.runtime.stop.is_set())
        self.assertTrue(restore._journal_path(self.base).exists())
        self.owners.close()
        self.assertTrue(recover_pending_restore(self.base, self.settings))
        self.assertFalse(recover_pending_restore(self.base, self.settings))
        with open_stores(self.base, ASSETS, self.rules, "sqlite", settings_path=self.settings) as (paper, positions):
            self.assertEqual(paper._backend.database.documents(), before[0])
            self.assertEqual(self.settings.read_bytes(), before[1])

    def test_restore_waits_for_existing_activity(self):
        preview = self.preview()
        started, finished = threading.Event(), threading.Event()
        errors = []
        def apply():
            started.set()
            try:
                self.confirm(preview)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with self.runtime.activity_gate.activity():
            thread = threading.Thread(target=apply)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(.05))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_settings_publication_failure_rolls_back_without_losing_history(self):
        preview = self.preview()
        before = self.database.documents(), self.settings.read_bytes()
        actual = restore._write_bytes
        calls = 0
        def fail_once(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("settings disk unavailable")
            return actual(*args)
        with patch.object(restore, "_write_bytes", side_effect=fail_once), self.assertRaisesRegex(DataError, "were recovered"):
            self.confirm(preview)
        self.assertEqual(before, (self.database.documents(), self.settings.read_bytes()))
        self.assertFalse(restore._journal_path(self.base).exists())

    def test_complete_restore_removes_namespaces_not_in_archive_and_clears_cache(self):
        from adaptive_crypto.smc_ledger import SMCStore
        SMCStore(self.base.with_suffix(".smc.json"), ASSETS, replace(self.rules, strategy_model="smc_video"),
                 backend=self.database.namespace("smc"))
        self.assertIn("smc", self.database.documents())
        self.confirm(self.preview())
        self.assertNotIn("smc", self.database.documents())
        self.assertNotIn("smc", self.database._cache)

    def test_restore_preserves_saved_edits_separately_from_archived_applied_rules(self):
        saved = {**self.saved, "strategy": asdict(replace(self.rules, nn_stop_loss=.06))}
        atomic_json(self.settings, saved)
        backup_runtime(self.runtime)
        preview = self.preview()
        self.assertTrue(preview["saved_settings_differ"])
        result = self.confirm(preview)
        self.assertEqual(self.runtime.rules, self.rules)
        self.assertEqual(json.loads(self.settings.read_bytes()), saved)
        self.assertIn("Apply saved settings", result["message"])

    def test_running_alert_is_marked_uncertain_without_replay(self):
        self.runtime.store.transaction(lambda doc: doc["outbox"][0].update(status="running"))
        backup_runtime(self.runtime)
        self.confirm(self.preview())
        self.assertEqual(self.runtime.store.snapshot()["outbox"][0]["status"], "uncertain")

    def test_unknown_schema_is_rejected_without_touching_current_database(self):
        def alter(entries):
            copied = self.root/"malformed.sqlite3"
            copied.write_bytes(entries["state/study.sqlite3"])
            with closing(sqlite3.connect(copied)) as connection:
                connection.execute("CREATE TABLE unexpected (value TEXT)")
                connection.commit()
            entries["state/study.sqlite3"] = copied.read_bytes()
        self.rewrite(alter)
        before = self.database.documents()
        with self.assertRaisesRegex(DataError, "unsupported schema"):
            self.preview()
        self.assertEqual(self.database.documents(), before)

    def test_document_and_context_settings_mismatch_is_rejected(self):
        documents, contexts = self.database.documents(), self.database.information()["contexts"]
        documents["neural"]["settings"]["nn_stop_loss"] = .07
        self.database.replace_documents(documents, contexts)
        backup_runtime(self.runtime)
        with self.assertRaisesRegex(DataError, "paper settings disagree"):
            self.preview()

    def test_recovery_rejects_wrong_study_identity_before_mutating(self):
        marker = restore._journal_path(self.base)
        atomic_json(marker, {"version": 1, "state_base": str(self.base), "settings_path": str(self.root/"other.json")})
        original = self.settings.read_bytes()
        self.owners.close()
        with self.assertRaisesRegex(DataError, "different study or settings"):
            recover_pending_restore(self.base, self.settings)
        self.assertEqual(self.settings.read_bytes(), original)

    def guidance(self, label, now, price):
        from adaptive_crypto.position_guidance import monitor_position_guidance
        from test_nn_alert_recovery import reading
        self.runtime.positions.transaction(lambda doc: monitor_position_guidance(
            doc, "BTC", "BTC/USD", [], [], {"bid": price, "ask": price, "asof_ms": now},
            reading(now, label), self.rules, None, now))

    def assert_no_delivery(self, now):
        from adaptive_crypto.notifications import dispatch_once
        sender = Mock(return_value={"status": "sent"})
        self.assertFalse(dispatch_once(self.runtime.positions, "telegram", sender, now))
        sender.assert_not_called()

    def test_restore_then_real_nn_stop_monitor_never_replays_but_new_nn_signal_delivers(self):
        from adaptive_crypto.notifications import dispatch_once
        now = 3*H4+1000
        self.runtime.positions.transaction(lambda doc: doc["positions"][0].update(
            stop_price=98., stop_revision=1, nn_alert_signal="HOLD",
            position_targets={"smc": None, "gex_smc": None}))
        self.guidance("SELL", now, 95.)
        events = self.runtime.positions.snapshot()["outbox"]
        self.assertEqual({e["alert_type"] for e in events}, {"position_neural", "position_stop"})
        self.runtime.positions.transaction(lambda doc: doc["outbox"][0].update(status="cancelled"))
        backup_runtime(self.runtime)
        self.confirm(self.preview())
        self.guidance("SELL", now+15000, 95.)
        restored = self.runtime.positions.snapshot()["outbox"]
        self.assertEqual({e["id"] for e in restored}, {e["id"] for e in events})
        self.assertTrue(all(e["status"] == "cancelled" and e["retired"] for e in restored))
        self.assert_no_delivery(now+15000)
        # A later, distinct completed-candle transition still sends normally.
        self.guidance("HOLD", now+H4, 105.)
        self.guidance("SELL", now+2*H4, 105.)
        sender = Mock(return_value={"status": "sent"})
        self.assertTrue(dispatch_once(self.runtime.positions, "telegram", sender, now+2*H4))
        self.assertNotIn(sender.call_args.args[0]["id"], {e["id"] for e in events})
        self.assertFalse(dispatch_once(self.runtime.positions, "telegram", sender, now+2*H4))
        self.assertEqual(sender.call_count, 1)

    def seed_target(self):
        level = 110.
        target = {"liquidity_price": level, "pivot_ms": 100, "price": level*(1+self.rules.smc_tp_sweep_buffer_bps/10000),
                  "sweep_buffer_bps": self.rules.smc_tp_sweep_buffer_bps, "selected_ms": 200,
                  "setup_minutes": self.rules.smc_setup_minutes, "state": "watching", "reached_ms": None}
        self.runtime.positions.transaction(lambda doc: doc["positions"][0].update(
            position_targets={"smc": target, "gex_smc": None}, nn_alert_signal="HOLD"))
        return target["price"]

    def test_restored_cancelled_near_tp_cannot_requeue_on_fresh_quotes(self):
        now, target = 3*H4+1000, self.seed_target()
        self.guidance("HOLD", now, target*.9995)
        event, = self.runtime.positions.snapshot()["outbox"]
        self.assertEqual((event["alert_type"], event["status"]), ("near_take_profit", "queued"))
        self.runtime.positions.transaction(lambda doc: doc["outbox"][0].update(status="cancelled"))
        backup_runtime(self.runtime)
        self.confirm(self.preview())
        self.guidance("HOLD", now+15000, target*.9995)
        restored, = self.runtime.positions.snapshot()["outbox"]
        self.assertEqual((restored["id"], restored["status"], restored["retired"]), (event["id"], "cancelled", True))
        self.assert_no_delivery(now+15000)

    def test_restored_target_hit_cannot_requeue_on_fresh_quotes(self):
        now, target = 3*H4+1000, self.seed_target()
        self.guidance("HOLD", now, target+.01)
        event, = self.runtime.positions.snapshot()["outbox"]
        self.assertEqual((event["alert_type"], event["status"]), ("target_reached", "queued"))
        backup_runtime(self.runtime)
        self.confirm(self.preview())
        self.guidance("HOLD", now+15000, target+.02)
        restored, = self.runtime.positions.snapshot()["outbox"]
        self.assertEqual((restored["id"], restored["status"], restored["retired"]), (event["id"], "cancelled", True))
        self.assert_no_delivery(now+15000)

    def test_dispatcher_refuses_retired_identity_even_if_producer_queues_it(self):
        from adaptive_crypto.notifications import dispatch_once
        self.confirm(self.preview())
        self.runtime.store.transaction(lambda doc: doc["outbox"][0].update(status="queued"))
        sender = Mock(return_value={"status": "sent"})
        self.assertFalse(dispatch_once(self.runtime.store, "telegram", sender, 1000))
        sender.assert_not_called()
        self.assertEqual(self.runtime.store.snapshot()["outbox"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
