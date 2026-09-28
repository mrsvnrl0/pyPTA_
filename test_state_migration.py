"""Disposable migration, provenance, backup and rollback acceptance tests."""
from dataclasses import asdict, replace
import json
from pathlib import Path
from zipfile import ZipFile
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.ledger import StateStore, atomic_json
from adaptive_crypto.smc_ledger import SMCStore
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.persistence import SQLiteDatabase, wal_version_supported
from adaptive_crypto.state_paths import StatePaths, open_stores, state_owner
from adaptive_crypto.state_migration import import_state, verify_state, export_state, backup_state

ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


@unittest.skipUnless(wal_version_supported(sqlite3.sqlite_version_info), "Use a WAL-fixed SQLite runtime")
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root/"study.json"
        self.paths = StatePaths(self.base)
        self.settings = self.root/"settings.json"
        self.rules = Rules(strategy_model="smc_video", market_mode="margin")
        self.write_settings()
        self.paper = SMCStore(self.paths.sources["smc"], ASSETS, self.rules)
        self.positions = PositionStore(self.paths.sources["positions"], market_mode="margin")
        self.positions.open_position(ASSETS, "BTC", "long", 100, 2, "1"*32, 1)
        self.sources = {k: p.read_bytes() for k, p in self.paths.sources.items() if p.exists()}

    def write_settings(self):
        atomic_json(self.settings, {"assets": [{"name": name, **value} for name, value in ASSETS.items()], "strategy": asdict(self.rules)})

    def migrate(self):
        return import_state(self.base, self.settings)

    def test_atomic_import_exact_backups_and_auto_selection(self):
        self.base.write_bytes(b"inactive legacy content, not an active SMC source")
        result = self.migrate()
        self.assertEqual(result["status"], "imported")
        for name, source in self.sources.items():
            self.assertEqual(self.paths.sources[name].read_bytes(), source)
            with ZipFile(result["backup"]) as archive:
                self.assertEqual(archive.read("state/"+self.paths.sources[name].name), source)
        self.assertEqual(self.base.read_bytes(), b"inactive legacy content, not an active SMC source")
        info = verify_state(self.base)
        self.assertEqual(info["integrity"], "ok")
        self.assertEqual(info["counts"]["positions"]["positions"], 1)
        self.assertEqual(self.paths.select("auto"), "sqlite")
        with self.assertRaises(DataError):
            self.paths.select("json")
        with open_stores(self.base, ASSETS, self.rules) as (paper, positions):
            self.assertEqual(paper.database_path, positions.database_path)
            self.assertEqual(positions.snapshot(), self.positions.snapshot())

    def test_dry_run_writes_nothing_even_in_new_parent(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.assertTrue(import_state(self.base, self.settings, dry_run=True)["dry_run"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        missing = self.root/"not-created"/"new.json"
        import_state(missing, self.settings, dry_run=True)
        self.assertFalse(missing.parent.exists())

    def test_corrupt_selected_source_aborts_without_database_or_backup(self):
        self.paths.sources["positions"].write_text('{"version": 1}')
        before = self.paths.sources["positions"].read_bytes()
        with self.assertRaises((DataError, KeyError, ValueError)):
            self.migrate()
        self.assertFalse(self.paths.database.exists())
        self.assertFalse((self.root/"backup").exists())
        self.assertEqual(self.paths.sources["positions"].read_bytes(), before)

    def test_required_backup_failure_does_not_publish(self):
        with patch("adaptive_crypto.state_migration.create_backup", side_effect=OSError("backup full")), self.assertRaises(OSError):
            self.migrate()
        self.assertFalse(self.paths.database.exists())
        self.assertEqual(self.paths.sources["smc"].read_bytes(), self.sources["smc"])

    def test_interrupted_staging_leaves_json_selected_and_rerun_succeeds(self):
        with patch("adaptive_crypto.state_migration._publish_database", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            self.migrate()
        self.assertFalse(self.paths.database.exists())
        self.assertEqual(self.paths.select("auto"), "json")
        self.assertTrue(list(self.root.glob("*.staging-*")))
        self.assertEqual(self.migrate()["status"], "imported")

    def test_failed_multi_namespace_transaction_leaves_no_partial_target(self):
        write = SQLiteDatabase._write_rows
        def fail_second(db, name, *args):
            result = write(db, name, *args)
            if name == "positions":
                raise sqlite3.OperationalError("second namespace failed")
            return result
        with patch.object(SQLiteDatabase, "_write_rows", fail_second), self.assertRaises(sqlite3.OperationalError):
            self.migrate()
        self.assertFalse(self.paths.database.exists())

    def test_import_is_idempotent_after_database_advances_and_sources_conflict(self):
        self.migrate()
        with open_stores(self.base, ASSETS, self.rules) as (paper, positions):
            paper.transaction(lambda d: d["warnings"].append("later state"))
        before = verify_state(self.base)["revisions"]
        self.assertEqual(self.migrate()["status"], "already_imported")
        self.assertEqual(verify_state(self.base)["revisions"], before)
        self.paper.transaction(lambda d: d["warnings"].append("changed stale source"))
        with self.assertRaises(DataError):
            self.migrate()
        with open_stores(self.base, ASSETS, self.rules) as (paper, positions):
            self.assertEqual(paper.snapshot()["warnings"], ["later state"])

    def test_new_strategy_namespace_preserves_existing_database_holdings(self):
        self.migrate()
        with open_stores(self.base, ASSETS, self.rules) as (paper, positions):
            positions.open_position(ASSETS, "BTC", "long", 101, 3, "2"*32, 2)
        self.rules = replace(self.rules, strategy_model="legacy")
        self.write_settings()
        StateStore(self.base, ASSETS, self.rules)
        with self.assertRaises(DataError):
            with open_stores(self.base, ASSETS, self.rules):
                pass
        self.migrate()
        info = verify_state(self.base)
        self.assertEqual(set(info["counts"]), {"legacy", "smc", "positions"})
        self.assertEqual(info["counts"]["positions"]["positions"], 2)

    def test_export_backup_and_reimport_preserve_current_data_and_statuses(self):
        self.migrate()
        with open_stores(self.base, ASSETS, self.rules) as (paper, positions):
            paper.transaction(lambda d: d["outbox"].append({"id": "e", "kind": "telegram", "status": "running", "attempts": 1, "retry_ms": 0}))
            expected_paper, expected_positions = paper.snapshot(), positions.snapshot()
        output = self.root/"export"
        export_state(self.base, output, self.settings)
        self.assertEqual(json.loads((output/self.paths.sources["smc"].name).read_text()), expected_paper)
        self.assertEqual(json.loads((output/self.paths.sources["positions"].name).read_text()), expected_positions)
        with self.assertRaises(DataError):
            export_state(self.base, output)
        backup = Path(backup_state(self.base, settings=self.settings)["backup"])
        with ZipFile(backup) as archive:
            restored = self.root/"restored.sqlite3"
            restored.write_bytes(archive.read("state/"+self.paths.database.name))
            self.assertEqual(archive.read("settings/settings.json"), self.settings.read_bytes())
        with SQLiteDatabase(restored) as db:
            self.assertEqual(db.documents()["smc"], expected_paper)
            self.assertEqual(db.documents()["positions"], expected_positions)
        self.assertEqual(backup_state(self.base, backup, settings=self.settings)["backup"], str(backup))
        self.assertEqual(list(backup.parent.iterdir()), [backup])
        import_state(output/self.base.name, output/"settings.json")
        with open_stores(output/self.base.name, ASSETS, self.rules) as (paper, positions):
            self.assertEqual(paper.snapshot()["outbox"][0]["status"], "uncertain")
            self.assertEqual(positions.snapshot(), expected_positions)

    def test_verify_and_export_do_not_apply_restart_recovery(self):
        self.migrate()
        with open_stores(self.base, ASSETS, self.rules) as (paper, positions):
            paper.transaction(lambda d: d["outbox"].append({"id": "e", "kind": "telegram", "status": "running"}))
        before = verify_state(self.base)["revisions"]
        export_state(self.base, self.root/"export")
        self.assertEqual(verify_state(self.base)["revisions"], before)

    def test_maintenance_respects_ownership_and_bad_database_never_falls_back(self):
        with state_owner(self.paths), self.assertRaises(SystemExit):
            self.migrate()
        self.paths.database.write_bytes(b"not sqlite")
        with self.assertRaises((DataError, sqlite3.DatabaseError)):
            with open_stores(self.base, ASSETS, self.rules):
                pass
        self.assertEqual(self.paths.database.read_bytes(), b"not sqlite")

    def test_legacy_json_archive_policy_preserves_original_bytes(self):
        self.rules = replace(self.rules, strategy_model="legacy")
        self.write_settings()
        source = b'\xef\xbb\xbf{"version":8,"old":"history"}'
        self.base.write_bytes(source)
        result = self.migrate()
        with ZipFile(result["backup"]) as archive:
            self.assertEqual(archive.read("state/"+self.base.name), source)
        self.assertEqual(self.base.read_bytes(), source)
        with open_stores(self.base, ASSETS, self.rules) as (paper, positions):
            self.assertIn("Previous state archived", paper.snapshot()["warnings"][0])

    def test_database_current_context_tracks_settings_changes_for_verification(self):
        self.migrate()
        changed = replace(self.rules, fee_rate=self.rules.fee_rate*2)
        with open_stores(self.base, ASSETS, changed) as (paper, positions):
            self.assertEqual(paper.snapshot()["settings"], asdict(changed))
        self.assertEqual(verify_state(self.base)["contexts"]["smc"]["rules"], asdict(changed))
        with self.assertRaises(DataError):
            export_state(self.base, self.root/"mismatch", self.settings)
