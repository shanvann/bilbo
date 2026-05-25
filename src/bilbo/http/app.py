"""Control-API — REST surface over bilbo.api.*

A standalone Flask app that runs in its own container (port 5556) and
exposes the dashboard's data endpoints under `/api/v1/*`. The dashboard
container reverse-proxies `/api/*` to here; other consumers (CLI, future
mobile app, automation) can hit it directly.

Every route is a thin dispatcher: parse the request, call into
`bilbo.api.<group>.<func>`, jsonify the result (or send_file a returned
Path for binary responses). All business logic lives under `bilbo.api`.
"""
from __future__ import annotations

from flask import Flask, abort, jsonify, request, send_file

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

app = Flask(__name__)


def _respond(payload):
    """jsonify with optional status code extracted from `_status` key."""
    status = 200
    if isinstance(payload, dict) and "_status" in payload:
        status = payload["_status"]
        payload = {k: v for k, v in payload.items() if k != "_status"}
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Status / timeline / sleep stats
# ---------------------------------------------------------------------------

@app.get("/api/v1/status")
def api_status():
    return _respond(api_stats.status())


@app.get("/api/v1/timeline")
def api_timeline():
    date_str = request.args.get("date")
    hours = int(request.args.get("hours", 24))
    return _respond(api_entries.timeline(date=date_str, hours=hours))


@app.get("/api/v1/sleep-stats")
def api_sleep_stats():
    days = int(request.args.get("days", 7))
    return _respond(api_stats.sleep_stats(days=days))


@app.get("/api/v1/bassinet-daily")
def api_bassinet_daily():
    days = int(request.args.get("days", 7))
    return _respond(api_stats.bassinet_daily(days=days))


@app.get("/api/v1/sleep-trend")
def api_sleep_trend():
    days = int(request.args.get("days", 14))
    return _respond(api_stats.sleep_trend(days=days))


@app.get("/api/v1/feeds")
def api_feeds():
    days = int(request.args.get("days", 1))
    return _respond(api_stats.feeds(days=days))


@app.get("/api/v1/diapers")
def api_diapers():
    days = int(request.args.get("days", 1))
    return _respond(api_stats.diapers(days=days))


@app.get("/api/v1/events")
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

@app.post("/api/v1/update-entry")
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


@app.post("/api/v1/mark-reviewed")
def api_mark_reviewed():
    data = request.get_json() or {}
    return _respond(api_entries.mark_reviewed(timestamps=data.get("timestamps", [])))


@app.post("/api/v1/run-inference")
def api_run_inference():
    data = request.get_json() or {}
    return _respond(api_entries.run_inference(timestamp=data.get("timestamp")))


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@app.get("/api/v1/training-status")
def api_training_status():
    return _respond(api_training.training_status())


@app.post("/api/v1/retrain")
def api_retrain():
    data = request.get_json(silent=True) or {}
    return _respond(api_training.retrain(
        trigger=data.get("trigger", "dashboard"),
        skip_face_detect=data.get("skipFaceDetect", False),
    ))


@app.post("/api/v1/retrain/abort")
def api_retrain_abort():
    return _respond(api_training.retrain_abort())


# ---------------------------------------------------------------------------
# Corrections inbox
# ---------------------------------------------------------------------------

@app.get("/api/v1/pending-corrections")
def api_pending_corrections():
    return _respond(api_corrections.pending_corrections())


@app.post("/api/v1/correction/resolve")
def api_correction_resolve():
    data = request.get_json(silent=True) or {}
    return _respond(api_corrections.correction_resolve(
        correction_id=data.get("id"),
        eye_state=data.get("eyeState"),
    ))


@app.post("/api/v1/correction/discard")
def api_correction_discard():
    data = request.get_json(silent=True) or {}
    return _respond(api_corrections.correction_discard(correction_id=data.get("id")))


# ---------------------------------------------------------------------------
# System + pipeline health
# ---------------------------------------------------------------------------

@app.get("/api/v1/system-usage")
def api_system_usage():
    return jsonify(api_system.gather())


@app.get("/api/v1/pipeline-health")
def api_pipeline_health():
    return _respond(api_stats.pipeline_health())


@app.get("/api/v1/classification-rate")
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

@app.get("/api/v1/safety-stats")
def api_safety_stats():
    hours = float(request.args.get("hours", 168))
    return _respond(api_stats.safety_stats(hours=hours))


@app.get("/api/v1/monitor-stats")
def api_monitor_stats():
    hours = float(request.args.get("hours", 24))
    return _respond(api_stats.monitor_stats(hours=hours))


@app.get("/api/v1/eye-state-daily-metrics")
def api_eye_state_daily_metrics():
    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    return _respond(api_stats.eye_state_daily_metrics(days=days))


@app.get("/api/v1/pipeline-history")
def api_pipeline_history():
    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    return _respond(api_stats.pipeline_history(days=days))


# ---------------------------------------------------------------------------
# Air quality
# ---------------------------------------------------------------------------

@app.get("/api/v1/air-quality")
def api_air_quality():
    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    return _respond(api_air_quality_mod.api_air_quality(hours=hours))


# ---------------------------------------------------------------------------
# Frames + recap
# ---------------------------------------------------------------------------

@app.get("/api/v1/frame")
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


@app.post("/api/v1/recap/generate")
def api_recap_generate():
    body = request.get_json(silent=True) or {}
    try:
        fps = int(body.get("fps", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "fps must be an integer"}), 400
    date_str = str(body.get("date", "")).strip()
    force = bool(body.get("force"))
    return _respond(api_recap.recap_generate(date=date_str, fps=fps, force=force))


@app.get("/api/v1/recap/video")
def api_recap_video():
    name = request.args.get("name", "")
    path = api_recap.recap_video(name=name)
    if path is None:
        abort(404)
    # send_file handles Range requests, which the <video> element uses for seeking.
    return send_file(str(path), mimetype="video/mp4", conditional=True)


# ---------------------------------------------------------------------------
# Entry point (console script: bilbo-control-api)
# ---------------------------------------------------------------------------

def main():
    """Local-dev entry point. Production runs under gunicorn (see compose)."""
    app.run(host="0.0.0.0", port=5556, debug=False)


if __name__ == "__main__":
    main()
