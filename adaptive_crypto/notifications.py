"""Optional Telegram delivery and AI commentary from the durable outbox."""
from __future__ import annotations

import copy
import os
import time
import requests
from contextlib import nullcontext
from .core import safe_error
from .position_options import alerts_enabled, paper_alert_allowed
from .connection_credentials import environment_ai, PROVIDERS, validate_telegram
from .ai_providers import ai_request, ProviderError


def telegram_send(event, config=None):
    config = config if config is not None else {"bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""), "chat_id": os.getenv("TELEGRAM_CHAT_ID", "")}
    try:
        token, chat = validate_telegram(config["bot_token"], config["chat_id"])
    except Exception:
        return {"status": "failed", "error": "Telegram connection is incomplete. Save a bot token and chat ID."}
    try:
        with requests.Session() as client:
            client.trust_env = False
            response = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                   json={"chat_id": chat, "text": event["text"][:4000]}, timeout=(4, 10), allow_redirects=False)
        body = response.json()
    except Exception:
        # A timeout may happen after delivery; never claim it was definitely lost.
        return {"status": "uncertain", "error": "Telegram delivery could not be confirmed. Check the chat before sending again."}
    if response.status_code == 200 and isinstance(body, dict) and body.get("ok") is True and isinstance(body.get("result"), dict):
        return {"status": "sent", "message_id": body.get("result", {}).get("message_id"), "error": None}
    if isinstance(body, dict) and body.get("error_code") == 429 and event["attempts"] < 5:
        try:
            delay = max(1, min(3600, int(body.get("parameters", {}).get("retry_after", 30))))
        except (ValueError, TypeError, AttributeError, OverflowError):
            delay = 30
        return {"status": "queued", "retry_ms": int(time.time()*1000)+delay*1000, "error": "Telegram rate limit; scheduled retry"}
    return {"status": "failed", "error": "Telegram rejected the alert. Check the bot token, chat ID and delivery permissions."}


def ai_configuration(environ=None):
    """Report local prerequisites without exposing keys or claiming API access."""
    environ = os.environ if environ is None else environ
    document = environment_ai(environ)
    provider = document["provider"]
    profile = document["profiles"].get(provider, {})
    has_key = bool(profile.get("api_key"))
    model = profile.get("model") or None
    missing = []
    if not has_key:
        missing.append("API key")
    if not model:
        missing.append("model")
    if provider not in PROVIDERS:
        missing.append("supported provider")
    return {"ready": not missing, "status": "off" if missing else "ready",
            "missing": missing, "model": model, "provider": provider}


def ai_comment(event, config=None):
    try:
        if config is None:
            document = environment_ai(os.environ)
            config = {"provider": document["provider"], **document["profiles"].get(document["provider"], {})}
        text = ai_request(config, event["payload"])
        return {"status": "done", "commentary": text, "error": None,
                "ai_provider": config["provider"], "ai_model": config["model"]}
    except ProviderError as exc:
        return {"status": "failed", "error": str(exc)}
    except Exception:
        return {"status": "failed", "error": "AI commentary failed. Check the selected provider, model and connection."}


def dispatch_once(store, kind, sender, now=None):
    gate = getattr(store, "activity_gate", None)
    with gate.activity() if gate else nullcontext():
        return _dispatch_once(store, kind, sender, now)


def _dispatch_once(store, kind, sender, now=None):
    now = int(time.time()*1000) if now is None else now
    preference_store = getattr(store, "paper_alert_store", None)
    preferences = preference_store.snapshot().get("paper_alert_preferences", {}) if preference_store else {}
    def claim(document):
        positions = {p["id"]: p for p in document.get("positions", [])}
        for event in document["outbox"]:
            if event["status"] == "queued" and event.get("retired"):
                event.update(status="cancelled", error=event.get("error") or "Retired alert cannot be delivered.")
            if event["status"] == "queued" and preferences and not paper_alert_allowed(document, event, preferences, store.paper_namespace):
                event.update(status="cancelled", error="Alerts disabled for this paper position.")
            if event["status"] == "queued" and event.get("position_ids") and any(
                    p_id in positions and (positions[p_id]["status"] != "open" or not alerts_enabled(positions[p_id], event.get("alert_type")))
                    for p_id in event["position_ids"]):
                event.update(status="cancelled", retired=True, error="This position is closed or its alerts are off.")
            if (event["kind"] == kind and event["status"] == "queued"
                    and event.get("expires_ms", now+1) <= now):
                event.update(status="cancelled", error="Alert price observation expired before delivery; waiting for a fresh signal or price.")
        event = next((e for e in document["outbox"] if e["kind"] == kind and e["status"] == "queued" and e["retry_ms"] <= now), None)
        if event is None:
            return None
        event["status"] = "running"
        event["attempts"] += 1
        return copy.deepcopy(event)
    # Avoid disk writes on an idle worker.
    if not any(e["kind"] == kind and e["status"] == "queued" and
               (e["retry_ms"] <= now or e.get("expires_ms", now+1) <= now) for e in store.snapshot()["outbox"]):
        return False
    event = store.transaction(claim)
    if event is None:
        return False
    try:
        result = sender(event)
    except Exception as exc:
        result = {"status": "uncertain" if kind == "telegram" else "failed", "error": safe_error(exc)}
    def finish(document):
        current = next(e for e in document["outbox"] if e["id"] == event["id"])
        current.update(result)
    store.transaction(finish)
    return True


def worker(store, kind, stop_event):
    while not stop_event.is_set():
        connections = getattr(store, "connections", None)
        if connections is not None:
            try:
                connections.dispatch(store, kind)
            except Exception:
                print(f"{kind} worker: delivery processing failed", flush=True)
            stop_event.wait(1)
            continue
        configured = (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")) if kind == "telegram" else ai_configuration()["ready"]
        if configured:
            try:
                dispatch_once(store, kind, telegram_send if kind == "telegram" else ai_comment)
            except Exception as exc:
                print(f"{kind} worker: {safe_error(exc)}", flush=True)
        stop_event.wait(1)
