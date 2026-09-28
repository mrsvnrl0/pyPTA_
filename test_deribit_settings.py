"""Credential protection, local request boundaries, and offline connection lifecycle."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.deribit_settings import DeribitCredentials
from adaptive_crypto.ledger import StateStore, atomic_json
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.web import create_app


ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}
CLIENT_ID = "disposable-client-identifier"
CLIENT_SECRET = "disposable-test-secret"


def fake_protector(data, decrypt=False):
    """Reversible test double; production always uses DPAPI."""
    if decrypt:
        if not data.startswith(b"test:"):
            raise ValueError(CLIENT_SECRET)
        return data[5:][::-1]
    return b"test:"+data[::-1]


class DeribitCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {}
        self.store = DeribitCredentials(self.root, environment=self.environment, protector=fake_protector)

    def test_default_public_and_complete_environment_do_not_write_files(self):
        self.assertIsNone(self.store())
        self.assertEqual(self.store.status(), {"configured": False, "source": "public", "error": None})
        self.environment.update(DERIBIT_CLIENT_ID=CLIENT_ID, DERIBIT_CLIENT_SECRET=CLIENT_SECRET)
        self.assertEqual(self.store.read(), (CLIENT_ID, CLIENT_SECRET))
        self.assertEqual(self.store.status()["source"], "environment")
        self.assertFalse(self.store.path.exists())

    def test_incomplete_or_invalid_environment_fails_closed_without_values(self):
        for environment in ({"DERIBIT_CLIENT_ID": CLIENT_ID}, {"DERIBIT_CLIENT_SECRET": CLIENT_SECRET},
                            {"DERIBIT_CLIENT_ID": CLIENT_ID, "DERIBIT_CLIENT_SECRET": "\n"}):
            self.environment.clear()
            self.environment.update(environment)
            with self.assertRaisesRegex(DataError, "incomplete or invalid") as raised:
                self.store.read()
            self.assertNotIn(CLIENT_ID, str(raised.exception))
            self.assertNotIn(CLIENT_SECRET, json.dumps(self.store.status()))
            self.assertEqual(self.store.status()["source"], "error")

    def test_saved_credentials_override_environment_and_public_choice_persists(self):
        self.environment.update(DERIBIT_CLIENT_ID="environment-id", DERIBIT_CLIENT_SECRET="environment-secret")
        self.store.save(CLIENT_ID, CLIENT_SECRET)
        self.assertEqual(self.store(), (CLIENT_ID, CLIENT_SECRET))
        self.assertEqual(self.store.status()["source"], "saved")
        raw = self.store.path.read_bytes()
        self.assertNotIn(CLIENT_ID.encode(), raw)
        self.assertNotIn(CLIENT_SECRET.encode(), raw)
        self.store.disable()
        restarted = DeribitCredentials(self.root, environment=self.environment, protector=fake_protector)
        self.assertIsNone(restarted())
        self.assertEqual(restarted.status()["source"], "public")
        self.assertTrue(self.store.path.exists())

    def test_invalid_credentials_leave_saved_connection_untouched(self):
        self.store.save(CLIENT_ID, CLIENT_SECRET)
        before = self.store.path.read_bytes()
        for values in (("", CLIENT_SECRET), (CLIENT_ID, ""), (None, CLIENT_SECRET),
                       (CLIENT_ID, 12), (CLIENT_ID, "abc\ndef"), ("x"*4097, CLIENT_SECRET)):
            with self.assertRaises(DataError):
                self.store.save(*values)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_corrupt_saved_file_never_falls_back_to_environment(self):
        self.environment.update(DERIBIT_CLIENT_ID=CLIENT_ID, DERIBIT_CLIENT_SECRET=CLIENT_SECRET)
        for raw in (b"broken", b"", b"x"*65537,
                    fake_protector(json.dumps({"version": True, "enabled": False}).encode()),
                    fake_protector(json.dumps({"version": 1, "enabled": False, "client_id": CLIENT_ID}).encode())):
            self.store.path.write_bytes(raw)
            with self.assertRaisesRegex(DataError, "cannot be unlocked"):
                self.store.read()
            status = self.store.status()
            self.assertFalse(status["configured"])
            self.assertNotIn(CLIENT_ID, json.dumps(status))
            self.assertNotIn(CLIENT_SECRET, json.dumps(status))

    def test_atomic_write_failure_preserves_previous_credentials_and_removes_temp(self):
        self.store.save(CLIENT_ID, CLIENT_SECRET)
        with patch("adaptive_crypto.deribit_settings.os.replace", side_effect=OSError(CLIENT_SECRET)):
            with self.assertRaisesRegex(DataError, "could not be saved securely") as raised:
                self.store.save("new-id", "new-secret")
        self.assertNotIn(CLIENT_SECRET, str(raised.exception))
        self.assertEqual(self.store(), (CLIENT_ID, CLIENT_SECRET))
        self.assertEqual(list(self.root.glob(".deribit-*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI requires Windows")
    def test_real_dpapi_round_trip_in_temporary_folder(self):
        store = DeribitCredentials(self.root, environment={})
        store.save(CLIENT_ID, CLIENT_SECRET)
        self.assertEqual(store.read(), (CLIENT_ID, CLIENT_SECRET))
        for value in (CLIENT_ID, CLIENT_SECRET):
            self.assertNotIn(value.encode(), store.path.read_bytes())
        store.disable()
        self.assertIsNone(store.read())


class DeribitSettingsRoutesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rules = Rules()
        self.settings = self.root/"configuration"/"settings.json"
        atomic_json(self.settings, {"assets": [{"name": "BTC", **ASSETS["BTC"]}],
                                    "strategy": asdict(self.rules), "refresh_seconds": 15})
        self.store = StateStore(self.root/"state"/"study.json", ASSETS, self.rules)
        self.runtime = DashboardRuntime(ASSETS, self.rules, self.store, settings_path=self.settings)
        self.environment = {}
        self.runtime.deribit_credentials = DeribitCredentials(self.settings.parent, environment=self.environment, protector=fake_protector)
        self.runtime.deribit._credentials = self.runtime.deribit_credentials.read
        self.app = create_app(self.runtime)
        self.client = self.app.test_client()
        self.client.get("/settings")
        with self.client.session_transaction() as session:
            self.token = session["csrf_token"]

    def post(self, public=False, **kwargs):
        body = {} if public else {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        body.update(kwargs.pop("body", {}))
        return self.client.post("/api/settings/deribit"+("/public" if public else ""), json=body,
                                headers={"X-CSRF-Token": self.token, **kwargs.pop("headers", {})}, **kwargs)

    def test_default_runtime_wires_authenticated_provider_and_settings_directory(self):
        runtime = DashboardRuntime(ASSETS, self.rules, self.store, settings_path=self.settings)
        self.assertEqual(runtime.gex.provider, runtime.deribit.fetch)
        self.assertEqual(runtime.deribit_credentials.path.parent, self.settings.parent)
        fallback = DashboardRuntime(ASSETS, self.rules, self.store)
        self.assertEqual(fallback.deribit_credentials.path.parent, self.store.path.parent)

    def test_local_save_is_secret_free_does_not_fetch_and_replaces_gex(self):
        previous = self.runtime.gex
        with patch.object(self.runtime.deribit, "fetch") as fetch:
            response = self.post()
            fetch.assert_not_called()
            self.assertEqual(self.runtime.gex.provider, fetch)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.runtime.deribit_credentials(), (CLIENT_ID, CLIENT_SECRET))
        self.assertIsNot(previous, self.runtime.gex)
        self.assertTrue(self.runtime.wake.is_set())
        self.assertEqual(response.json["state"], "not_connected")
        self.assertFalse(response.json["authenticated"])
        self.assertTrue(response.json["configured"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for value in (CLIENT_ID, CLIENT_SECRET):
            self.assertNotIn(value, response.get_data(as_text=True))
            self.assertNotIn(value, self.client.get("/settings").get_data(as_text=True))

    def test_public_choice_overrides_environment_and_resets_connection(self):
        self.environment.update(DERIBIT_CLIENT_ID=CLIENT_ID, DERIBIT_CLIENT_SECRET=CLIENT_SECRET)
        self.assertEqual(self.client.get("/api/settings/deribit").json["source"], "environment")
        with patch.object(self.runtime.deribit, "invalidate", wraps=self.runtime.deribit.invalidate) as invalidate:
            response = self.post(public=True)
            invalidate.assert_called_once()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNone(self.runtime.deribit_credentials())
        self.assertEqual(response.json["state"], "public")
        self.assertFalse(response.json["configured"])

    def test_status_reports_authenticated_only_after_confirmed_client_success(self):
        self.runtime.deribit_credentials.save(CLIENT_ID, CLIENT_SECRET)
        self.assertEqual(self.client.get("/api/settings/deribit").json["state"], "not_connected")
        with patch.object(self.runtime.deribit, "status", return_value={"state": "connected", "mode": "authenticated", "last_success_ms": 123}):
            result = self.client.get("/api/settings/deribit").json
            self.assertTrue(result["authenticated"])
            self.assertEqual(result["last_success_ms"], 123)
        with patch.object(self.runtime.deribit, "status", return_value={"state": "error", "mode": "authenticated", "error": CLIENT_SECRET, "client_id": CLIENT_ID}):
            result = self.client.get("/api/settings/deribit")
            self.assertEqual(result.json["state"], "error")
            self.assertNotIn(CLIENT_ID, result.get_data(as_text=True))
            self.assertNotIn(CLIENT_SECRET, result.get_data(as_text=True))

    def test_remote_status_is_read_only_and_remote_host_proxy_mutations_are_rejected(self):
        remote = {"environ_overrides": {"REMOTE_ADDR": "192.0.2.1"}}
        result = self.client.get("/api/settings/deribit", **remote)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json["editable"])
        cases = [remote, {"base_url": "http://example.test"}, {"base_url": "http://localhost.evil.test"},
                 {"base_url": "https://localhost"}, {"headers": {"Forwarded": "for=127.0.0.1"}},
                 {"headers": {"X-Forwarded-For": "127.0.0.1"}}, {"headers": {"X-Forwarded-Host": "localhost"}},
                 {"headers": {"X-Real-IP": "127.0.0.1"}}, {"headers": {"Via": "proxy"}}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.post(**kwargs).status_code, 403)
                self.assertEqual(self.post(public=True, **kwargs).status_code, 403)
        self.assertFalse(self.runtime.deribit_credentials.path.exists())

    def test_ipv6_loopback_and_explicit_ipv4_host_are_accepted(self):
        for base, remote in (("http://127.0.0.1:5000", "127.0.0.1"), ("http://[::1]:5000", "::1")):
            # Session cookies are host-scoped, so initialize the session for each host.
            self.client.get("/settings", base_url=base)
            with self.client.session_transaction(base_url=base) as session:
                self.token = session["csrf_token"]
            result = self.post(base_url=base, environ_overrides={"REMOTE_ADDR": remote})
            self.assertEqual(result.status_code, 200, result.json)

    def test_mutations_require_csrf_fresh_generation_and_exact_json_schema(self):
        self.assertEqual(self.client.post("/api/settings/deribit", json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).status_code, 400)
        self.assertEqual(self.post(body={"unexpected": True}).status_code, 400)
        self.assertEqual(self.post(body={"client_secret": ""}).status_code, 400)
        self.assertEqual(self.post(public=True, body={"client_id": CLIENT_ID}).status_code, 400)
        self.assertEqual(self.client.post("/api/settings/deribit", data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}, headers={"X-CSRF-Token": self.token}).status_code, 400)
        self.runtime.generation = "different"
        self.assertEqual(self.post().status_code, 400)
        self.assertFalse(self.runtime.deribit_credentials.path.exists())


if __name__ == "__main__":
    unittest.main()
