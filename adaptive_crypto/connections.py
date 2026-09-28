"""Live credential selection and secret-free status shared by every notification worker."""
import threading
import time
from contextlib import nullcontext
from .ai_providers import ProviderError, ai_request, check_telegram
from .connection_credentials import ConnectionCredentials
from .core import DataError


class Connections:
    def __init__(self, directory, *, credentials=None):
        self.credentials = credentials or ConnectionCredentials(directory)
        self.locks = {kind: threading.RLock() for kind in ("ai", "telegram")}
        self.state_lock = threading.Lock()
        self.observations = {kind: {} for kind in self.locks}

    def status(self, kind):
        metadata = self.credentials.status(kind)
        with self.state_lock:
            observed = dict(self.observations[kind])
        state = ("error" if metadata["error"] else "off" if not metadata["enabled"] else
                 "configured" if metadata["configured"] else "not_configured")
        if metadata["configured"]:
            state = observed.get("state", state)
        return {**metadata, "state": state, "last_success_ms": observed.get("last_success_ms"),
                "last_check_ms": observed.get("last_check_ms"),
                "error": metadata["error"] or observed.get("error"), "detail": observed.get("detail")}

    def ai_configuration(self):
        status = self.credentials.status("ai")
        provider = status.get("provider", "openai")
        profile = status.get("profiles", {}).get(provider, {})
        missing = (["connection settings"] if status["error"] else ["enabled connection"] if not status["enabled"] else
                   (["API key"] if not profile.get("has_key") else [])+(["model"] if not profile.get("model") else []))
        return {"ready": status["configured"], "status": "ready" if status["configured"] else "off",
                "missing": missing, "model": profile.get("model") or None, "provider": provider}

    def change(self, kind, values=None, *, disable=False):
        # A delivery admitted with old credentials finishes before replacement.
        # No market/runtime lock is held while waiting for that bounded request.
        with self.locks[kind]:
            if disable:
                self.credentials.disable(kind)
            elif kind == "ai":
                self.credentials.save_ai(**values)
            else:
                self.credentials.save_telegram(**values)
            with self.state_lock:
                self.observations[kind] = {}

    def check(self, kind):
        with self.locks[kind]:
            try:
                config = self.credentials.read(kind)
                if config is None:
                    raise DataError("Save an enabled connection before checking it.")
                detail = ai_request(config, check=True) if kind == "ai" else check_telegram(config)
                result = {"state": "verified", "detail": detail, "error": None}
            except (DataError, ProviderError) as exc:
                result = {"state": "error", "error": str(exc), "detail": None}
            except Exception:
                result = {"state": "error", "error": "Connection check failed. Check the saved settings and try again.", "detail": None}
            with self.state_lock:
                self.observations[kind].update(result, last_check_ms=int(time.time()*1000))
        return self.status(kind)

    def dispatch(self, store, kind, now=None):
        from .notifications import _dispatch_once, telegram_send, ai_comment
        gate = getattr(store, "activity_gate", None)
        # Activity admission precedes the connection lock, matching route order.
        with gate.activity() if gate else nullcontext():
            with self.locks[kind]:
                try:
                    config = self.credentials.read(kind)
                except DataError:
                    return False
                if config is None:
                    return False
                def send(event):
                    result = (ai_comment if kind == "ai" else telegram_send)(event, config=config)
                    with self.state_lock:
                        if result["status"] in {"sent", "done"}:
                            self.observations[kind].update(state="working", error=None, detail="Latest delivery completed.",
                                                           last_success_ms=int(time.time()*1000))
                        else:
                            self.observations[kind].update(state="error", error=result.get("error"), detail=None)
                    return result
                return _dispatch_once(store, kind, send, now)
