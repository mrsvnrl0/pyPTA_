"""Launch the paper signal dashboard or perform one public-data scan."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading
from .core import load_application_settings as load_settings
from .state_lock import SingleInstance  # Compatibility re-export.
from .state_paths import open_stores
from .notifications import worker
from .runtime import DashboardRuntime
from .web import create_app

BASE = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=BASE/"adaptive_crypto_settings.json")
    parser.add_argument("--state", type=Path, default=BASE/"adaptive_crypto_reclaim_state.json")
    parser.add_argument("--state-backend", choices=("auto", "json", "sqlite"), default="auto")
    parser.add_argument("--once", action="store_true", help="Scan public market data once; no AI or Telegram calls")
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "5000")))
    args = parser.parse_args()
    while True:
        from .restore import recover_pending_restore
        recover_pending_restore(args.state, args.settings)
        assets, rules, refresh = load_settings(args.settings)
        with open_stores(args.state, assets, rules, args.state_backend, settings_path=args.settings) as (store, holdings):
            restart = _run(args, assets, rules, refresh, store, holdings)
        if not restart:
            break


def _run(args, assets, rules, refresh, store, holdings):
    runtime = None
    threads = []
    server = None
    restarting = threading.Event()
    try:
        runtime = DashboardRuntime(assets, rules, store, refresh, position_store=holdings, settings_path=args.settings)
        runtime.state_base = args.state
        if args.once:
            runtime.scan_once()
            snap = runtime.snapshot()
            print(json.dumps({"assets": snap["data"], "portfolio": snap["portfolio"]}, indent=2, allow_nan=False))
            return
        def start(target, name, *args):
            thread = threading.Thread(target=target, args=args, daemon=True, name=name)
            thread.start()
            threads.append(thread)
        start(runtime.run, "market-scan")
        start(runtime.market_brief.run, "market-brief")
        for kind in ("telegram", "ai"):
            start(worker, kind, store, kind, runtime.stop)
        start(worker, "holding-alerts", runtime.positions, "telegram", runtime.stop)
        def request_restart():
            restarting.set()
            # Let the accepted response leave the request thread before closing
            # the listener. Worker joins and store closure happen below.
            threading.Timer(0.5, server.shutdown).start()
        app = create_app(runtime, restart_callback=request_restart)
        from werkzeug.serving import make_server
        server = make_server(args.host, args.port, app, threaded=True)
        server.daemon_threads = False
        print(f"Signal dashboard: http://{args.host}:{args.port} · paper tracker only", flush=True)
        server.serve_forever()
    finally:
        if runtime:
            runtime.stop.set()
            runtime.wake.set()
            runtime.market_brief.wake.set()
        for thread in threads:
            thread.join()
        if server:
            server.server_close()
    return restarting.is_set()
