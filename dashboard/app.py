#!/usr/bin/env python3
"""Baby Monitor Dashboard — Flask backend.

Thin dispatchers — every handler parses the request, calls into
`bilbo.api.<group>.<func>`, and `jsonify`-s the result (or `send_file`-s
a returned Path for binary responses). All business logic lives under
`src/bilbo/api/`; step 5 will reuse the same Python contract for the
control-api, and step 6 will replace this module with a reverse proxy.
"""

from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from bilbo.api import (
    air_quality as api_air_quality_mod,
    corrections as api_corrections,
    entries as api_entries,
    frames as api_frames,
    recap as api_recap,
    stats as api_stats,
    system as api_system,
    training as api_training,
)

app = Flask(__name__, static_folder="static")


def _respond(payload):
    """jsonify with optional status code extracted from `_status` key."""
    status = 200
    if isinstance(payload, dict) and "_status" in payload:
        status = payload["_status"]
        payload = {k: v for k, v in payload.items() if k != "_status"}
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# Static + PWA
# ---------------------------------------------------------------------------

# Cloudflare Access service token, loaded once at startup from
# /Users/shanit/.openclaw/workspace/.env.dashboard. Injected into the
# service worker body so the installed PWA can keep hitting /api/* once
# the user's Access SSO cookie has expired (which is the common case for
# background fetches and cold launches on iOS).
_ENV_DASHBOARD = Path("/Users/shanit/.openclaw/workspace/.env.dashboard")
_CF_ACCESS = {"id": "", "secret": ""}
if _ENV_DASHBOARD.exists():
    for _line in _ENV_DASHBOARD.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#"):
            continue
        _k, _, _v = _line.partition("=")
        _v = _v.strip().strip('"').strip("'")
        if _k.strip() == "CF_ACCESS_CLIENT_ID":
            _CF_ACCESS["id"] = _v
        elif _k.strip() == "CF_ACCESS_CLIENT_SECRET":
            _CF_ACCESS["secret"] = _v


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/sw.js")
def service_worker():
    # Service workers must be served from a path whose URL scope covers
    # the pages they should control. Serving from /sw.js (not /static/sw.js)
    # gives this worker control over the entire origin.
    sw_path = Path(app.static_folder) / "sw.js"
    body = sw_path.read_text()
    body = body.replace("__CF_ACCESS_CLIENT_ID__", _CF_ACCESS["id"])
    body = body.replace("__CF_ACCESS_CLIENT_SECRET__", _CF_ACCESS["secret"])
    response = app.response_class(body, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest")


# ---------------------------------------------------------------------------
# Status / timeline / sleep stats
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    return _respond(api_stats.status())


@app.route("/api/timeline")
def api_timeline():
    date_str = request.args.get("date")
    hours = int(request.args.get("hours", 24))
    return _respond(api_entries.timeline(date=date_str, hours=hours))


@app.route("/api/sleep-stats")
def api_sleep_stats():
    days = int(request.args.get("days", 7))
    return _respond(api_stats.sleep_stats(days=days))


@app.route("/api/bassinet-daily")
def api_bassinet_daily():
    days = int(request.args.get("days", 7))
    return _respond(api_stats.bassinet_daily(days=days))


@app.route("/api/sleep-trend")
def api_sleep_trend():
    days = int(request.args.get("days", 14))
    return _respond(api_stats.sleep_trend(days=days))


@app.route("/api/feeds")
def api_feeds():
    days = int(request.args.get("days", 1))
    return _respond(api_stats.feeds(days=days))


@app.route("/api/diapers")
def api_diapers():
    days = int(request.args.get("days", 1))
    return _respond(api_stats.diapers(days=days))


@app.route("/api/events")
def api_events():
    hours_arg = request.args.get("hours", "72")
    try:
        hours_val = float(hours_arg)
    except ValueError:
        hours_val = 72.0
    count = int(request.args.get("count", 20))
    type_filter = request.args.get("type", "all")
    return _respond(api_stats.events(hours=hours_val, count=count, type_filter=type_filter))


# ---------------------------------------------------------------------------
# Entry edits + review
# ---------------------------------------------------------------------------

@app.route("/api/update-entry", methods=["POST"])
def api_update_entry():
    data = request.get_json() or {}
    face_bbox = data["faceBbox"] if "faceBbox" in data else ...
    return _respond(api_entries.update_entry(
        timestamp=data.get("timestamp"),
        state=data.get("state"),
        position=data.get("position"),
        eye_state=data.get("eyeState"),
        face_bbox=face_bbox,
    ))


@app.route("/api/mark-reviewed", methods=["POST"])
def api_mark_reviewed():
    data = request.get_json() or {}
    return _respond(api_entries.mark_reviewed(timestamps=data.get("timestamps", [])))


@app.route("/api/run-inference", methods=["POST"])
def api_run_inference():
    data = request.get_json() or {}
    return _respond(api_entries.run_inference(timestamp=data.get("timestamp")))


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@app.route("/api/training-status")
def api_training_status():
    return _respond(api_training.training_status())


@app.route("/api/retrain", methods=["POST"])
def api_retrain():
    data = request.get_json(silent=True) or {}
    return _respond(api_training.retrain(
        trigger=data.get("trigger", "dashboard"),
        skip_face_detect=data.get("skipFaceDetect", False),
    ))


@app.route("/api/retrain/abort", methods=["POST"])
def api_retrain_abort():
    return _respond(api_training.retrain_abort())


# ---------------------------------------------------------------------------
# Corrections inbox
# ---------------------------------------------------------------------------

@app.route("/api/pending-corrections")
def api_pending_corrections():
    return _respond(api_corrections.pending_corrections())


@app.route("/api/correction/resolve", methods=["POST"])
def api_correction_resolve():
    data = request.get_json(silent=True) or {}
    return _respond(api_corrections.correction_resolve(
        correction_id=data.get("id"),
        eye_state=data.get("eyeState"),
    ))


@app.route("/api/correction/discard", methods=["POST"])
def api_correction_discard():
    data = request.get_json(silent=True) or {}
    return _respond(api_corrections.correction_discard(correction_id=data.get("id")))


# ---------------------------------------------------------------------------
# System + pipeline health
# ---------------------------------------------------------------------------

@app.route("/api/system-usage")
def api_system_usage():
    return jsonify(api_system.gather())


@app.route("/api/pipeline-health")
def api_pipeline_health():
    return _respond(api_stats.pipeline_health())


@app.route("/api/classification-rate")
def api_classification_rate():
    try:
        hours = int(request.args.get("hours", 24))
        bucket_min = int(request.args.get("bucketMin", 60))
    except ValueError:
        return jsonify({"error": "hours and bucketMin must be integers"}), 400
    return _respond(api_stats.classification_rate(hours=hours, bucket_min=bucket_min))


# ---------------------------------------------------------------------------
# Model performance + safety
# ---------------------------------------------------------------------------

@app.route("/api/safety-stats")
def api_safety_stats():
    hours = float(request.args.get("hours", 168))
    return _respond(api_stats.safety_stats(hours=hours))


@app.route("/api/monitor-stats")
def api_monitor_stats():
    hours = float(request.args.get("hours", 24))
    return _respond(api_stats.monitor_stats(hours=hours))


@app.route("/api/eye-state-daily-metrics")
def api_eye_state_daily_metrics():
    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    return _respond(api_stats.eye_state_daily_metrics(days=days))


@app.route("/api/pipeline-history")
def api_pipeline_history():
    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    return _respond(api_stats.pipeline_history(days=days))


# ---------------------------------------------------------------------------
# Air quality
# ---------------------------------------------------------------------------

@app.route("/api/air-quality")
def api_air_quality():
    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    return _respond(api_air_quality_mod.api_air_quality(hours=hours))


# ---------------------------------------------------------------------------
# Frames + recap
# ---------------------------------------------------------------------------

@app.route("/api/frame")
def api_frame():
    frame_path = request.args.get("path", "")
    if not frame_path:
        abort(400)
    try:
        path = api_frames.get_frame_path(frame_path)
    except api_frames.FrameForbidden:
        abort(403)
    except api_frames.FrameNotFound:
        abort(404)
    return send_file(str(path), mimetype="image/jpeg")


@app.route("/api/recap/generate", methods=["POST"])
def api_recap_generate():
    body = request.get_json(silent=True) or {}
    try:
        fps = int(body.get("fps", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "fps must be an integer"}), 400
    date_str = str(body.get("date", "")).strip()
    force = bool(body.get("force"))
    return _respond(api_recap.recap_generate(date=date_str, fps=fps, force=force))


@app.route("/api/recap/video")
def api_recap_video():
    name = request.args.get("name", "")
    path = api_recap.recap_video(name=name)
    if path is None:
        abort(404)
    # send_file handles Range requests, which the <video> element uses for seeking.
    return send_file(str(path), mimetype="video/mp4", conditional=True)


if __name__ == "__main__":
    # debug=False on purpose. The werkzeug debugger crashes on Python 3.14
    # (sysconfig.get_paths() raises AttributeError: 'installed_base'), turning
    # any uncaught exception into an opaque HTTP 500. The dev-server reloader
    # also doubles file-descriptor usage, contributing to EMFILE crashes after
    # long uptimes (we hit one on 2026-04-09). Production stays on the dev
    # server for simplicity but without the debugger and reloader.
    app.run(host="0.0.0.0", port=5555, debug=False)
