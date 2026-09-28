"""Real SQLite durability, record layout, ownership and threading regressions."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.ledger import StateStore, queue_event
from adaptive_crypto.notifications import dispatch_once
from adaptive_crypto.persistence import SQLiteDatabase, check_sqlite_runtime, wal_version_supported
from adaptive_crypto.state_codec import encode, decode

ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}
FIXED = wal_version_supported(sqlite3.sqlite_version_info)


class CodecTests(unittest.TestCase):
    def test_roundtrip_preserves_absent_fields_unknown_metadata_and_duplicate_values(self):
        doc = {"version": 1, "opaque": {"tiny": 1e-20, "null": None}, "assets": {
            "B": {"symbol": "BTC/USD", "trades": [{"id": "same"}, {"id": "same"}], "consumed": ["dup", "dup"]},
            "A": {"symbol": "ETH/USD", "trades": [], "consumed": []}}, "outbox": list(range(12)), "watches": {}}
        before = copy.deepcopy(doc)
        root, rows = encode(doc)
        self.assertEqual(doc, before)
        restored = decode(root, rows)
        self.assertEqual(restored, doc)
        self.assertEqual(list(restored["assets"]), ["B", "A"])
        self.assertNotIn("buy_watches", restored)

    def test_corrupt_layout_and_nonfinite_payloads_fail(self):
        root, rows = encode({"outbox": [1, 2]})
        for bad in ({("outbox", "", "1"): rows[("outbox", "", "1")]},
                    {**rows, ("alien", "", "0"): (0, "{}")},
                    {("outbox", "", "0"): (0, "NaN")},
                    {("trade", "absent", "0"): (0, "{}")},
                    {("outbox", "", "0"): (0, "1"), ("outbox", "", "1"): (0, "2")}):
            with self.subTest(bad=bad), self.assertRaises((DataError, ValueError)):
                decode(root, bad)
        with self.assertRaises(ValueError):
            encode({"value": float("inf")})

    def test_version_gate_accepts_only_fixed_branches(self):
        for version in ((3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 1)):
            self.assertTrue(wal_version_supported(version))
        for version in ((3, 44, 5), (3, 50, 4), (3, 51, 2), (3, 49, 99)):
            self.assertFalse(wal_version_supported(version))
        with patch("adaptive_crypto.persistence.sqlite3.sqlite_version_info", (3, 50, 4)):
            with self.assertRaises(DataError):
                check_sqlite_runtime()

    def test_database_filename_is_not_a_logical_base_regardless_of_case(self):
        from adaptive_crypto.state_paths import StatePaths
        for name in ("study.sqlite3", "study.SQLITE3"):
            with self.assertRaises(DataError):
                StatePaths(Path(name))


@unittest.skipUnless(FIXED, "SQLite integration requires a WAL-fixed runtime; use .venv/Scripts/python.exe")
class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)/"study.json"
        self.path = self.base.with_suffix(".sqlite3")
        self.rules = Rules()
        self.db = SQLiteDatabase(self.path, create=True)
        self.addCleanup(self.db.close)
        self.store = StateStore(self.base, ASSETS, self.rules, backend=self.db.namespace("legacy"))

    def reopen(self):
        self.db.close()
        self.db = SQLiteDatabase(self.path)
        self.addCleanup(self.db.close)
        self.store = StateStore(self.base, ASSETS, self.rules, backend=self.db.namespace("legacy"))

    def event(self):
        self.store.transaction(lambda d: queue_event(d, "event", "telegram", "stub", 1))

    def test_pragmas_noop_snapshot_and_bounded_event_writes(self):
        self.assertEqual(self.db._connection.execute("PRAGMA journal_mode").fetchone(), ("wal",))
        self.assertEqual(self.db._connection.execute("PRAGMA synchronous").fetchone(), (2,))
        self.store.transaction(lambda d: [queue_event(d, str(i), "telegram", "stub", 1) for i in range(1000)])
        statements = []
        self.db._connection.set_trace_callback(statements.append)
        self.store.snapshot()
        self.store.transaction(lambda d: None)
        self.assertEqual(statements, [])
        self.store.transaction(lambda d: d["outbox"][500].update(status="sent"))
        self.assertEqual(self.db.last_changed_rows, 2)
        writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(len(writes), 2)
        self.assertFalse(self.base.exists())
        self.db._connection.set_trace_callback(None)
        self.reopen()
        self.assertEqual(self.store.snapshot()["outbox"][500]["status"], "sent")

    def test_detached_aliases_and_recursive_transactions(self):
        retained = []
        def mutate(doc):
            retained.append(doc)
            doc["warnings"].append("saved")
            return doc["warnings"]
        result = self.store.transaction(mutate)
        result.append("returned")
        retained[0]["warnings"].append("retained")
        self.store.snapshot()["warnings"].append("snapshot")
        self.assertEqual(self.store.snapshot()["warnings"], ["saved"])
        with self.assertRaises(DataError):
            self.store.transaction(lambda doc: self.store.transaction(lambda nested: nested["warnings"].append("nested")))
        self.assertEqual(self.store.snapshot()["warnings"], ["saved"])

    def test_callback_validation_encoding_write_and_commit_failures_roll_back(self):
        before = self.store.snapshot()
        with self.assertRaises(RuntimeError):
            self.store.transaction(lambda d: (_ for _ in ()).throw(RuntimeError("callback")))
        with self.assertRaises(ValueError):
            self.store.transaction(lambda d: d.update(cash=float("nan")))
        for target, attribute in ((sys.modules["adaptive_crypto.persistence"], "encode"), (self.db, "_write_rows"), (self.db, "_commit")):
            with self.subTest(attribute=attribute), patch.object(target, attribute, side_effect=sqlite3.OperationalError("injected failure")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.store.transaction(lambda d: d["warnings"].append("uncommitted"))
            self.assertEqual(self.store.snapshot(), before)
            self.assertFalse(self.db._connection.in_transaction)
        self.reopen()
        self.assertEqual(self.store.snapshot(), before)

    def test_uncertain_commit_poisoned_until_reopen(self):
        commit = self.db._commit
        def interrupted():
            commit()
            raise OSError("interrupt after commit")
        with patch.object(self.db, "_commit", side_effect=interrupted), self.assertRaises(OSError):
            self.store.transaction(lambda d: d["warnings"].append("durable"))
        with self.assertRaises(DataError):
            self.store.snapshot()
        self.reopen()
        self.assertEqual(self.store.snapshot()["warnings"], ["durable"])

    def test_concurrent_transactions_and_claims(self):
        barrier = threading.Barrier(8)
        def append(index):
            barrier.wait(5)
            for j in range(8):
                self.store.transaction(lambda d: d["warnings"].append(f"{index}:{j}"))
        with ThreadPoolExecutor(8) as executor:
            list(executor.map(append, range(8)))
        self.assertEqual(len(set(self.store.snapshot()["warnings"])), 64)
        self.event()
        sent = []
        barrier = threading.Barrier(8)
        def send(_):
            barrier.wait(5)
            return dispatch_once(self.store, "telegram", lambda event: (sent.append(event["id"]) or {"status": "sent"}), now=2)
        with ThreadPoolExecutor(8) as executor:
            self.assertEqual(sum(executor.map(send, range(8))), 1)
        self.assertEqual(sent, ["event"])

    def test_sender_observes_durable_claim_without_holding_lock(self):
        self.event()
        entered, release = threading.Event(), threading.Event()
        def sender(event):
            from adaptive_crypto.persistence import connect
            conn = connect(self.path, readonly=True)
            try:
                payload = conn.execute("SELECT payload_json FROM records WHERE collection='outbox'").fetchone()[0]
                self.assertIn('"status":"running"', payload)
            finally:
                conn.close()
            entered.set()
            self.assertTrue(release.wait(5))
            return {"status": "sent"}
        with ThreadPoolExecutor(2) as executor:
            future = executor.submit(dispatch_once, self.store, "telegram", sender, 2)
            self.assertTrue(entered.wait(5))
            try:
                executor.submit(self.store.transaction, lambda d: d["warnings"].append("not blocked")).result(3)
            finally:
                release.set()
            self.assertTrue(future.result(5))

    def test_failed_claim_never_sends_and_failed_finish_retains_running(self):
        self.event()
        with patch.object(self.db, "_commit", side_effect=OSError("claim failure")), patch("builtins.print") as sender:
            with self.assertRaises(OSError):
                dispatch_once(self.store, "telegram", sender, 2)
            sender.assert_not_called()
        commit = self.db._commit
        calls = []
        def commit_once():
            calls.append(1)
            if len(calls) == 2:
                raise OSError("finish failure")
            commit()
        with patch.object(self.db, "_commit", side_effect=commit_once), self.assertRaises(OSError):
            dispatch_once(self.store, "telegram", lambda e: {"status": "sent"}, 2)
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "running")
        self.reopen()
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "uncertain")
        self.assertFalse(dispatch_once(self.store, "telegram", lambda e: self.fail("resent"), 3))

    def test_second_owner_duplicate_adapter_and_stale_revision_are_rejected(self):
        with self.assertRaises(SystemExit):
            SQLiteDatabase(self.path.parent/"."/self.path.name)
        with self.assertRaises(DataError):
            self.db.namespace("legacy")
        child = subprocess.run([sys.executable, "-B", "-c", "from adaptive_crypto.persistence import SQLiteDatabase; import sys; SQLiteDatabase(sys.argv[1])", str(self.path)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("already owns", child.stderr)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("UPDATE namespaces SET revision=revision+1")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(DataError):
            self.store.transaction(lambda d: d["warnings"].append("stale"))
        self.reopen()
        self.assertEqual(self.store.snapshot()["warnings"], [])

    def test_external_busy_failure_is_bounded_and_does_not_poison(self):
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.db._connection.execute("PRAGMA busy_timeout=30")
            with self.assertRaises(sqlite3.OperationalError):
                self.store.transaction(lambda d: d["warnings"].append("busy"))
            self.assertEqual(self.store.snapshot()["warnings"], [])
        finally:
            conn.execute("ROLLBACK")
            conn.close()
        self.store.transaction(lambda d: d["warnings"].append("recovered"))

    def test_process_death_before_and_after_commit(self):
        self.event()
        self.db.close()
        script = '''
import sys
from pathlib import Path
from adaptive_crypto.persistence import SQLiteDatabase
from adaptive_crypto.ledger import StateStore
from adaptive_crypto.core import Rules
db=SQLiteDatabase(sys.argv[1])
s=StateStore(Path(sys.argv[1]).with_suffix('.json'), {'BTC': {'symbol':'BTC/USD','price_decimals':2}}, Rules(), backend=db.namespace('legacy'))
def pause():
    print('READY',flush=True)
    sys.stdin.readline()
if sys.argv[2]=='before':
    write=db._write_rows
    def paused(*args):
        result=write(*args)
        pause()
        return result
    db._write_rows=paused
s.transaction(lambda d: d['outbox'][0].update(status='running'))
pause()
'''
        for phase, expected in (("before", "queued"), ("after", "uncertain")):
            with self.subTest(phase=phase):
                process = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.path), phase], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                ready = queue.Queue()
                reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
                reader.start()
                try:
                    self.assertEqual(ready.get(timeout=10).strip(), "READY")
                finally:
                    process.kill()
                    process.communicate(timeout=10)
                    reader.join(2)
                self.reopen()
                self.assertEqual(self.store.snapshot()["outbox"][0]["status"], expected)
                self.db.close()

    def test_unknown_schema_fails_without_reset(self):
        self.db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version=999")
        conn.close()
        before = self.path.read_bytes()
        with self.assertRaises(DataError):
            SQLiteDatabase(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_saved_fingerprint_corruption_is_not_a_configuration_reset(self):
        self.db.close()
        import json
        connection = sqlite3.connect(self.path)
        try:
            root = json.loads(connection.execute("SELECT root_json FROM namespaces").fetchone()[0])
            root["fingerprint"] = "damaged"
            connection.execute("UPDATE namespaces SET root_json=?", (json.dumps(root),))
            connection.commit()
        finally:
            connection.close()
        with SQLiteDatabase(self.path) as database:
            with self.assertRaises(DataError):
                StateStore(self.base, ASSETS, self.rules, backend=database.namespace("legacy"))
            self.assertEqual(database.documents()["legacy"]["fingerprint"], "damaged")
        self.assertFalse((self.path.parent/"backup"/"latest.zip").exists())

    def test_rollback_failure_poisoned_and_close_recovers_uncommitted_state(self):
        connection = self.db._connection
        class BrokenRollback:
            def __getattr__(self, name):
                return getattr(connection, name)
            def execute(self, sql, *args):
                if sql == "ROLLBACK":
                    raise sqlite3.OperationalError("rollback failed")
                return connection.execute(sql, *args)
        self.db._connection = BrokenRollback()
        with patch.object(self.db, "_commit", side_effect=OSError("commit failed")), self.assertRaises(OSError):
            self.store.transaction(lambda d: d["warnings"].append("uncommitted"))
        with self.assertRaises(DataError):
            self.store.snapshot()
        self.reopen()
        self.assertEqual(self.store.snapshot()["warnings"], [])

    def test_result_copy_failure_precedes_durable_commit(self):
        class Uncopyable:
            def __deepcopy__(self, memo):
                raise TypeError("result cannot detach")
        def operation(doc):
            doc["warnings"].append("must roll back")
            return Uncopyable()
        with self.assertRaises(TypeError):
            self.store.transaction(operation)
        self.reopen()
        self.assertEqual(self.store.snapshot()["warnings"], [])
