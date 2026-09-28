"""Per-user Windows launcher for the self-contained dashboard distribution."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import webbrowser

APP_ID = "AdaptiveCryptoDashboard.Windows.1"
APP_NAME = "pyPTA"
SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY", "OPENAI_MODEL")


def data_directory():
    root = Path(os.environ["LOCALAPPDATA"])
    previous = root/"AdaptiveCryptoDashboard"/"data"
    return previous if previous.exists() else root/"pyPTA"


def initialize(directory):
    from dataclasses import asdict
    from .core import DEFAULT_ASSETS, Rules
    directory.mkdir(parents=True, exist_ok=True)
    settings = directory/"adaptive_crypto_settings.json"
    try:
        with settings.open("x", encoding="utf-8") as handle:
            json.dump({"assets": DEFAULT_ASSETS, "refresh_seconds": 15,
                       "strategy": asdict(Rules(strategy_model="neural_network"))}, handle, indent=2)
    except FileExistsError:
        pass
    return settings


def crypt(data, decrypt=False):
    """Bind optional credentials to the current Windows user using DPAPI."""
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
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def read_connections(directory):
    path = directory/"connections.dat"
    values = json.loads(crypt(path.read_bytes(), True)) if path.exists() else {}
    if not isinstance(values, dict) or any(k not in SECRET_NAMES or not isinstance(v, str) for k,v in values.items()):
        raise ValueError("Invalid saved connection settings")
    return values


def save_connections(directory, values):
    values = {key: str(values.get(key, "")).strip() for key in SECRET_NAMES}
    if bool(values["TELEGRAM_BOT_TOKEN"]) != bool(values["TELEGRAM_CHAT_ID"]):
        raise ValueError("Enter both the Telegram bot token and chat ID, or leave both empty.")
    if bool(values["OPENAI_API_KEY"]) != bool(values["OPENAI_MODEL"]):
        raise ValueError("Enter both the AI API key and model, or leave both empty.")
    encrypted = crypt(json.dumps(values).encode("utf-8"))
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encrypted)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, directory/"connections.dat")


def apply_connections(directory):
    values = read_connections(directory)
    # A fresh installation never inherits the build computer's service accounts.
    for key in SECRET_NAMES:
        os.environ.pop(key, None)
        if values.get(key):
            os.environ[key] = values[key]


def identity(directory):
    return hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()


def session_info(directory):
    import requests
    saved = json.loads((directory/"desktop-session.json").read_text(encoding="utf-8"))
    port = saved["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid desktop port")
    url = f"http://127.0.0.1:{port}"
    with requests.Session() as client:
        client.trust_env = False
        info = client.get(url+"/api/desktop/status", timeout=2).json()
    if info.get("app_id") != APP_ID or info.get("profile") != identity(directory):
        raise ValueError("The local port belongs to another application")
    return url, saved


class DesktopService:
    def __init__(self, directory, port=5000, offline=False):
        self.directory = directory.resolve()
        self.port = port
        self.offline = offline
        self.exit = threading.Event()
        self.ready = threading.Event()
        self.runtime = self.server = None
        self.url = None
        self.status = "Starting dashboard…"
        self.error = None
        self.thread = threading.Thread(target=self.run, name="desktop-server")

    def shutdown(self):
        self.exit.set()
        if self.runtime:
            self.runtime.stop.set()
            self.runtime.wake.set()
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def run(self):
        from flask import jsonify, request
        from werkzeug.serving import make_server
        from .core import load_application_settings as load_settings, safe_error
        from .state_paths import open_stores
        from .runtime import DashboardRuntime
        from .web import create_app
        from .notifications import worker
        from .ledger import atomic_json
        try:
            while not self.exit.is_set():
                apply_connections(self.directory)
                settings = initialize(self.directory)
                state = self.directory/"adaptive_crypto_reclaim_state.json"
                from .restore import recover_pending_restore
                recover_pending_restore(state, settings)
                assets, rules, refresh = load_settings(settings)
                with open_stores(state, assets, rules, "auto", settings_path=settings) as (store, positions):
                    runtime = self.runtime = DashboardRuntime(assets, rules, store, refresh,
                        position_store=positions, settings_path=settings)
                    runtime.state_base = state
                    restarting = threading.Event()
                    def restart():
                        restarting.set()
                        self.status = "Restarting dashboard…"
                        threading.Timer(0.5, self.server.shutdown).start()
                    app = create_app(runtime, restart_callback=restart)
                    token = secrets.token_urlsafe(32)
                    @app.get("/api/desktop/status")
                    def desktop_status():
                        return jsonify(app_id=APP_ID, profile=identity(self.directory))
                    @app.post("/api/desktop/stop")
                    def desktop_stop():
                        if not secrets.compare_digest(request.headers.get("X-Desktop-Token", ""), token):
                            return jsonify(error="Unauthorized"), 403
                        threading.Timer(0.2, self.shutdown).start()
                        return jsonify(stopping=True), 202
                    try:
                        server = make_server("127.0.0.1", self.port, app, threaded=True)
                    except SystemExit:
                        if self.port == 0:
                            raise
                        server = make_server("127.0.0.1", 0, app, threaded=True)
                    self.server = server
                    # Browser keep-alive threads must not block a desktop restart.
                    server.daemon_threads = True
                    self.port = server.server_port
                    self.url = f"http://127.0.0.1:{self.port}"
                    atomic_json(self.directory/"desktop-session.json", {"port":self.port, "token":token})
                    threads = []
                    if not self.offline:
                        for target, args, name in [(runtime.run, (), "market-scan"),
                            (worker, (store,"telegram",runtime.stop), "telegram"),
                            (worker, (store,"ai",runtime.stop), "ai"),
                            (worker, (positions,"telegram",runtime.stop), "holding-alerts")]:
                            thread = threading.Thread(target=target, args=args, name=name, daemon=True)
                            thread.start()
                            threads.append(thread)
                    self.status = "Running at "+self.url
                    self.ready.set()
                    try:
                        server.serve_forever(poll_interval=0.2)
                    finally:
                        runtime.stop.set()
                        runtime.wake.set()
                        for thread in threads:
                            thread.join()
                        server.server_close()
                        # Finish any admitted mutations before closing the database.
                        with runtime.activity_gate.maintenance():
                            pass
                        self.server = None
                    if not restarting.is_set():
                        break
        except BaseException as exc:
            self.error = safe_error(exc)
            self.status = "Could not start: "+self.error
            logging.exception("Desktop service failed")
        finally:
            self.runtime = None
            self.server = None
            self.ready.set()
            if not self.error:
                self.status = "Stopped"


def launch_window(service, directory, no_browser=False):
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("660x510")
    root.minsize(600, 490)
    style = ttk.Style(root)
    style.theme_use("clam")
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 19, "bold")).pack(anchor="w")
    status = tk.StringVar(value=service.status)
    ttk.Label(frame, textvariable=status, wraplength=600).pack(anchor="w", pady=(8,12))
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")
    def browse(path="/"):
        if service.url:
            webbrowser.open(service.url+path)
    ttk.Button(buttons, text="Open dashboard", command=browse).pack(side="left", padx=(0,8))
    ttk.Button(buttons, text="Dashboard settings", command=lambda:browse("/settings")).pack(side="left")
    ttk.Label(frame, text="Optional connections", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(22,4))
    ttk.Label(frame, text="Manage encrypted AI, Telegram and Deribit connections in Settings.", wraplength=600).pack(anchor="w")
    ttk.Button(frame, text="AI provider and API keys", command=lambda:browse("/settings#ai-connection")).pack(anchor="w", pady=(10,4))
    ttk.Button(frame, text="Telegram connection", command=lambda:browse("/settings#telegram-connection")).pack(anchor="w", pady=(0,4))
    ttk.Label(frame, text="Connection changes apply immediately. Existing saved connections remain available.", wraplength=600).pack(anchor="w", pady=(6,0))
    ttk.Label(frame,text="Keep this launcher open for scans and alerts. Closing it stops the dashboard.",wraplength=600).pack(anchor="w",pady=(16,4))
    ttk.Label(frame,text="Settings and records: "+str(directory),wraplength=600).pack(anchor="w")
    closing = False
    opened = False
    def close():
        nonlocal closing
        closing = True
        status.set("Stopping; finishing current scans and saving records…")
        service.shutdown()
    def poll():
        nonlocal opened
        if closing:
            if not service.thread.is_alive():
                root.destroy()
                return
        else:
            status.set(service.status)
            if service.ready.is_set() and not opened:
                opened = True
                if service.url and not no_browser:
                    browse()
        root.after(300,poll)
    root.protocol("WM_DELETE_WINDOW",close)
    service.thread.start()
    root.after(100,poll)
    root.mainloop()
    service.shutdown()
    service.thread.join()


def self_test(report):
    import tkinter
    import numpy as np
    import h5py
    import talib
    import openai
    import certifi
    from .core import Candle, load_settings
    from .neural import NeuralModel
    from .runtime import DashboardRuntime
    from .state_paths import open_stores
    from .web import create_app
    from .settings_editor import read_editor, save_editor
    from zipfile import ZipFile
    fixture = json.loads((Path(__file__).parent/"models"/"self_test_btc.json").read_text())
    model = NeuralModel()
    candles = [Candle(**row) for row in fixture["candles"]]
    frame = model.features(candles,"BTC")
    probabilities = model.probabilities(frame.iloc[-3:].to_numpy())
    np.testing.assert_allclose(probabilities,np.asarray(fixture["probabilities"]),rtol=1e-4,atol=1e-5)
    assert Path(certifi.where()).is_file()
    assert tkinter.Tcl().eval("info patchlevel")
    assert crypt(crypt(b"installer-self-test"),True) == b"installer-self-test"
    with tempfile.TemporaryDirectory(prefix="adaptive-crypto-check-") as temp:
        directory = Path(temp)
        settings = initialize(directory)
        assets,rules,refresh = load_settings(settings)
        with open_stores(directory/"state.json",assets,rules,"sqlite") as (store,positions):
            runtime = DashboardRuntime(assets,rules,store,refresh,position_store=positions,settings_path=settings)
            client = create_app(runtime).test_client()
            for route in ("/","/positions","/trade-records","/settings",
                          "/static/dashboard.css","/static/dashboard.js","/static/gex.js"):
                assert client.get(route).status_code == 200,route
            html = client.get("/").get_data(as_text=True)
            assert "pyPTA - Neural Network" in html
            assert "EternuLL Organisation" in html
            document, revision = read_editor(runtime)
            original = settings.read_bytes()
            first = save_editor(runtime, document, revision)
            with ZipFile(first["backup"]) as archive:
                assert archive.read("settings/settings.json") == original
                assert "state/state.sqlite3" in archive.namelist()
            _, revision = read_editor(runtime)
            second = save_editor(runtime, document, revision)
            assert first["backup"] == second["backup"]
            assert [p.name for p in (directory/"backup").iterdir()] == ["latest.zip"]
            assert client.get("/health").status_code == 503  # No market scan in this offline check.
            assert not positions.snapshot()["positions"]
    result = {"ok":True,"app":APP_ID,"python":sys.version.split()[0],"platform":"Windows x64",
              "model_inference":True,"pages":True,"empty_database":True,"credential_encryption":True,"tkinter":True,
              "branding":True,"single_complete_backup":True}
    Path(report).write_text(json.dumps(result,indent=2),encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--data-dir",type=Path,default=None)
    parser.add_argument("--port",type=int,default=5000)
    parser.add_argument("--headless",action="store_true")
    parser.add_argument("--offline",action="store_true",help=argparse.SUPPRESS)
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--shutdown",action="store_true")
    parser.add_argument("--self-test",action="store_true")
    parser.add_argument("--report",type=Path,default=Path("self-test.json"))
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    if args.self_test:
        try:
            self_test(args.report)
            return 0
        except BaseException as exc:
            args.report.write_text(json.dumps({"ok":False,"error":str(exc)}),encoding="utf-8")
            return 1
    directory = (args.data_dir or data_directory()).resolve()
    if args.shutdown:
        import requests
        url,saved = session_info(directory)
        with requests.Session() as client:
            client.trust_env = False
            response = client.post(url+"/api/desktop/stop",headers={"X-Desktop-Token":saved["token"]},timeout=10)
            response.raise_for_status()
        return 0
    initialize(directory)
    from .state_lock import SingleInstance
    try:
        owner = SingleInstance(directory/"desktop.lock")
    except SystemExit:
        try:
            url,_ = session_info(directory)
            if not args.no_browser and not args.headless:
                webbrowser.open(url)
            return 0
        except Exception:
            if not args.headless:
                ctypes.windll.user32.MessageBoxW(None,"The dashboard is already starting or stopping. Try again shortly.",APP_NAME,0x40)
            return 2
    mutex = None
    if directory == data_directory().resolve():
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex = kernel.CreateMutexW(None,False,"Local\\AdaptiveCryptoDashboard.Installed")
    log = RotatingFileHandler(directory/"dashboard.log",maxBytes=2_000_000,backupCount=3,encoding="utf-8")
    logging.basicConfig(level=logging.INFO,handlers=[log],format="%(asctime)s %(levelname)s %(message)s",force=True)
    if sys.stdout is None:
        sys.stdout = open(os.devnull,"w",encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(directory/"launcher-error.log","a",encoding="utf-8")
    service = DesktopService(directory,args.port,args.offline)
    try:
        if args.headless:
            service.thread.start()
            service.thread.join()
        else:
            launch_window(service,directory,args.no_browser)
        return 1 if service.error else 0
    finally:
        service.shutdown()
        if service.thread.ident is not None:
            service.thread.join()
        owner.close()
        if mutex:
            kernel.CloseHandle(mutex)
