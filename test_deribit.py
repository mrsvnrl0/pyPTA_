"""Offline transport, token lifecycle and secret-boundary tests for Deribit."""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import requests

from adaptive_crypto.core import DataError
from adaptive_crypto.deribit import API_URL, AUTH_COOLDOWN, DeribitClient


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def wall(self):
        return 1800000000.0 + self.now


class Response:
    def __init__(self, result=None, *, error=None, status=200, body=None):
        self.status_code = status
        self.body = body if body is not None else ({"error": error} if error else {"result": result})

    def json(self):
        return self.body


class Transport:
    def __init__(self, handler=None):
        self.calls = []
        self.lock = threading.Lock()
        self.handler = handler
        self.sessions = []

    def session(self):
        transport = self

        class Session:
            trust_env = True

            def __enter__(self):
                transport.sessions.append(self)
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, **kwargs):
                with transport.lock:
                    transport.calls.append((url, copy.deepcopy(kwargs)))
                if transport.handler:
                    response = transport.handler(url, kwargs)
                    if response is not None:
                        return response
                method = kwargs["json"]["method"]
                if method == "public/auth":
                    return Response({"access_token": "TEST-ACCESS-TOKEN", "expires_in": 100,
                                     "token_type": "bearer"})
                if method == "public/get_index_price":
                    return Response({"index_price": 123.5})
                return Response([])

        return Session()

    def count(self, method):
        return sum(kwargs["json"]["method"] == method for _, kwargs in self.calls)


class DeribitClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.credentials = ("TEST-CLIENT-ID", "TEST-CLIENT-SECRET")
        self.transport = Transport()
        self.client = DeribitClient(lambda: self.credentials, self.transport.session,
                                    self.clock, self.clock.wall)

    def test_public_mode_uses_three_read_only_requests_without_auth(self):
        self.credentials = None
        self.assertEqual(self.client.fetch("BTC", "BTC"), ([], [], 123.5))
        self.assertEqual(len(self.transport.calls), 3)
        for url, kwargs in self.transport.calls:
            self.assertTrue(url.startswith(API_URL + "public/get_"))
            self.assertNotIn("Authorization", kwargs["headers"])
            self.assertNotIn("params", kwargs)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], (4, 8))
        self.assertEqual(self.client.status()["state"], "public")
        self.assertFalse(self.client.status()["configured"])

    def test_auth_credentials_only_in_post_body_and_bearer_on_every_read(self):
        self.client.fetch("BTC", "BTC")
        self.assertEqual(self.transport.count("public/auth"), 1)
        url, auth = self.transport.calls[0]
        self.assertEqual(url, API_URL + "public/auth")
        self.assertEqual(auth["json"]["params"]["client_secret"], self.credentials[1])
        self.assertNotIn("Authorization", auth["headers"])
        self.assertIn("account:none trade:none wallet:none", auth["json"]["params"]["scope"])
        for url, kwargs in self.transport.calls:
            self.assertNotIn("?", url)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["json"]["jsonrpc"], "2.0")
            self.assertNotIn("params", kwargs)
            if kwargs["json"]["method"] != "public/auth":
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer TEST-ACCESS-TOKEN")
                self.assertNotIn("TEST-CLIENT", json.dumps(kwargs))
        self.assertTrue(all(not session.trust_env for session in self.transport.sessions))
        self.assertEqual(self.client.status()["state"], "connected")
        self.assertNotIn("TEST-", json.dumps(self.client.status()))

    def test_token_cache_shared_between_assets_and_refreshes_before_expiry(self):
        self.client.fetch("BTC", "BTC")
        self.client.fetch("ETH", "ETH")
        self.client.fetch("SOL", "USDC")
        self.assertEqual(self.transport.count("public/auth"), 1)
        self.assertEqual(self.transport.calls[-1][1]["json"]["params"], {"index_name": "sol_usdc"})
        self.clock.now += 90
        self.assertEqual(self.client.status()["state"], "not_connected")
        self.client.fetch("BTC", "BTC")
        self.assertEqual(self.transport.count("public/auth"), 2)

    def test_concurrent_assets_authenticate_once_and_status_never_waits_for_auth(self):
        entered, release = threading.Event(), threading.Event()

        def handler(url, kwargs):
            if kwargs["json"]["method"] == "public/auth":
                entered.set()
                self.assertTrue(release.wait(2))

        self.transport.handler = handler
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.client.fetch, "BTC", "BTC")
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.client.fetch, "ETH", "ETH")
            status_future = pool.submit(self.client.status)
            try:
                self.assertEqual(status_future.result(timeout=.5)["state"], "connecting")
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        self.assertEqual(self.transport.count("public/auth"), 1)

    def test_invalid_token_reauthenticates_once_then_retries_only_failed_read(self):
        failures = [True]

        def handler(url, kwargs):
            if kwargs["json"]["method"] == "public/get_book_summary_by_currency" and failures:
                failures.pop()
                return Response(error={"code": 13009, "message": "unauthorized"})

        self.transport.handler = handler
        self.client.fetch("BTC", "BTC")
        self.assertEqual(self.transport.count("public/auth"), 2)
        self.assertEqual(self.transport.count("public/get_instruments"), 1)
        self.assertEqual(self.transport.count("public/get_book_summary_by_currency"), 2)
        self.assertEqual(self.client.status()["state"], "connected")

    def test_persistent_invalid_token_has_one_retry_and_auth_cooldown(self):
        def handler(url, kwargs):
            if kwargs["json"]["method"] != "public/auth":
                return Response(status=401)

        self.transport.handler = handler
        for _ in range(3):
            with self.assertRaisesRegex(DataError, "authentication failed"):
                self.client.fetch("BTC", "BTC")
        self.assertEqual(self.transport.count("public/auth"), 2)
        self.assertEqual(self.transport.count("public/get_instruments"), 2)
        self.assertEqual(self.client.status()["state"], "error")
        self.clock.now += AUTH_COOLDOWN
        with self.assertRaises(DataError):
            self.client.fetch("ETH", "ETH")
        self.assertEqual(self.transport.count("public/auth"), 4)

    def test_auth_failure_is_sanitized_and_never_falls_back(self):
        def handler(url, kwargs):
            return Response(error={"code": 13004, "message": "TEST-CLIENT-SECRET echoed"})

        self.transport.handler = handler
        for _ in range(3):
            with self.assertRaisesRegex(DataError, "authentication failed") as raised:
                self.client.fetch("BTC", "BTC")
            self.assertNotIn("TEST-", str(raised.exception))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertNotIn("TEST-", json.dumps(self.client.status()))
        self.assertGreater(self.client.status()["retry_at_ms"], self.clock.wall() * 1000)

    def test_auth_cooldown_clears_when_credentials_change(self):
        self.transport.handler = lambda url, kwargs: Response(status=403)
        with self.assertRaises(DataError):
            self.client.fetch("BTC", "BTC")
        self.credentials = ("ROTATED-ID", "ROTATED-SECRET")
        self.transport.handler = None
        self.client.fetch("BTC", "BTC")
        self.assertEqual(self.transport.count("public/auth"), 2)
        self.assertEqual(self.client.status()["state"], "connected")

    def test_removing_credentials_drops_token_and_uses_public_mode(self):
        self.client.fetch("BTC", "BTC")
        self.credentials = None
        self.client.fetch("ETH", "ETH")
        for _, kwargs in self.transport.calls[-3:]:
            self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual(self.client.status()["state"], "public")
        self.assertIsNone(self.client.status()["token_expires_ms"])

    def test_incomplete_credentials_and_loader_errors_do_not_use_network(self):
        for value in (("id", ""), ("", "secret"), ("only-one",), "secret", (123, "secret")):
            self.credentials = value
            with self.assertRaisesRegex(DataError, "credentials could not be read"):
                self.client.fetch("BTC", "BTC")
        self.assertEqual(self.transport.calls, [])
        def failed_reader():
            raise ValueError("TEST-CLIENT-SECRET")
        client = DeribitClient(failed_reader, self.transport.session)
        with self.assertRaisesRegex(DataError, "credentials could not be read") as raised:
            client.fetch("BTC", "BTC")
        self.assertNotIn("TEST-", str(raised.exception))
        self.assertTrue(client.status()["configured"])

    def test_network_exceptions_and_server_messages_cannot_leak_secrets(self):
        def fail(url, kwargs):
            if kwargs["json"]["method"] != "public/auth":
                raise requests.ConnectionError("url?TEST-CLIENT-SECRET&token=TEST-ACCESS-TOKEN")
        self.transport.handler = fail
        with self.assertRaisesRegex(DataError, "request failed") as raised:
            self.client.fetch("BTC", "BTC")
        self.assertNotIn("TEST-", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("TEST-", json.dumps(self.client.status()))

    def test_session_supplied_dataerror_is_also_sanitized(self):
        def fail(url, kwargs):
            if kwargs["json"]["method"] != "public/auth":
                raise DataError("TEST-CLIENT-SECRET in external exception")
        self.transport.handler = fail
        with self.assertRaisesRegex(DataError, "request failed") as raised:
            self.client.fetch("BTC", "BTC")
        self.assertNotIn("TEST-", str(raised.exception))
        self.assertNotIn("TEST-", json.dumps(self.client.status()))

    def test_retry_budget_is_shared_by_all_reads_in_one_fetch(self):
        failed_first = []
        def handler(url, kwargs):
            method = kwargs["json"]["method"]
            if method == "public/get_instruments" and not failed_first:
                failed_first.append(True)
                return Response(error={"code": 13009})
            if method == "public/get_book_summary_by_currency":
                return Response(error={"code": 13009})
        self.transport.handler = handler
        with self.assertRaisesRegex(DataError, "authentication failed"):
            self.client.fetch("BTC", "BTC")
        self.assertEqual(self.transport.count("public/auth"), 2)
        self.assertEqual(self.transport.count("public/get_book_summary_by_currency"), 1)
        self.assertEqual(self.transport.count("public/get_index_price"), 0)

    def test_redirect_is_not_followed_and_token_is_not_retried(self):
        def handler(url, kwargs):
            if kwargs["json"]["method"] != "public/auth":
                return Response(status=302)
        self.transport.handler = handler
        with self.assertRaisesRegex(DataError, "redirected"):
            self.client.fetch("BTC", "BTC")
        self.assertEqual(len(self.transport.calls), 2)

    def test_bad_auth_response_does_not_send_any_market_requests(self):
        for result in ({}, {"access_token": "bad\r\ntoken", "expires_in": 100},
                       {"access_token": "token", "expires_in": True},
                       {"access_token": "token", "expires_in": float("nan")},
                       {"access_token": "token", "expires_in": 0},
                       {"access_token": "token", "expires_in": 100, "token_type": None}):
            with self.subTest(result=result):
                transport = Transport(lambda url, kwargs: Response(result))
                client = DeribitClient(lambda: self.credentials, transport.session)
                with self.assertRaisesRegex(DataError, "authentication failed"):
                    client.fetch("BTC", "BTC")
                self.assertEqual(len(transport.calls), 1)

    def test_invalid_market_responses_are_sanitized(self):
        for bad in (Response(body=[]), Response(body={"malformed": "TEST-CLIENT-SECRET"}),
                    Response(error={"code": 11050, "message": "TEST-CLIENT-SECRET"}),
                    Response({"index_price": float("nan")}), Response({"index_price": True})):
            def handler(url, kwargs):
                if kwargs["json"]["method"] == "public/get_index_price":
                    return bad
            self.transport.handler = handler
            with self.assertRaises(DataError) as raised:
                self.client.fetch("BTC", "BTC")
            self.assertNotIn("TEST-", str(raised.exception))

    def test_invalidation_during_auth_is_nonblocking_and_discards_old_token(self):
        entered, release = threading.Event(), threading.Event()
        def handler(url, kwargs):
            if kwargs["json"]["method"] == "public/auth":
                entered.set()
                self.assertTrue(release.wait(2))
        self.transport.handler = handler
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.client.fetch, "BTC", "BTC")
            self.assertTrue(entered.wait(2))
            try:
                pool.submit(self.client.invalidate).result(timeout=.5)
                self.assertEqual(self.client.status()["state"], "not_connected")
            finally:
                release.set()
            with self.assertRaisesRegex(DataError, "credentials changed"):
                pending.result(timeout=2)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertIsNone(self.client.status()["error"])
        self.transport.handler = None
        self.client.fetch("BTC", "BTC")
        self.assertEqual(self.transport.count("public/auth"), 2)

    def test_status_reads_neither_credentials_nor_transport_and_returns_copy(self):
        def fail():
            raise AssertionError("No IO allowed")
        client = DeribitClient(fail, fail)
        status = client.status()
        status["state"] = "connected"
        self.assertEqual(client.status()["state"], "not_connected")
        client.invalidate()
        self.assertIsNone(client.status()["configured"])

    def test_unsupported_asset_cannot_issue_requests(self):
        for pair in (("DOGE", "USDC"), ("BTC", "USDC"), ("ETH", "BTC")):
            with self.assertRaisesRegex(DataError, "Unsupported"):
                self.client.fetch(*pair)
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
