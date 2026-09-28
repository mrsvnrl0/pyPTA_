"""Local, current-user protected Deribit credentials and secret-free settings routes."""
from __future__ import annotations

import ctypes
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from .core import DataError
from .gex import NaiveGEX


def protect_credentials(data, decrypt=False):
    """Use Windows DPAPI with its default current-user scope and no UI prompts."""
    if os.name != "nt":
        raise DataError("Saving Deribit credentials requires Windows. Use environment credentials on this system.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("Windows credential protection failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def _pair(client_id, client_secret):
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise DataError("Enter both the Deribit client ID and client secret as text.")
    values = client_id.strip(), client_secret.strip()
    if not all(values):
        raise DataError("Enter both the Deribit client ID and client secret.")
    if any(len(value) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value) for value in values):
        raise DataError("Deribit credentials must contain printable characters without spaces and be at most 4096 characters each.")
    return values


class DeribitCredentials:
    """Saved credentials override the environment; a public marker disables both."""
    def __init__(self, directory, *, environment=None, protector=None):
        self.path = Path(directory)/"deribit-credentials.dat"
        self.environment = os.environ if environment is None else environment
        self.protector = protector or protect_credentials
        self.lock = threading.RLock()

    def _read(self):
        try:
            with self.path.open("rb") as handle:
                encrypted = handle.read(65537)
        except FileNotFoundError:
            client_id = self.environment.get("DERIBIT_CLIENT_ID", "")
            secret = self.environment.get("DERIBIT_CLIENT_SECRET", "")
            if not client_id and not secret:
                return None, "public"
            try:
                return _pair(client_id, secret), "environment"
            except DataError:
                raise DataError("Deribit environment credentials are incomplete or invalid. Set both DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET, or save a local connection.") from None
        except OSError:
            raise DataError("Saved Deribit credentials cannot be read. Save the connection again or select public requests.") from None
        try:
            if not encrypted or len(encrypted) > 65536:
                raise ValueError("Invalid encrypted file")
            document = json.loads(self.protector(encrypted, True))
            if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
                raise ValueError("Invalid document")
            if document.get("enabled") is False and set(document) == {"version", "enabled"}:
                return None, "public"
            if document.get("enabled") is not True or set(document) != {"version", "enabled", "client_id", "client_secret"}:
                raise ValueError("Invalid document")
            return _pair(document["client_id"], document["client_secret"]), "saved"
        except Exception:
            # Cryptographic and decoding errors can contain plaintext; never expose them.
            raise DataError("Saved Deribit credentials cannot be unlocked. Save the connection again as this Windows user, or select public requests.") from None

    def read(self):
        with self.lock:
            return self._read()[0]

    __call__ = read

    def status(self):
        with self.lock:
            try:
                pair, source = self._read()
                return {"configured": pair is not None, "source": source, "error": None}
            except DataError as exc:
                return {"configured": False, "source": "error", "error": str(exc)}

    def _write(self, document):
        temporary = None
        try:
            encrypted = self.protector(json.dumps(document).encode("utf-8"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".deribit-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            raise DataError("Deribit connection could not be saved securely. Check access to the settings folder and try again.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def save(self, client_id, client_secret):
        pair = _pair(client_id, client_secret)
        with self.lock:
            self._write({"version": 1, "enabled": True, "client_id": pair[0], "client_secret": pair[1]})

    def disable(self):
        with self.lock:
            # Persist the choice so environment credentials cannot silently reactivate.
            self._write({"version": 1, "enabled": False})


def local_credential_request(request):
    """Require a direct loopback HTTP request, including a loopback Host header."""
    proxy_headers = {"forwarded", "via", "x-real-ip", "x-original-host", "x-original-url",
                     "x-rewrite-url", "cf-connecting-ip", "true-client-ip"}
    if request.scheme != "http" or any(key.lower() in proxy_headers or key.lower().startswith("x-forwarded-") for key in request.headers.keys()):
        return False
    try:
        if not ipaddress.ip_address(request.remote_addr or "").is_loopback:
            return False
    except ValueError:
        return False
    host = request.headers.get("Host", "").lower()
    match = re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?", host)
    return bool(match and (match[1] is None or 0 < int(match[1]) <= 65535))


def connection_status(runtime, request):
    metadata = runtime.deribit_credentials.status()
    client = runtime.deribit.status()
    configured = metadata["configured"]
    state = client.get("state")
    if metadata["error"]:
        state = "error"
    elif not configured:
        state = "public"
    elif state not in {"connecting", "connected", "error"} or client.get("mode") != "authenticated":
        state = "not_connected"
    result = {**metadata, "editable": local_credential_request(request), "state": state,
              "authenticated": configured and state == "connected" and client.get("mode") == "authenticated"}
    if state == "error" and result["error"] is None:
        # Do not echo server/protocol errors into the settings page.
        result["error"] = "Deribit authentication or market request failed. Check the connection details; market requests will retry automatically."
    for key in ("last_success_ms", "last_failure_ms"):
        value = client.get(key)
        result[key] = value if type(value) is int and value >= 0 else None
    return result


def register_deribit_routes(app, runtime, position_payload):
    from flask import jsonify, request, session

    def response(payload, status=200):
        result = jsonify(payload)
        result.status_code = status
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.get("/api/settings/deribit")
    def deribit_connection_status():
        with runtime.lock:
            return response(connection_status(runtime, request))

    def change_connection(public=False):
        if not local_credential_request(request):
            return response({"error": "Edit Deribit credentials using this computer's http://127.0.0.1 dashboard address directly."}, 403)
        if not request.is_json:
            return response({"error": "Expected a JSON connection request."}, 400)
        try:
            payload = position_payload(set() if public else {"client_id", "client_secret"})
            with runtime.lock:
                if session.get("generation") != runtime.generation:
                    raise DataError("The dashboard changed. Reload before saving this connection.")
                if runtime.stop.is_set():
                    raise DataError("The dashboard is restarting. Wait for it to return.")
                if public:
                    runtime.deribit_credentials.disable()
                else:
                    runtime.deribit_credentials.save(payload["client_id"], payload["client_secret"])
                runtime.deribit.invalidate()
                runtime.gex = NaiveGEX(runtime.assets, provider=runtime.deribit.fetch)
                runtime.wake.set()
            data = connection_status(runtime, request)
            data["message"] = "Public Deribit requests enabled." if public else "Connection saved. Authentication will be checked by the next options request."
            return response(data)
        except (DataError, KeyError, TypeError, ValueError) as exc:
            # Only our own validation messages are safe; unexpected errors use fixed text.
            message = str(exc) if isinstance(exc, DataError) else "Invalid Deribit connection request."
            return response({"error": message}, 400)
        except Exception:
            return response({"error": "Could not update the Deribit connection. Reload and check its status before retrying."}, 500)

    @app.post("/api/settings/deribit")
    def save_deribit_connection():
        return change_connection()

    @app.post("/api/settings/deribit/public")
    def use_public_deribit():
        return change_connection(public=True)
