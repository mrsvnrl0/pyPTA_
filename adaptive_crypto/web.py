"""Flask routes and presentation-only measurement formatting."""
from __future__ import annotations

import copy
import secrets
import time
import uuid
from flask import Flask, jsonify, render_template, request, session, redirect, url_for, g
from .core import H4, DataError, Rules, finite, utc, safe_error
from .market_data import LiveCharts
from .gex import NaiveGEX, map_smc
from .administration import apply_settings, purge_database, save_model_selection
from .settings_editor import read_editor, field_groups, save_editor, check_restart
from .trade_records import paper_records
from .position_options import cancel_disabled_paper_alerts
from .neural_models import model_catalog, public_report, candidate_bundle_path


def number(value, places=3):
    try:
        return f"{finite(value):,.{places}f}"
    except (ValueError, TypeError):
        return "—"


def measurement(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        # General values keep small quantities legible without exponent notation.
        return number(value, 8).rstrip("0").rstrip(".") if value != 0 else "0"
    if isinstance(value, dict):
        return "; ".join(f"{str(k).replace('_', ' ').capitalize()}: {measurement(v)}" for k, v in value.items()) or "—"
    if isinstance(value, (list, tuple)):
        return " · ".join(measurement(item) for item in value) or "—"
    return str(value)


def money(value, places=2):
    text = number(value, places)
    if text == "—":
        return text
    return f"-${text[1:]}" if text.startswith("-") else f"${text}"


def exact_measurement(value):
    if isinstance(value, dict):
        return "; ".join(f"{str(k).replace('_', ' ').capitalize()}: {exact_measurement(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " · ".join(exact_measurement(v) for v in value)
    return repr(value) if isinstance(value, float) else measurement(value)


def multiple(value):
    text = number(value, 2)
    return text if text == "—" else f"{text}×"


def measurement_rows(evidence, required=False, places=3):
    """Display-only labels and units; persisted evidence and comparisons stay raw."""
    value = evidence.get("required" if required else "measured")
    key = evidence.get("key", "")
    fields = {
        **{key: (label, "price") for key, label in {
            "liquidity_price": "Liquidity level", "sweep_extreme": "Sweep extreme", "return_close": "Return close",
            "sweep": "Sweep price", "return": "Return close", "zone_low": "Block low", "zone_high": "Block high",
            "gap_low": "Gap low", "gap_high": "Gap high", "midpoint": "Gap midpoint",
            "swing_high": "4H swing high", "swing_low": "4H swing low", "protected_level": "4H protected level",
            "swing_price": "Stop basis", "stop_price": "Stop level", "entry_price": "Entry price",
            "reference_price": "Bearish reference", "buy_price": "Spot-buy limit",
            "target_price": "Sweep take-profit"}.items()},
        "sweep_buffer_bps": ("Sweep beyond liquidity", "bps"),
        "body_atr": ("Body / ATR", "atr"),
        "close_location": ("Close in range (from low)", "fraction"),
        "close": ("Close price", "price"), "low": ("Low price", "price"),
        "open": ("Open price", "price"), "high": ("High price", "price"),
        "close_change": ("Close − open", "price"),
        "rvol": ("Relative volume", "multiple"),
        "roc_percent": ("ROC", "percent"), "ema": ("ROC signal (EMA)", "percent"),
        "age_bars": ("Age", "bars"), "zone": ("Retest zone", "price_range"),
        "liquidity_low": ("Liquidity low", "price"),
        "previous_atr": ("Previous 4H ATR", "price"),
        "sweep_depth_atr": ("Sweep depth", "atr"),
        "retest_age_bars": ("Paired retest age", "bars"),
        "retest_opened_ms": ("Paired retest opened", "timestamp"),
        "prior_bars": ("Available prior candles", "bars"),
        "replacement_confirmation_allowed": ("May confirm a replacement break", "general"),
        "age_minutes": ("Age since confirmation close", "minutes"),
    }

    def formatted(item, kind):
        if item is None:
            return "—"
        if isinstance(item, bool):
            return measurement(item)
        if isinstance(item, str):
            return {"context only": "Context only", "positive width": "Upper bound > lower bound",
                    "> 0 and > signal": "> 0% and > ROC signal", "> 0": "> " + money(0, places)}.get(item, item)
        if kind in {"price_range", "rsi_range"} and isinstance(item, (list, tuple)) and len(item) == 2:
            unit = "price" if kind == "price_range" else "number"
            return f"{formatted(item[0], unit)} – {formatted(item[1], unit)}" + (" (inclusive)" if kind == "rsi_range" else "")
        if kind == "price":
            return money(item, places)
        if kind == "timestamp":
            return utc(item)
        if kind == "multiple":
            return multiple(item)
        if kind in {"atr", "fraction", "percent", "bars", "minutes", "number", "bps"}:
            try:
                numeric = finite(item)
            except (ValueError, TypeError):
                return "—"
            text = number(numeric * 100 if kind == "fraction" else numeric, 2)
            if kind in {"bars", "minutes"}:
                text = text.rstrip("0").rstrip(".")
            return text + {"atr": " ATR", "fraction": "%", "percent": "%", "bars": " bars", "minutes": " minutes", "number": "", "bps": " bps"}[kind]
        return measurement(item)

    if isinstance(value, dict):
        rows = []
        for field, item in value.items():
            base, operator = field, ""
            if required:
                if base.startswith("max_"):
                    base, operator = base[4:], "≤ "
                else:
                    for suffix, symbol in (("_min", "≥ "), ("_max", "≤ "), ("_below", "< "), ("_above", "> ")):
                        if base.endswith(suffix):
                            base, operator = base[:-len(suffix)], symbol
                            break
            label, kind = fields.get(base, (str(base).replace("_", " ").capitalize(), "general"))
            text = formatted(item, kind)
            rows.append((label, (operator if not isinstance(item, str) and text != "—" else "") + text))
        return rows or [("Value", "—")]

    scalar = {
        "smc_bos": ("Structure-break close", "price", ""),
        "smc_mss": ("Structure price", "price", ""),
        "smc_ifvg": ("Inversion close", "price", ""),
        "smc_target": ("Liquidity target", "price", ""),
        "liquidity": ("Liquidity low", "price", ""),
        "entry": ("Entry estimate", "price", ""),
        "body": ("Body / ATR", "atr", "≥ "),
        "close": ("Close in range (from low)", "fraction", "≥ "),
        "volume": ("Relative volume", "multiple", "≥ "),
        "break": ("Close price", "price", "> "),
        "history": ("Completed history", "bars", "≥ "),
        "cross": ("Bars since crossover", "bars", "< "),
        "trend": ("Trend conditions met", "general", ""),
        "rsi": ("RSI", "rsi_range" if required else "number", ""),
        "discount": ("Overlap zone", "price_range", ""),
        "bullish": ("Close − open", "price", ""),
        "sweep": ("Sweep depth" if required else "Liquidity low", "atr" if required else "price", "> " if required else ""),
    }
    label, kind, operator = scalar.get(key, ("Value", "general", ""))
    text = formatted(value, kind)
    return [(label, (operator if required and not isinstance(value, str) and text != "—" else "") + text)]


def create_app(runtime, chart_provider=None, gex_provider=None, restart_callback=None):
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.config.update(MAX_CONTENT_LENGTH=65536, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    def chart_fallback(name):
        with runtime.lock:
            return copy.deepcopy(runtime.chart_fallback.get(name))
    charts = LiveCharts(runtime.assets, provider=chart_provider, fallback=chart_fallback, interval=H4)
    chart_generation = runtime.generation
    if gex_provider is not None:
        runtime.gex = NaiveGEX(runtime.assets, provider=gex_provider)
    @app.before_request
    def coordinate_mutations():
        if request.method == "POST" and runtime.stop.is_set():
            return jsonify({"error": "Dashboard is restarting. Wait for it to return."}), 503
        if request.method == "POST" and request.endpoint not in {"apply_strategy_settings", "purge_strategy_database", "restart_dashboard", "update_position_alerts", "update_paper_alerts", "confirm_backup_restore"}:
            g.activity = runtime.activity_gate.activity()
            g.activity.__enter__()
            if runtime.stop.is_set():
                return jsonify({"error": "Dashboard is restarting. Wait for it to return."}), 503

    @app.teardown_request
    def release_mutation(_error):
        activity = g.pop("activity", None)
        if activity is not None:
            activity.__exit__(None, None, None)

    @app.get("/")
    def dashboard():
        with runtime.lock:
            return render_dashboard("market_dashboard.html")

    @app.get("/paper-trading")
    def paper_page():
        with runtime.lock:
            return render_dashboard("neural_dashboard.html", records=paper_records(runtime))

    @app.get("/positions")
    def positions_page():
        with runtime.lock:
            return render_dashboard("positions_page.html")

    @app.get("/trade-records")
    def trade_records_page():
        with runtime.lock:
            return render_dashboard("trade_records.html", records=paper_records(runtime))

    @app.get("/settings")
    def settings_page():
        with runtime.lock:
            if session.get("generation") != runtime.generation:
                session["generation"] = runtime.generation
                session["csrf_token"] = secrets.token_hex(32)
            document, revision = read_editor(runtime)
            return render_template("settings.html", s=runtime.snapshot(), document=document,
                                   revision=revision, groups=field_groups(document), time=utc,
                                   model_catalog=model_catalog(runtime.settings_path,
                                                               (Rules(**document["strategy"]), runtime.rules)),
                                   applied_model_id=runtime.rules.nn_model_id,
                                   csrf_token=session.setdefault("csrf_token", secrets.token_hex(32)),
                                   restart_available=restart_callback is not None)

    @app.get("/api/models/<model_id>/report")
    def candidate_model_report(model_id):
        try:
            document, _ = read_editor(runtime)
            for rules in (Rules(**document["strategy"]), runtime.rules):
                if rules.nn_model_id == model_id and rules.nn_model_bundle_path:
                    path = candidate_bundle_path(model_id, runtime.settings_path, rules.nn_model_bundle_path)
                    return jsonify(public_report(model_id, path))
            return jsonify(public_report(model_id))
        except (DataError, OSError, ValueError, TypeError):
            return jsonify({"error": "Validated candidate report is unavailable"}), 404

    @app.post("/api/settings/save")
    def save_settings():
        try:
            body = position_payload({"settings", "revision"})
            return jsonify(save_editor(runtime, body["settings"], body["revision"]))
        except (DataError, ValueError, TypeError, OSError) as exc:
            return jsonify({"error": safe_error(exc)}), 400

    @app.post("/api/dashboard/restart")
    def restart_dashboard():
        try:
            position_payload(set())
            if restart_callback is None:
                return jsonify({"error": "Restart is unavailable with this launcher."}), 503
            with runtime.activity_gate.maintenance(), runtime.lock:
                position_payload(set())
                if runtime.stop.is_set():
                    raise DataError("A restart is already in progress.")
                check_restart(runtime)
                runtime.stop.set()
                runtime.wake.set()
                restart_callback()
            return jsonify({"message": "Restarting dashboard…", "generation": runtime.generation}), 202
        except (DataError, ValueError, TypeError, OSError) as exc:
            return jsonify({"error": safe_error(exc)}), 400

    @app.get("/api/dashboard/status")
    def dashboard_status():
        return jsonify({"generation": runtime.generation, "restarting": runtime.stop.is_set(),
                        "strategy_model": runtime.rules.strategy_model})

    def render_dashboard(template=None, **context):
        if session.get("generation") != runtime.generation:
            session["generation"] = runtime.generation
            session["csrf_token"] = secrets.token_hex(32)
        csrf = session.setdefault("csrf_token", secrets.token_hex(32))
        return render_template(template or ("neural_dashboard.html" if runtime.is_neural else "dashboard.html"), s=runtime.snapshot(), refresh=runtime.refresh,
                                      num=number, measure=measurement, time=utc, money=money,
                                      multiple=multiple, measurement_rows=measurement_rows, exact_measurement=exact_measurement,
                                      now=int(time.time()*1000), digits={n: c["price_decimals"] for n, c in runtime.assets.items()},
                                      assets=runtime.assets, generation=runtime.generation, csrf_token=csrf, position_request_id=uuid.uuid4().hex, **context)
    def position_payload(required, optional=()):
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        if not isinstance(payload, dict):
            raise DataError("Expected position fields")
        token = request.headers.get("X-CSRF-Token") or payload.get("csrf_token")
        if not isinstance(token, str) or not session.get("csrf_token") or not secrets.compare_digest(token, session["csrf_token"]):
            raise DataError("This form has expired; reload the dashboard and try again")
        if session.get("generation") != runtime.generation:
            raise DataError("Settings or the database changed. Reload before submitting this form.")
        if set(payload)-set(required)-set(optional)-{"csrf_token"} or not set(required).issubset(payload):
            raise DataError("Missing or unexpected position fields")
        return payload

    from .restore import register_restore_routes
    register_restore_routes(app, runtime, position_payload)
    from .deribit_settings import register_deribit_routes
    register_deribit_routes(app, runtime, position_payload)
    from .connection_settings import register_connection_routes
    register_connection_routes(app, runtime, position_payload)
    from .market_brief import register_market_brief_routes
    register_market_brief_routes(app, runtime, position_payload)

    def position_response(position, status=200):
        if request.is_json or request.accept_mimetypes.best == "application/json":
            return jsonify({"position": position}), status
        return redirect(url_for("trade_records_page")+"#manual-trades" if position.get("status") == "closed" else url_for("positions_page"), code=303)

    def administration_response(action, required):
        try:
            body = position_payload(required)
            if action is purge_database and body.get("confirm") != "purge":
                raise DataError("Confirm that all positions, buy watches and history should be cleared.")
            result = action(runtime, session["generation"])
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify(result)
            return redirect(url_for("settings_page")+"#settings-database", code=303)
        except (DataError, ValueError, TypeError) as exc:
            return jsonify({"error": safe_error(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "Could not complete the update: "+safe_error(exc)}), 500

    @app.post("/api/settings/apply")
    def apply_strategy_settings():
        return administration_response(apply_settings, set())

    @app.post("/api/settings/model")
    def select_strategy_model():
        try:
            body = position_payload({"strategy_model"})
            result = save_model_selection(runtime, body["strategy_model"])
            session["model_selection_message"] = result["message"]
            session["model_selection"] = body["strategy_model"]
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify(result)
            return redirect(url_for("settings_page")+"#model-settings", code=303)
        except (DataError, ValueError, TypeError, OSError) as exc:
            return jsonify({"error": safe_error(exc)}), 400

    @app.post("/api/database/purge")
    def purge_strategy_database():
        return administration_response(purge_database, {"confirm"})

    @app.post("/api/positions")
    def open_position():
        try:
            body = position_payload({"asset", "entry", "quantity", "request_id"},
                {"side", "position_type", "target_mode", "nn_target_mode", "momentum_alerts", "tp_alerts", "stop_price"})
            position, created = runtime.positions.open_position(runtime.assets, body["asset"], body.get("side"),
                body["entry"], body["quantity"], body["request_id"], int(time.time()*1000), body.get("target_mode", "smc"),
                body.get("position_type"), body.get("nn_target_mode", "gex_smc"),
                alert_boolean(body.get("momentum_alerts", True)), alert_boolean(body.get("tp_alerts", True)), None if body.get("stop_price") in (None, "") else body["stop_price"])
            runtime.wake.set()
            return position_response(position, 201 if created else 200)
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    def alert_boolean(value):
        if not request.is_json and value in ("true", "false"):
            value = value == "true"
        if type(value) is not bool:
            raise DataError("Choose On or Off for alerts")
        return value

    @app.post("/api/paper-positions/<record_id>/alerts")
    def update_paper_alerts(record_id):
        try:
            with runtime.activity_gate.maintenance():
                body = position_payload({"momentum_alerts", "tp_alerts"})
                if runtime.stop.is_set():
                    raise DataError("Dashboard is restarting; try again when it returns.")
                if not any(t["paper_key"] == record_id for t in paper_records(runtime)["active"]):
                    return jsonify({"error":"Active paper position not found"}), 404
                options = runtime.positions.set_paper_alerts(record_id, alert_boolean(body["momentum_alerts"]), alert_boolean(body["tp_alerts"]))
                preferences = runtime.positions.snapshot().get("paper_alert_preferences", {})
                runtime.store.transaction(lambda document: cancel_disabled_paper_alerts(document, preferences, runtime.store.paper_namespace))
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify({"preferences": options})
            return redirect(url_for("positions_page"), code=303)
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/positions/<position_id>/stop")
    def update_position_stop(position_id):
        try:
            body = position_payload({"stop_price"})
            position = runtime.positions.set_stop(position_id, None if body["stop_price"] in (None, "") else body["stop_price"], int(time.time()*1000))
            runtime.wake.set()
            return position_response(position)
        except KeyError:
            return jsonify({"error": "Position not found"}), 404
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/positions/<position_id>/alerts")
    def update_position_alerts(position_id):
        try:
            with runtime.activity_gate.maintenance():
                body = position_payload({"momentum_alerts", "tp_alerts"})
                if runtime.stop.is_set():
                    raise DataError("Dashboard is restarting; try again when it returns.")
                position = runtime.positions.set_alerts(position_id, alert_boolean(body["momentum_alerts"]),
                    alert_boolean(body["tp_alerts"]), int(time.time()*1000))
            return position_response(position)
        except KeyError:
            return jsonify({"error": "Position not found"}), 404
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/positions/<position_id>/close")
    def close_position(position_id):
        try:
            body = position_payload({"close"})
            position = runtime.positions.close_position(position_id, body["close"], int(time.time()*1000))
            return position_response(position)
        except KeyError:
            return jsonify({"error": "Position not found"}), 404
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/buy-watches")
    def open_buy_watch():
        try:
            body = position_payload({"asset", "reference_price", "buy_price", "quantity", "request_id"}, {"target_mode"})
            watch, created = runtime.positions.open_buy_watch(runtime.assets, body["asset"], body["reference_price"],
                body["buy_price"], body["quantity"], body["request_id"], int(time.time()*1000), body.get("target_mode", "smc"))
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify({"buy_watch": watch}), 201 if created else 200
            return redirect(url_for("positions_page")+"#buy-watches", code=303)
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/buy-watches/<watch_id>/cancel")
    def cancel_buy_watch(watch_id):
        try:
            position_payload(set())
            watch = runtime.positions.cancel_buy_watch(watch_id)
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify({"buy_watch": watch})
            return redirect(url_for("positions_page")+"#buy-watches", code=303)
        except KeyError:
            return jsonify({"error": "Buy watch not found"}), 404
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
    @app.get("/api/state")
    def api_state():
        return jsonify(runtime.snapshot())
    @app.get("/api/feeds")
    def api_feeds():
        from .feed_diagnostics import build_feed_status
        response = jsonify(build_feed_status(runtime))
        response.headers["Cache-Control"] = "no-store"
        return response
    @app.get("/api/chart/<name>")
    def api_chart(name):
        nonlocal charts, chart_generation
        with runtime.lock:
            if chart_generation != runtime.generation:
                charts = LiveCharts(runtime.assets, provider=chart_provider, fallback=chart_fallback, interval=H4)
                chart_generation = runtime.generation
            current_charts = charts
        try:
            chart = current_charts.get(name)
        except KeyError:
            return jsonify({"error": "Unknown asset"}), 404
        return jsonify(chart), 503 if chart.get("stale") else 200
    @app.get("/api/gex/<name>")
    def api_gex(name):
        try:
            profile = runtime.gex.get(name)
        except KeyError:
            return jsonify({"error": "Unknown asset"}), 404
        with runtime.lock:
            row = copy.deepcopy(runtime.market.get(name, {}))
        result = map_smc(profile, row, int(time.time()*1000))
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response, 503 if result.get("stale") else 200

    @app.get("/health")
    def health():
        snapshot = runtime.snapshot()
        fresh = snapshot["updated_ms"] is not None and int(time.time()*1000)-snapshot["updated_ms"] < max(60000, runtime.refresh*3000)
        good = fresh and not snapshot["error"] and len(snapshot["data"]) == len(runtime.assets)
        good = good and all(not r.get("errors") and not r.get("scan_error") for r in snapshot["data"].values())
        good = good and all(not r.get("neural", {}).get("error") for r in snapshot["data"].values())
        paused = snapshot["administration"]["paused"]
        return jsonify({"ok": bool(good and not paused), "paused": paused,
                        "last_scan_ms": snapshot["updated_ms"]}), 200 if good and not paused else 503
    return app
