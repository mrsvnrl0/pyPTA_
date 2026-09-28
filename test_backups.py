"""Complete backup contents, single-copy retention, and failed-write recovery."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from adaptive_crypto.administration import purge_database, save_model_selection
from adaptive_crypto.backups import create_backup, backup_runtime
from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.ledger import StateStore, atomic_json
from adaptive_crypto.neural_ledger import NeuralStore
from adaptive_crypto.persistence import SQLiteDatabase, wal_version_supported
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.settings_editor import read_editor, save_editor
from adaptive_crypto.state_migration import backup_state
from adaptive_crypto.state_paths import StatePaths, open_stores

ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root/"study.json"
        self.paths = StatePaths(self.base)
        self.settings = self.root/"custom-settings.json"
        self.rules = Rules(strategy_model="smc_video")
        atomic_json(self.settings, {"assets": [{"name": name, **cfg} for name, cfg in ASSETS.items()],
                                   "strategy": asdict(self.rules), "refresh_seconds": 15})
        self.target = self.root/"backup"/"latest.zip"

    def runtime(self, backend):
        owner = open_stores(self.base, ASSETS, self.rules, backend, settings_path=self.settings)
        paper, holdings = owner.__enter__()
        self.addCleanup(owner.__exit__, None, None, None)
        runtime = DashboardRuntime(ASSETS, self.rules, paper, 15, position_store=holdings,
                                   settings_path=self.settings)
        runtime.state_base = self.base
        return runtime

    def test_manual_json_backup_contains_all_sources_and_replaces_one_file(self):
        for name, path in self.paths.sources.items():
            path.write_bytes(json.dumps({"namespace": name, "history": [1, 2]}).encode())
        historical = self.root/"study.json.pre-edit-old.json"
        historical.write_bytes(b"older evidence")
        first = backup_state(self.base, settings=self.settings)
        self.assertEqual(first["backup"], str(self.target))
        before = self.target.read_bytes()
        self.settings.write_bytes(self.settings.read_bytes()+b"\n")
        self.paths.sources["positions"].write_bytes(b'{"latest": true}')
        self.assertEqual(backup_state(self.base, settings=self.settings), first)
        self.assertNotEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])
        self.assertEqual(historical.read_bytes(), b"older evidence")
        with ZipFile(self.target) as archive:
            self.assertEqual(archive.read("settings/settings.json"), self.settings.read_bytes())
            for path in self.paths.sources.values():
                self.assertEqual(archive.read("state/"+path.name), path.read_bytes())
            manifest = json.loads(archive.read("manifest.json"))
            for name, record in manifest["files"].items():
                self.assertEqual(record["sha256"], hashlib.sha256(archive.read(name)).hexdigest())
        with self.assertRaisesRegex(DataError, "single archive"):
            backup_state(self.base, self.root/"second.zip", settings=self.settings)
        self.assertFalse((self.root/"second.zip").exists())

    def test_failed_backup_leaves_previous_archive_and_settings_unchanged(self):
        runtime = self.runtime("json")
        document, revision = read_editor(runtime)
        backup_runtime(runtime)
        previous = self.target.read_bytes()
        saved = self.settings.read_bytes()
        document["refresh_seconds"] = 25
        for target, kwargs in (
                ("adaptive_crypto.backups.os.fsync", {"side_effect": OSError("disk full")}),
                ("adaptive_crypto.backups.ZipFile.testzip", {"return_value": "broken entry"}),
                ("adaptive_crypto.backups.os.replace", {"side_effect": OSError("replace failed")})):
            with self.subTest(failure=target):
                with patch(target, **kwargs), self.assertRaises((DataError, OSError)):
                    save_editor(runtime, document, revision)
                self.assertEqual(self.target.read_bytes(), previous)
                self.assertEqual(self.settings.read_bytes(), saved)
                self.assertEqual(list(self.target.parent.iterdir()), [self.target])
                self.assertEqual(runtime.refresh, 15)

    def test_save_selection_and_purge_share_one_complete_archive(self):
        runtime = self.runtime("json")
        runtime.positions.open_position(ASSETS, "BTC", "long", 100, 2, "a"*32, 1)
        before = runtime.positions.snapshot()
        self.paths.sources["neural"].write_bytes(b"inactive neural history")
        document, revision = read_editor(runtime)
        document["refresh_seconds"] = 25
        self.assertEqual(save_editor(runtime, document, revision)["backup"], str(self.target))
        self.assertEqual(save_model_selection(runtime, "neural_network")["backup"], str(self.target))
        self.assertEqual(purge_database(runtime, runtime.generation)["backup"], str(self.target))
        self.assertEqual(runtime.positions.snapshot()["positions"], [])
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])
        with ZipFile(self.target) as archive:
            self.assertEqual(json.loads(archive.read("metadata/documents.json"))["documents"]["positions"], before)
            self.assertEqual(archive.read("state/study.neural.json"), b"inactive neural history")
            self.assertEqual(json.loads(archive.read("settings/settings.json"))["strategy"]["strategy_model"], "neural_network")
            self.assertEqual(json.loads(archive.read("settings/smc.settings.json"))["strategy"]["strategy_model"], "smc_video")
            self.assertEqual(json.loads(archive.read("manifest.json"))["action"], "purge")

    def test_startup_archive_preserves_exact_damaged_source_and_custom_settings(self):
        original = b'\xef\xbb\xbf{"version":8,"old":"history"}'
        self.base.write_bytes(original)
        self.paths.sources["neural"].write_bytes(b"inactive history")
        rules = replace(self.rules, strategy_model="legacy")
        with open_stores(self.base, ASSETS, rules, "json", settings_path=self.settings):
            pass
        with ZipFile(self.target) as archive:
            self.assertEqual(archive.read("state/"+self.base.name), original)
            self.assertEqual(archive.read("state/study.neural.json"), b"inactive history")
            self.assertEqual(archive.read("settings/settings.json"), self.settings.read_bytes())
        self.assertNotEqual(self.base.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.archived-*.json")))

    @unittest.skipUnless(wal_version_supported(sqlite3.sqlite_version_info), "Use a WAL-fixed SQLite runtime")
    def test_sqlite_archive_restores_committed_wal_all_namespaces_and_contexts(self):
        runtime = self.runtime("sqlite")
        database = runtime.store._backend.database
        StateStore(self.base, ASSETS, replace(self.rules, strategy_model="legacy"),
                   backend=database.namespace("legacy"))
        NeuralStore(self.paths.sources["neural"], ASSETS, replace(self.rules, strategy_model="neural_network"),
                    backend=database.namespace("neural"))
        runtime.positions.open_position(ASSETS, "BTC", "long", 100, 2, "a"*32, 1)
        runtime.store.transaction(lambda doc: doc["warnings"].append("committed in WAL"))
        self.paths.sources["positions"].write_bytes(b"stale JSON")
        expected, info = database.documents(), database.information()
        self.assertTrue(Path(str(database.path)+"-wal").stat().st_size)
        backup_runtime(runtime)
        with ZipFile(self.target) as archive:
            copied = self.root/"restored.sqlite3"
            copied.write_bytes(archive.read("state/"+self.paths.database.name))
            self.assertNotIn("state/"+self.paths.sources["positions"].name, archive.namelist())
            self.assertEqual(json.loads(archive.read("metadata/documents.json"))["documents"], expected)
            self.assertEqual(archive.read("settings/settings.json"), self.settings.read_bytes())
        with SQLiteDatabase(copied) as restored:
            self.assertEqual(restored.documents(), expected)
            self.assertEqual(restored.information()["contexts"], info["contexts"])
            self.assertEqual(restored.information()["imports"], info["imports"])
            self.assertEqual(restored.information()["integrity"], "ok")
        previous = self.target.read_bytes()
        with patch.object(database, "backup", side_effect=OSError("database copy failed")), self.assertRaises(OSError):
            purge_database(runtime, runtime.generation)
        self.assertEqual(self.target.read_bytes(), previous)
        self.assertEqual(database.documents(), expected)
        self.assertFalse(runtime.paused)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_custom_state_filename_cannot_collide_with_archive_metadata(self):
        base = self.root/"documents.json"
        original = b'{"original": "legacy data"}'
        base.write_bytes(original)
        create_backup(base, settings=self.settings, documents={"positions": {}})
        with ZipFile(self.target) as archive:
            self.assertEqual(archive.read("state/documents.json"), original)
            self.assertEqual(json.loads(archive.read("metadata/documents.json"))["documents"], {"positions": {}})
