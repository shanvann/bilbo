#!/usr/bin/env python3
"""Baby Monitor Dashboard — Flask backend."""

import csv
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

# Add scripts/ to path so we can import lib.db
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from lib.db import get_db, get_entries, get_connection

app = Flask(__name__, static_folder="static")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SLEEP_LOG = DATA_DIR / "sleep-log.jsonl"
ACTIVITY_CSV = DATA_DIR / "activity-log.csv"
CORRECTIONS_LOG = DATA_DIR / "corrections.jsonl"
VIDEOS_DIR = DATA_DIR / "videos"
ET = timezone(timedelta(hours=-4))  # America/New_York (EDT)

# AirGradient logger DB. The logger lives as a sibling skill at
# skills/airgradient-logger/ (merged 2026-04-28); the dashboard reads its
# DB read-only via URI, so the writer (a launchd agent) and the dashboard
# never contend on a journal lock. The default resolves relative to this
# file: skills/baby-monitor/dashboard/ → ../../airgradient-logger/data/.
AIRGRADIENT_DB_PATH = os.environ.get(
    "AIRGRADIENT_DB_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "airgradient-logger" / "data" / "airgradient.db"),
)
AIR_QUALITY_MAX_POINTS = 360


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_csv_rows():
    rows = []
    with open(ACTIVITY_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def parse_ts(ts_str):
    """Parse an ISO timestamp string to a tz-aware UTC datetime."""
    if not ts_str:
        return None
    ts_str = ts_str.strip()
    # Handle Z suffix
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def parse_csv_dt(dt_str):
    """Parse CSV datetime like '2026-03-31 11:28' as ET-local, return UTC."""
    if not dt_str or not dt_str.strip():
        return None
    try:
        naive = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=ET).astimezone(timezone.utc)
    except ValueError:
        return None


def humanize_duration(td):
    total_sec = int(td.total_seconds())
    if total_sec < 0:
        return "0m"
    hours, rem = divmod(total_sec, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def api_status():
    db = get_db()
    last = db.get_last_entry()
    if not last:
        return jsonify({"error": "no data"}), 404

    ts = parse_ts(last.get("timestamp"))
    now = datetime.now(timezone.utc)

    # Use SQL to find the oldest entry in the current contiguous (babyPresent,
    # state) run — a 50-entry walk-back capped duration at ~50m once the baby
    # had been out of bassinet (or in any single state) for longer.
    current_present = bool(last.get("babyPresent"))
    current_state = last.get("state")
    run_start_ts = db.find_current_run_start(current_present, current_state)
    state_start = parse_ts(run_start_ts) or ts

    duration = now - state_start if state_start else timedelta(0)

    # Determine display status
    if not current_present:
        display = "Out of bassinet"
        icon = "absent"
    elif current_state == "Asleep":
        display = "Asleep"
        icon = "asleep"
    elif current_state == "Awake":
        display = "Awake"
        icon = "awake"
    else:
        display = "In bassinet"
        icon = "unknown"

    return jsonify({
        "display": display,
        "icon": icon,
        "duration": humanize_duration(duration),
        "durationSeconds": int(duration.total_seconds()),
        "timestamp": last.get("timestamp"),
        "frame": last.get("frame"),
        "position": last.get("sleepPosition"),
        "alerts": last.get("alerts", []),
        "captureMode": last.get("captureMode"),
        "secondsSinceCapture": int((now - ts).total_seconds()) if ts else None,
    })


@app.route("/api/timeline")
def api_timeline():
    date_str = request.args.get("date")  # YYYY-MM-DD in ET
    hours = int(request.args.get("hours", 24))
    db = get_db()

    if date_str:
        # `date` means the overnight window that began on this ET date:
        # 4 PM ET on `date` → 11 AM ET on `date + 1` (19 h). The Timeline
        # is the night-of-sleep view; daytime out-of-bassinet stretches
        # are excluded by design.
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        night_start = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=16, tzinfo=et
        )
        night_end = night_start + timedelta(hours=19)
        cutoff = night_start.astimezone(timezone.utc)
        end_cutoff = night_end.astimezone(timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        end_cutoff = datetime.now(timezone.utc)

    raw_entries = db.get_entries(
        start=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=end_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    timeline = []
    for e in raw_entries:
        shadow = e.get("shadow") or {}
        timeline.append({
            "timestamp": e["timestamp"],
            "babyPresent": e.get("babyPresent"),
            "state": e.get("state"),
            "eyeState": e.get("eyeState"),
            "eyeStateEdited": e.get("eyeStateEdited", False),
            "eyeStateCorrectedAt": e.get("eyeStateCorrectedAt"),
            "detectionMethod": e.get("detectionMethod"),
            "shadowModelVersion": e.get("shadowModelVersion"),
            "shadowBirdeyeState": shadow.get("birdeyeState"),
            "shadowEyeState": shadow.get("eyeState"),
            "shadowPresenceConfidence": shadow.get("presenceConfidence"),
            "shadowEyeConfidence": shadow.get("eyeConfidence"),
            "shadowFallback": shadow.get("fallback"),
            "headPosition": e.get("headPosition"),
            "faceBbox": e.get("faceBbox"),
            "faceConfidence": e.get("faceConfidence"),
            "faceBboxCorrected": e.get("faceBboxCorrected"),
            "retrainAgreed": e.get("retrainAgreed"),
            "reviewed": e.get("reviewed", False),
            "frame": e.get("frame"),
            "alerts": e.get("alerts", []),
            # Shadow-experiment results (dict keyed by experiment name).
            # Projected through so the Block Detail viewer can render each
            # registered shadow's eye-state prediction next to the prod
            # BIRDEYE labels. Absent on frames where no experiment ran.
            "experiments": e.get("experiments"),
        })

    # Also include feed events from CSV
    csv_rows = load_csv_rows()
    feeds = []
    for row in csv_rows:
        if row["Type"] == "Feed":
            dt = parse_csv_dt(row["Start"])
            if dt and dt >= cutoff:
                feeds.append({
                    "timestamp": dt.isoformat(),
                    "type": "Feed",
                    "condition": row.get("Start Condition", ""),
                    "location": row.get("Start Location", ""),
                    "notes": row.get("Notes", ""),
                })

    return jsonify({"entries": timeline, "feeds": feeds})


@app.route("/api/sleep-stats")
def api_sleep_stats():
    days = int(request.args.get("days", 7))
    # Windowed SQLite query instead of slurping the entire JSONL. The old
    # load_jsonl() parsed all ~8k rows on every request and then discarded
    # anything outside the cutoff; this is the same data via an indexed
    # timestamp range.
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    entries = get_db().get_entries(hours=days * 24)

    # Build sleep segments: consecutive entries where babyPresent and state is
    # Asleep or Unknown (Unknown between Asleep entries counts as sleep)
    segments = []
    seg_start = None
    seg_end = None

    for e in entries:
        ts = parse_ts(e.get("timestamp"))
        if not ts or ts < cutoff:
            continue
        state = e.get("state")
        present = e.get("babyPresent")
        # Count as sleeping if: present AND (Asleep OR Unknown)
        # Unknown is included because vision model often can't tell — baby is still in bassinet sleeping
        is_in_bassinet_resting = present and state in ("Asleep", "Unknown")
        if is_in_bassinet_resting:
            if seg_start is None:
                seg_start = ts
            seg_end = ts
        else:
            if seg_start is not None:
                segments.append((seg_start, seg_end))
                seg_start = None
                seg_end = None
    if seg_start is not None:
        segments.append((seg_start, seg_end))

    # Build bassinet segments: consecutive entries where babyPresent (any state)
    bassinet_segments = []
    bseg_start = None
    bseg_end = None
    for e in entries:
        ts = parse_ts(e.get("timestamp"))
        if not ts or ts < cutoff:
            continue
        if e.get("babyPresent"):
            if bseg_start is None:
                bseg_start = ts
            bseg_end = ts
        else:
            if bseg_start is not None:
                bassinet_segments.append((bseg_start, bseg_end))
                bseg_start = None
                bseg_end = None
    if bseg_start is not None:
        bassinet_segments.append((bseg_start, bseg_end))

    # CSV Sleep rows as fallback for days with sparse JSONL data (<100 entries)
    from collections import Counter
    jsonl_day_counts = Counter()
    for e in entries:
        ts = parse_ts(e.get("timestamp"))
        if ts and ts >= cutoff:
            jsonl_day_counts[ts.astimezone(ET).date().isoformat()] += 1

    JSONL_MIN_ENTRIES = 100  # ~7 hours of coverage at 4-min intervals

    csv_rows = load_csv_rows()
    for row in csv_rows:
        if row["Type"] == "Sleep":
            start = parse_csv_dt(row["Start"])
            end = parse_csv_dt(row["End"])
            if start and end and start >= cutoff:
                csv_date = start.astimezone(ET).date().isoformat()
                if jsonl_day_counts.get(csv_date, 0) < JSONL_MIN_ENTRIES:
                    segments.append((start, end))

    # Group by ET date
    daily = {}
    for start, end in segments:
        et_date = start.astimezone(ET).date().isoformat()
        dur = (end - start).total_seconds()
        if dur <= 0:
            continue
        if et_date not in daily:
            daily[et_date] = {"total": 0, "longestSleep": 0, "longestBassinet": 0, "stretches": 0}
        daily[et_date]["total"] += dur
        daily[et_date]["longestSleep"] = max(daily[et_date]["longestSleep"], dur)
        daily[et_date]["stretches"] += 1

    # Add longest bassinet stretches per day
    for start, end in bassinet_segments:
        et_date = start.astimezone(ET).date().isoformat()
        dur = (end - start).total_seconds()
        if dur <= 0:
            continue
        if et_date not in daily:
            daily[et_date] = {"total": 0, "longestSleep": 0, "longestBassinet": 0, "stretches": 0}
        daily[et_date]["longestBassinet"] = max(daily[et_date]["longestBassinet"], dur)

    result = []
    for date_str in sorted(daily.keys()):
        d = daily[date_str]
        result.append({
            "date": date_str,
            "totalHours": round(d["total"] / 3600, 1),
            "longestSleepHours": round(d["longestSleep"] / 3600, 1),
            "longestBassinetHours": round(d["longestBassinet"] / 3600, 1),
            "stretches": d["stretches"],
        })

    return jsonify({"days": result})


@app.route("/api/bassinet-daily")
def api_bassinet_daily():
    """Daily in-bassinet vs out-of-bassinet hours for the last N days."""
    days = int(request.args.get("days", 7))
    db = get_db()

    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    raw = db.get_entries(
        start=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    # Accumulate per-day durations, split in-bassinet time by state.
    # `in` is the legacy total (asleep + awake + falling_asleep + unknown_in)
    # and is kept for any consumer that still wants a single in-vs-out
    # breakdown.
    daily = {}
    for i in range(len(raw) - 1):
        e = raw[i]
        next_e = raw[i + 1]
        ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        next_ts = datetime.fromisoformat(next_e["timestamp"].replace("Z", "+00:00"))
        dur = (next_ts - ts).total_seconds()
        if dur <= 0 or dur > 3600:  # skip gaps > 1h
            continue

        date_str = ts.astimezone(et).strftime("%Y-%m-%d")
        if date_str not in daily:
            daily[date_str] = {
                "asleep": 0, "awake": 0, "falling_asleep": 0,
                "unknown_in": 0, "out": 0,
            }

        if not e.get("babyPresent"):
            daily[date_str]["out"] += dur
            continue

        state = e.get("state")
        if state == "Asleep":
            daily[date_str]["asleep"] += dur
        elif state == "Awake":
            daily[date_str]["awake"] += dur
        elif state == "FallingAsleep":
            daily[date_str]["falling_asleep"] += dur
        else:
            daily[date_str]["unknown_in"] += dur

    result = []
    for date_str in sorted(daily.keys()):
        d = daily[date_str]
        in_total = d["asleep"] + d["awake"] + d["falling_asleep"] + d["unknown_in"]
        total = in_total + d["out"]
        result.append({
            "date": date_str,
            "asleepHours": round(d["asleep"] / 3600, 1),
            "awakeHours": round(d["awake"] / 3600, 1),
            "fallingAsleepHours": round(d["falling_asleep"] / 3600, 1),
            "unknownInHours": round(d["unknown_in"] / 3600, 1),
            "inHours": round(in_total / 3600, 1),
            "outHours": round(d["out"] / 3600, 1),
            "inPct": round(in_total / total * 100) if total > 0 else 0,
        })

    return jsonify({"days": result})


@app.route("/api/feeds")
def api_feeds():
    days = int(request.args.get("days", 1))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = load_csv_rows()
    feeds = []
    for row in rows:
        if row["Type"] != "Feed":
            continue
        dt = parse_csv_dt(row["Start"])
        if dt and dt >= cutoff:
            feeds.append({
                "start": row["Start"],
                "end": row.get("End", ""),
                "duration": row.get("Duration", ""),
                "condition": row.get("Start Condition", ""),
                "location": row.get("Start Location", ""),
                "endCondition": row.get("End Condition", ""),
                "notes": row.get("Notes", ""),
            })
    return jsonify({"feeds": feeds, "count": len(feeds)})


@app.route("/api/diapers")
def api_diapers():
    days = int(request.args.get("days", 1))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = load_csv_rows()
    diapers = []
    for row in rows:
        if row["Type"] != "Diaper":
            continue
        dt = parse_csv_dt(row["Start"])
        if dt and dt >= cutoff:
            diapers.append({
                "start": row["Start"],
                "color": row.get("Duration", ""),
                "consistency": row.get("Start Condition", ""),
                "contents": row.get("End Condition", ""),
            })
    return jsonify({"diapers": diapers, "count": len(diapers)})


@app.route("/api/events")
def api_events():
    """Recent state transitions.

    Query params:
      - hours: lookback window in hours. 0 (or omitted) means "all time".
        Defaults to 72 to preserve prior behavior for callers that don't
        pass the param.
      - count: max rows to return after filtering (most recent first).
      - type: one of all|placed|removed|fell_asleep|woke|other.
    """
    db = get_db()
    hours_arg = request.args.get("hours", "72")
    try:
        hours_val = float(hours_arg)
    except ValueError:
        hours_val = 72.0
    entries = db.get_entries(hours=hours_val if hours_val > 0 else None)
    events = []
    prev = None

    def effective_state(e):
        """Normalize state: when baby is absent, always 'not_present'."""
        if not e.get("babyPresent"):
            return "not_present"
        return e.get("state", "Unknown")

    for e in entries:
        if prev is None:
            prev = e
            continue

        prev_state = effective_state(prev)
        curr_state = effective_state(e)

        if prev_state == curr_state:
            prev = e
            continue

        # Determine event type
        if prev_state == "not_present" and curr_state != "not_present":
            event_type = "Placed in bassinet"
        elif prev_state != "not_present" and curr_state == "not_present":
            event_type = "Removed from bassinet"
        elif curr_state == "Asleep" and prev_state != "Asleep":
            event_type = "Fell asleep"
        elif curr_state == "Awake" and prev_state == "Asleep":
            event_type = "Woke up"
        else:
            event_type = f"{prev_state} → {curr_state}"

        events.append({
            "timestamp": e["timestamp"],
            "type": event_type,
        })
        prev = e

    # Add durations between consecutive events. Done on the full unfiltered
    # list so each event's duration means "time until the next chronological
    # event" regardless of whether those neighbors survive the filter.
    for i in range(len(events) - 1):
        ts1 = parse_ts(events[i]["timestamp"])
        ts2 = parse_ts(events[i + 1]["timestamp"])
        if ts1 and ts2:
            events[i]["duration"] = humanize_duration(ts2 - ts1)

    # Optional type filter. The named categories match the same string
    # predicates the frontend badge logic uses, plus an "other" bucket for
    # the `{prev_state} -> {curr_state}` catch-all.
    type_filter = request.args.get("type", "all")
    if type_filter != "all":
        def _matches(t: str) -> bool:
            if type_filter == "placed":
                return "Placed" in t
            if type_filter == "removed":
                return "Removed" in t
            if type_filter == "fell_asleep":
                return "Fell asleep" in t
            if type_filter == "woke":
                return "Woke" in t
            if type_filter == "other":
                # Anything that didn't hit one of the named cases — the
                # generic "A → B" events.
                return not any(s in t for s in
                               ("Placed", "Removed", "Fell asleep", "Woke"))
            return True
        events = [e for e in events if _matches(e["type"])]

    # Return N most recent, most recent first
    count = int(request.args.get("count", 20))
    events.reverse()
    return jsonify({"events": events[:count]})


@app.route("/api/update-entry", methods=["POST"])
def api_update_entry():
    """Update state and/or position for a JSONL entry by timestamp.

    Also logs the correction to corrections.jsonl for retraining.
    """
    data = request.get_json()
    ts = data.get("timestamp")
    new_state = data.get("state")
    new_position = data.get("position")
    new_eye_state = data.get("eyeState")
    new_face_bbox = data.get("faceBbox")  # {x1, y1, x2, y2} normalized or null to clear

    if not ts:
        return jsonify({"error": "timestamp required"}), 400

    db = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build update dict
    updates = {}
    if new_state:
        updates["state"] = new_state
        updates["stateEdited"] = True
    if new_position:
        updates["sleepPosition"] = new_position
        updates["positionEdited"] = True
    if new_eye_state:
        updates["eyeState"] = new_eye_state
        updates["eyeStateEdited"] = True
        updates["eyeStateCorrectedAt"] = now
    if new_face_bbox is not None:
        # null clears the correction, dict sets it
        updates["faceBboxCorrected"] = new_face_bbox if new_face_bbox else None

    # Update in SQLite
    if not db.update_entry(ts, updates):
        return jsonify({"error": "entry not found"}), 404

    # Also update JSONL backup
    lines = SLEEP_LOG.read_text().strip().splitlines()
    original_entry = None
    new_lines = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("timestamp") == ts:
            original_entry = json.loads(line)
            entry.update(updates)
        new_lines.append(json.dumps(entry))
    SLEEP_LOG.write_text("\n".join(new_lines) + "\n")

    # Log correction to both SQLite and JSONL backup — only when a *label*
    # field actually changed. Bbox-only edits (faceBbox) are not label
    # corrections: they feed the face detector via entries.faceBboxCorrected,
    # not the corrections table. Historically we wrote phantom rows with
    # null corrected_state / corrected_eye_state here, which polluted the
    # pending-corrections view with "?" entries.
    label_changed = bool(new_state or new_eye_state or new_position)
    if original_entry and label_changed:
        correction = {
            "correctedAt": now,
            "originalTimestamp": ts,
            "frame": original_entry.get("frame"),
            "originalState": original_entry.get("state"),
            "correctedState": new_state,
            "originalEyeState": original_entry.get("eyeState"),
            "correctedEyeState": new_eye_state,
            "originalPosition": original_entry.get("sleepPosition"),
            "correctedPosition": new_position,
            "detectionMethod": original_entry.get("detectionMethod"),
            "source": "dashboard",
        }
        db.insert_correction(correction)
        with open(CORRECTIONS_LOG, "a") as f:
            f.write(json.dumps(correction) + "\n")

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Training manager — PID-based, works across CLI/dashboard/cron
# ---------------------------------------------------------------------------
from lib.training_state import is_running as _train_is_running, get_status as _train_get_status, \
    mark_subprocess_started as _train_mark_started, abort as _train_abort

MODELS_DIR = DATA_DIR.parent / "pipeline" / "models"
TRAINING_LOG = MODELS_DIR / "training-log.jsonl"


def _get_last_training_logs(n: int = 2) -> list[dict]:
    if not TRAINING_LOG.exists():
        return []
    lines = TRAINING_LOG.read_text().strip().splitlines()
    if not lines:
        return []
    result = []
    for line in reversed(lines[-n:]):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return result


@app.route("/api/training-status")
def api_training_status():
    """Return current training run status + last completed training details."""
    db = get_db()
    state = _train_get_status()  # PID-checked, auto-cleans stale

    logs = db.get_last_training_runs(2)
    if not logs:
        logs = _get_last_training_logs(2)
    last_log = logs[0] if logs else None
    prev_log = logs[1] if len(logs) > 1 else None

    last_trained_ts = last_log.get("timestamp") if last_log else None
    total_corrections, pending_corrections = db.get_pending_corrections_count(last_trained_ts)
    duration_stats = db.get_training_duration_stats()
    last_trained_per_classifier = db.get_last_trained_per_classifier()

    return jsonify({
        "running": state.get("status") == "running",
        "runStatus": state.get("status", "idle"),
        "pid": state.get("pid"),
        "trigger": state.get("trigger"),
        "startedAt": state.get("startedAt"),
        "finishedAt": state.get("finishedAt"),
        "exitCode": state.get("exitCode"),
        "lastTrained": last_log.get("timestamp") if last_log else None,
        "version": last_log.get("version") if last_log else None,
        "lastMetrics": last_log.get("metrics") if last_log else None,
        "lastLabelSources": last_log.get("label_sources") if last_log else None,
        "lastEntriesTotal": last_log.get("entries_total") if last_log else None,
        "lastDurationSeconds": last_log.get("duration_seconds") if last_log else None,
        "prevVersion": prev_log.get("version") if prev_log else None,
        "prevMetrics": prev_log.get("metrics") if prev_log else None,
        "pendingCorrections": pending_corrections,
        "totalCorrections": total_corrections,
        "trainingDurationStats": duration_stats,
        "lastTrainedPerClassifier": last_trained_per_classifier,
    })


@app.route("/api/retrain", methods=["POST"])
def api_retrain():
    """Start model retraining in the background.

    Body (optional): {"trigger": "dashboard" | "manual" | "scheduled"}
    """
    if _train_is_running():
        return jsonify({"ok": False, "error": "Training already in progress"}), 409

    data = request.get_json(silent=True) or {}
    trigger = data.get("trigger", "dashboard")
    skip_face = data.get("skipFaceDetect", False)

    monitor_py = str(DATA_DIR.parent / "scripts" / "monitor.py")
    python = str(DATA_DIR.parent / "venv" / "bin" / "python3")

    # Spawn subprocess — redirect to log files to avoid pipe buffer deadlock.
    # Face detection dataset init produces substantial output (~500 frames)
    # which would fill the 64KB pipe buffer and block the subprocess.
    retrain_stdout = open(DATA_DIR / "retrain-dashboard-stdout.log", "w")
    retrain_stderr = open(DATA_DIR / "retrain-dashboard-stderr.log", "w")
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    cmd = [python, "-u", monitor_py, "--retrain"]
    if skip_face:
        cmd.append("--skip-face-detect")
    proc = subprocess.Popen(
        cmd,
        cwd=str(DATA_DIR.parent),
        stdout=retrain_stdout,
        stderr=retrain_stderr,
        env=env,
        start_new_session=True,  # survive dashboard restarts
    )
    _train_mark_started(proc.pid, trigger)

    # Background thread to reap the child and update state when done
    def _reap():
        proc.wait()
        retrain_stdout.close()
        retrain_stderr.close()
        from lib.training_state import mark_completed
        mark_completed(proc.returncode)

    threading.Thread(target=_reap, daemon=True).start()

    return jsonify({"ok": True, "pid": proc.pid, "trigger": trigger})


@app.route("/api/retrain/abort", methods=["POST"])
def api_retrain_abort():
    """Abort a running training process (by PID)."""
    if not _train_is_running():
        return jsonify({"ok": False, "error": "No training in progress"}), 404

    killed = _train_abort()
    return jsonify({"ok": killed, "status": "aborted" if killed else "not found"})


@app.route("/api/pending-corrections")
def api_pending_corrections():
    """Return pending corrections not yet used in training."""
    db = get_db()
    logs = db.get_last_training_runs(1)
    last_trained_ts = logs[0].get("timestamp") if logs else None
    corrections = db.get_pending_corrections(last_trained_ts)

    # Summary breakdown
    eye_changes = {}
    for c in corrections:
        orig = c.get("originalEyeState") or "unknown"
        corr = c.get("correctedEyeState") or "unknown"
        key = f"{orig} → {corr}"
        eye_changes[key] = eye_changes.get(key, 0) + 1

    return jsonify({
        "corrections": corrections,
        "count": len(corrections),
        "lastTrained": last_trained_ts,
        "eyeStateChanges": eye_changes,
    })


@app.route("/api/correction/resolve", methods=["POST"])
def api_correction_resolve():
    """Resolve a phantom correction by filling in its eye-state label.

    Body: {id: int, eyeState: "eyes_open" | "eyes_closed" | "face_not_visible" | "not_in_bassinet"}

    Updates both the correction row (corrected_eye_state + corrected_at) and
    the matching entry (eyeState, eyeStateEdited=1), keeping SQLite + JSONL
    in sync. Used for phantom rows created before the 2026-04-19 bugfix that
    stopped logging corrections on bbox-only updates.
    """
    data = request.get_json(silent=True) or {}
    correction_id = data.get("id")
    new_eye_state = data.get("eyeState")

    allowed = {"eyes_open", "eyes_closed", "face_not_visible", "not_in_bassinet"}
    if not isinstance(correction_id, int):
        return jsonify({"error": "id (int) required"}), 400
    if new_eye_state not in allowed:
        return jsonify({"error": f"eyeState must be one of {sorted(allowed)}"}), 400

    db = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved = db.resolve_correction(correction_id, new_eye_state, now)
    if resolved is None:
        return jsonify({"error": "correction not found"}), 404

    ts = resolved["originalTimestamp"]

    # Mirror the label into the entry, same way api_update_entry does.
    entry_updates = {
        "eyeState": new_eye_state,
        "eyeStateEdited": True,
        "eyeStateCorrectedAt": now,
    }
    db.update_entry(ts, entry_updates)

    # Keep JSONL backup in sync with SQLite. Missing line is tolerated —
    # SQLite is authoritative; a JSONL row can be absent for older entries
    # that were only ever dual-written after migration.
    try:
        lines = SLEEP_LOG.read_text().strip().splitlines()
        new_lines = []
        for line in lines:
            entry = json.loads(line)
            if entry.get("timestamp") == ts:
                entry.update(entry_updates)
            new_lines.append(json.dumps(entry))
        SLEEP_LOG.write_text("\n".join(new_lines) + "\n")
    except FileNotFoundError:
        pass

    # Append to corrections.jsonl backup so the append-only log reflects the
    # resolution (the original phantom row is already in there with nulls).
    with open(CORRECTIONS_LOG, "a") as f:
        f.write(json.dumps({
            "correctedAt": now,
            "originalTimestamp": ts,
            "correctedEyeState": new_eye_state,
            "source": "dashboard-resolve",
            "resolvedCorrectionId": correction_id,
        }) + "\n")

    return jsonify({"ok": True, "id": correction_id, "eyeState": new_eye_state})


@app.route("/api/correction/discard", methods=["POST"])
def api_correction_discard():
    """Delete a phantom correction row outright.

    Body: {id: int}

    The row is removed from the corrections table; the underlying entry's
    label is untouched. corrections.jsonl is append-only, so this does not
    rewrite it — SQLite is authoritative for training label reads.
    """
    data = request.get_json(silent=True) or {}
    correction_id = data.get("id")
    if not isinstance(correction_id, int):
        return jsonify({"error": "id (int) required"}), 400
    db = get_db()
    ok = db.delete_correction(correction_id)
    if not ok:
        return jsonify({"error": "correction not found"}), 404
    return jsonify({"ok": True, "id": correction_id})


@app.route("/api/system-usage")
def api_system_usage():
    """Snapshot of machine load, memory, disk, and baby-monitor processes.

    Exposed for the System tab's "System Load" card. Pure stdlib — implemented
    in dashboard/system_usage.py so it can also be run as a CLI without the
    Flask process. Directory-size lookups inside are cached for 60s so the
    10s-poll UI doesn't re-walk data/frames/ on every tick.
    """
    from system_usage import gather as _gather_system_usage
    return jsonify(_gather_system_usage())


# Health checks need a freshness boundary and a gap threshold. Both live
# here so they're easy to retune later.
PIPELINE_FRESH_SEC = 5 * 60        # frames < 5 min old → "fresh"
PIPELINE_STALE_SEC = 15 * 60       # 5–15 min → "stale"; >15 min → "down"
PIPELINE_GAP_THRESHOLD_MIN = 10    # gaps under this don't count
PIPELINE_NOMINAL_INTERVAL_SEC = 60 # launchd StartInterval for com.baby-monitor


def _parse_launchctl_list_baby_monitor() -> list[dict]:
    """Parse `launchctl list` rows for baby-monitor jobs.

    Each row is `<pid>\\t<lastExit>\\t<label>`. PID `-` means not currently
    running (which is the *expected* state for the cron-style monitor and
    watchdog jobs — they run, exit, and are re-launched on StartInterval).
    A non-zero `lastExit` on those means the most recent tick crashed.

    Returns an empty list if `launchctl` isn't on PATH or returned an error
    — the panel will surface that as a UI warning rather than a hard 500.
    """
    try:
        proc = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    # Job kinds — we know the schedule because we own these plists. The
    # `kind` label is consumed by the UI to render the right freshness
    # signal (persistent jobs are "down" if PID is missing; scheduled
    # jobs are "down" only if lastExit != 0).
    kinds = {
        "com.baby-monitor": "scheduled",          # 1-min capture
        "com.baby-monitor-watchdog": "scheduled", # 2-min staleness check
        "com.baby-monitor-dashboard": "persistent",
        # Retrain is supposed to be unloaded (manual-only policy). If it shows
        # up here, surface it — non-zero lastExit already renders red on the
        # frontend, so a silently-crashing daily run becomes visible.
        "com.baby-monitor-retrain": "scheduled",
    }

    jobs = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_str, exit_str, label = parts[0], parts[1], parts[2]
        if label not in kinds:
            continue
        try:
            last_exit = int(exit_str)
        except ValueError:
            last_exit = None
        pid = None
        if pid_str.isdigit():
            pid = int(pid_str)
        jobs.append({
            "label": label,
            "kind": kinds[label],
            "pid": pid,
            "lastExit": last_exit,
        })
    # Stable display order: capture → watchdog → dashboard → retrain.
    order = [
        "com.baby-monitor",
        "com.baby-monitor-watchdog",
        "com.baby-monitor-dashboard",
        "com.baby-monitor-retrain",
    ]
    jobs.sort(key=lambda j: order.index(j["label"]) if j["label"] in order else 99)
    return jobs


@app.route("/api/pipeline-health")
def api_pipeline_health():
    """Operational health of the baby-monitor capture pipeline.

    Surfaces capture freshness, gap timeline, detection-method mix, launchd
    job state, and the watchdog's view of the current outage (if any). Read
    by the System-tab "Pipeline Health" card on a 10s polling loop.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    get_db()  # ensures init_db() has run before raw queries
    conn = get_connection()

    # --- Last entry / freshness ---
    last_row = conn.execute(
        "SELECT timestamp FROM entries ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    last_entry: dict | None = None
    if last_row:
        last_ts_str = last_row["timestamp"]
        last_dt = datetime.strptime(last_ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        age_sec = int((now - last_dt).total_seconds())
        if age_sec < PIPELINE_FRESH_SEC:
            freshness = "fresh"
        elif age_sec < PIPELINE_STALE_SEC:
            freshness = "stale"
        else:
            freshness = "down"
        last_entry = {
            "timestamp": last_ts_str,
            "ageSeconds": age_sec,
            "freshness": freshness,
        }

    # --- Captures in last 24 h ---
    actual_24h = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE timestamp > ?", (cutoff_str,),
    ).fetchone()[0]
    nominal_per_24h = (24 * 3600) // PIPELINE_NOMINAL_INTERVAL_SEC  # 1440

    # --- Gaps > threshold in last 24 h ---
    rows = conn.execute(
        "SELECT timestamp FROM entries WHERE timestamp > ? ORDER BY timestamp ASC",
        (cutoff_str,),
    ).fetchall()
    gap_items = []
    total_missed_sec = 0.0
    threshold_sec = PIPELINE_GAP_THRESHOLD_MIN * 60
    for i in range(1, len(rows)):
        a = datetime.strptime(rows[i - 1]["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        b = datetime.strptime(rows[i]["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        gap_sec = (b - a).total_seconds()
        if gap_sec > threshold_sec:
            total_missed_sec += gap_sec
            gap_items.append({
                "start": rows[i - 1]["timestamp"],
                "end": rows[i]["timestamp"],
                "minutes": round(gap_sec / 60, 1),
            })
    # Tail gap: from the last entry to now (if it's already past threshold,
    # this is the *currently-running* outage). Surfaced separately so the UI
    # can render it as "ongoing".
    ongoing_gap = None
    if rows:
        last_dt = datetime.strptime(rows[-1]["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        gap_to_now = (now - last_dt).total_seconds()
        if gap_to_now > threshold_sec:
            ongoing_gap = {
                "start": rows[-1]["timestamp"],
                "minutes": round(gap_to_now / 60, 1),
            }
    # Newest first so the most recent gaps render at the top.
    gap_items.sort(key=lambda g: g["end"], reverse=True)

    # --- Detection-method mix ---
    method_rows = conn.execute(
        "SELECT detection_method, COUNT(*) c FROM entries "
        "WHERE timestamp > ? GROUP BY detection_method ORDER BY c DESC",
        (cutoff_str,),
    ).fetchall()
    total_methods = sum(r["c"] for r in method_rows) or 1
    detection_methods = [
        {
            "method": r["detection_method"] or "unknown",
            "count": r["c"],
            "pct": round(100.0 * r["c"] / total_methods, 1),
        }
        for r in method_rows
    ]

    # --- Cloud API call metrics ---
    # Cloud is *attempted* whenever BIRDEYE bails (birdeyeFallback is set in
    # the row's data JSON). It *succeeds* if the resulting row is tagged
    # detection_method='vision-api'; it *fails* if cloudUnavailable=true.
    # Querying the JSON columns is fine at this volume (~1-2k rows / 24 h).
    cloud_attempted = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE timestamp > ? "
        "AND json_extract(data, '$.birdeyeFallback') IS NOT NULL",
        (cutoff_str,),
    ).fetchone()[0]
    cloud_succeeded = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE timestamp > ? "
        "AND detection_method = 'vision-api'",
        (cutoff_str,),
    ).fetchone()[0]
    cloud_failed = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE timestamp > ? "
        "AND json_extract(data, '$.cloudUnavailable') = 1",
        (cutoff_str,),
    ).fetchone()[0]
    # `insufficient_quota` is a distinct OpenAI failure mode (account out of
    # credit) — surfacing it separately tells the user "top up the account"
    # vs. transient errors which usually self-heal. Match on "exceeded your
    # current quota" (the human-readable phrase OpenAI emits for this case)
    # because monitor.py truncates the reason at 200 chars, which falls
    # before the `'type': 'insufficient_quota'` field.
    quota_exhausted = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE timestamp > ? "
        "AND json_extract(data, '$.cloudUnavailable') = 1 "
        "AND json_extract(data, '$.cloudUnavailableReason') "
        "    LIKE '%exceeded your current quota%'",
        (cutoff_str,),
    ).fetchone()[0]
    last_failure_row = conn.execute(
        "SELECT timestamp, json_extract(data, '$.cloudUnavailableReason') AS reason "
        "FROM entries WHERE json_extract(data, '$.cloudUnavailable') = 1 "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    last_failure = None
    if last_failure_row and last_failure_row["reason"]:
        last_failure = {
            "timestamp": last_failure_row["timestamp"],
            "reason": last_failure_row["reason"],
        }
    cloud_calls = {
        "attempted": cloud_attempted,
        "succeeded": cloud_succeeded,
        "failed": cloud_failed,
        "quotaExhausted": quota_exhausted,
        "lastFailure": last_failure,
    }

    # --- launchd jobs ---
    launchd_jobs = _parse_launchctl_list_baby_monitor()

    # --- Watchdog state file ---
    watchdog: dict | None = None
    wd_path = DATA_DIR / "watchdog-state.json"
    if wd_path.is_file():
        try:
            wd = json.loads(wd_path.read_text())
            watchdog = {
                "outageStartedAt": wd.get("outage_started_at"),
                "outageActive": bool(wd.get("outage_started_at")),
                "lastAlertAt": wd.get("last_alert_at"),
                "lastAlertKind": wd.get("last_alert_kind"),
            }
        except (OSError, json.JSONDecodeError):
            watchdog = None

    return jsonify({
        "asOf": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastEntry": last_entry,
        "captures24h": {
            "actual": actual_24h,
            "nominal": nominal_per_24h,
            "intervalSec": PIPELINE_NOMINAL_INTERVAL_SEC,
        },
        "gaps24h": {
            "thresholdMin": PIPELINE_GAP_THRESHOLD_MIN,
            "count": len(gap_items),
            "totalMissedMin": round(total_missed_sec / 60, 1),
            "items": gap_items[:20],
            "ongoing": ongoing_gap,
        },
        "detectionMethods24h": detection_methods,
        "cloudCalls24h": cloud_calls,
        "launchdJobs": launchd_jobs,
        "watchdog": watchdog,
    })


@app.route("/api/classification-rate")
def api_classification_rate():
    """Per-bucket classification outcomes for the System-tab chart.

    Bucket = 1 hour by default; ?bucketMin overrides. For each bucket we
    return counts split by outcome class so the UI can stack-render and
    the user can spot capture gaps and cloud-failure spikes at a glance.

    Outcome classes:
      - birdeye:           BIRDEYE handled the frame cleanly (healthy)
      - pixel-diff:        Empty bassinet, classified by pixel-diff (healthy)
      - cloud-success:     BIRDEYE bailed, cloud API filled in (degraded ok)
      - cloud-failed:      BIRDEYE bailed, cloud also failed (cloudUnavailable=true)
      - other:             anything else (stub, future detection methods)
    """
    try:
        hours = int(request.args.get("hours", 24))
        bucket_min = int(request.args.get("bucketMin", 60))
    except ValueError:
        return jsonify({"error": "hours and bucketMin must be integers"}), 400
    hours = max(1, min(hours, 168))  # 1h .. 7d
    bucket_min = max(5, min(bucket_min, 360))  # 5min .. 6h

    get_db()
    conn = get_connection()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    # Pre-compute the bucket boundaries in UTC. Aligning to whole UTC hours
    # for hourly buckets makes the chart labels (rendered ET on the UI) line
    # up with clock time rather than drifting by a few seconds per refresh.
    bucket_sec = bucket_min * 60
    if bucket_sec >= 3600 and 3600 % bucket_sec == 0:
        # Snap "now" down to the next bucket boundary so the rightmost
        # bucket represents the in-progress slot, not a partial slice.
        anchor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        anchor = now.replace(second=0, microsecond=0)
        # Round up to next bucket
        offset = (anchor.minute * 60) % bucket_sec
        anchor = anchor + timedelta(seconds=bucket_sec - offset) if offset else anchor

    n_buckets = max(1, int((hours * 3600) // bucket_sec))
    starts = [anchor - timedelta(seconds=(n_buckets - i) * bucket_sec) for i in range(n_buckets)]

    # Fetch all rows in window with the fields we need to bucket. JSON
    # extract pulls birdeyeFallback / cloudUnavailable from the data blob.
    rows = conn.execute(
        "SELECT timestamp, detection_method, "
        "       json_extract(data, '$.birdeyeFallback')   AS birdeye_fallback, "
        "       json_extract(data, '$.cloudUnavailable')  AS cloud_unavailable "
        "FROM entries WHERE timestamp > ?",
        (cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()

    def classify(r) -> str:
        if r["cloud_unavailable"] in (1, "1", True):
            return "cloud-failed"
        m = r["detection_method"]
        if m == "vision-api":
            return "cloud-success"
        if m == "birdeye":
            return "birdeye"
        if m == "pixel-diff":
            return "pixel-diff"
        return "other"

    # Bin
    bins = []
    for s in starts:
        bins.append({
            "start": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endExclusive": (s + timedelta(seconds=bucket_sec)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "birdeye": 0,
            "pixel-diff": 0,
            "cloud-success": 0,
            "cloud-failed": 0,
            "other": 0,
            "total": 0,
        })

    if bins:
        first_start_ts = datetime.strptime(bins[0]["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        for r in rows:
            try:
                ts = datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            idx = int((ts - first_start_ts).total_seconds() // bucket_sec)
            if idx < 0 or idx >= len(bins):
                continue
            cls = classify(r)
            bins[idx][cls] += 1
            bins[idx]["total"] += 1

    nominal_per_bucket = bucket_sec // PIPELINE_NOMINAL_INTERVAL_SEC

    return jsonify({
        "asOf": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours": hours,
        "bucketMin": bucket_min,
        "nominalPerBucket": nominal_per_bucket,
        "buckets": bins,
    })


_AQ_NUMERIC_KEYS = ("co2", "pm25", "temp", "rh", "tvoc_index")


def _bucket_air_quality(rows: list[dict], max_points: int) -> list[dict]:
    """Down-sample raw 1-minute readings to <= max_points by averaging
    consecutive buckets. Each bucket's timestamp is the median row's. None
    values are skipped per-field; if a whole bucket is None for a field, the
    output is None and the chart will show a gap."""
    n = len(rows)
    if n <= max_points:
        return rows
    bucket_size = math.ceil(n / max_points)
    out = []
    for i in range(0, n, bucket_size):
        chunk = rows[i:i + bucket_size]
        mid = chunk[len(chunk) // 2]
        agg = {"t": mid["t"]}
        for key in _AQ_NUMERIC_KEYS:
            vals = [r[key] for r in chunk if r[key] is not None]
            agg[key] = round(sum(vals) / len(vals), 2) if vals else None
        out.append(agg)
    return out


@app.route("/api/air-quality")
def api_air_quality():
    """Time-series readings from the AirGradient logger DB.

    Reads ~/airgradient-logger/airgradient.db (override via AIRGRADIENT_DB_PATH)
    read-only, so the logger's writes never contend with the dashboard's
    reads. Down-samples to AIR_QUALITY_MAX_POINTS by bucket-averaging. Returns
    {hours, points: [{t, co2, pm25, temp, rh}], latest, note} — `note` is set
    when the DB is missing or empty so the UI can show a friendly message.
    """
    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    hours = max(1, min(hours, 720))

    if not Path(AIRGRADIENT_DB_PATH).exists():
        return jsonify({
            "hours": hours,
            "points": [],
            "latest": None,
            "note": f"AirGradient DB not found at {AIRGRADIENT_DB_PATH}",
        })

    # SELECT projection: pulls the typed columns + tvoc_index out of the raw
    # JSON blob. tvoc_index is the human-meaningful 0–500 sensor index;
    # tvoc_raw (also stored) is the raw Sensirion register value. We didn't
    # break tvoc_index out into its own column when the logger schema was
    # set, but raw_json preserves the full payload, so json_extract recovers
    # it without a schema migration.
    SELECT_COLS = (
        "SELECT recorded_at, co2_ppm, pm25, temperature_c, humidity_pct, "
        "json_extract(raw_json, '$.tvocIndex') AS tvoc_index "
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # URI read-only — coexists with the logger's writer without lock waits.
        conn = sqlite3.connect(f"file:{AIRGRADIENT_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            SELECT_COLS + "FROM readings WHERE recorded_at >= ? ORDER BY recorded_at ASC",
            (cutoff,),
        ).fetchall()
        latest_row = conn.execute(
            SELECT_COLS + "FROM readings ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except sqlite3.Error as e:
        return jsonify({
            "hours": hours,
            "points": [],
            "latest": None,
            "note": f"sqlite error: {e}",
        }), 200

    def _row_to_point(r):
        tv = r["tvoc_index"]
        return {
            "t": r["recorded_at"],
            "co2": r["co2_ppm"],
            "pm25": r["pm25"],
            "temp": r["temperature_c"],
            "rh": r["humidity_pct"],
            "tvoc_index": int(tv) if tv is not None else None,
        }

    # raw_points: full 1-minute resolution — used for analysis (alerts,
    # insights, health %). display_points: bucketed for the chart polylines
    # so the SVG never has to render more than ~360 segments.
    raw_points = [_row_to_point(r) for r in rows]
    display_points = _bucket_air_quality(list(raw_points), AIR_QUALITY_MAX_POINTS)

    latest = _row_to_point(latest_row) if latest_row is not None else None

    # Bassinet state transitions in the same window — overlaid as vlines on
    # each chart so the user can correlate AQ excursions with state changes.
    db = get_db()
    transitions = db.get_state_transitions(start=cutoff)

    # Computed analysis — see dashboard/aq_analysis.py for the rules.
    import aq_analysis as _aq
    score = _aq.comfort_score(latest)
    statuses = _aq.latest_with_status(latest)
    alerts = _aq.compute_alerts(raw_points, latest)
    insights = _aq.compute_insights(raw_points, latest)
    recommendations = _aq.compute_recommendations(latest)
    bad_zones = _aq.compute_bad_zones(raw_points)
    health = _aq.compute_health(raw_points, latest)

    note = None if raw_points else "No readings in this window."

    return jsonify({
        "hours": hours,
        "points": display_points,
        "latest": latest,
        "statuses": statuses,
        "score": score,
        "alerts": alerts,
        "insights": insights,
        "recommendations": recommendations,
        "badZones": bad_zones,
        "health": health,
        "transitions": transitions,
        "note": note,
    })


@app.route("/api/mark-reviewed", methods=["POST"])
def api_mark_reviewed():
    """Mark a list of entries as reviewed (human-confirmed ground truth)."""
    data = request.get_json()
    timestamps = data.get("timestamps", [])
    if not timestamps:
        return jsonify({"error": "timestamps required"}), 400

    db = get_db()
    updated = db.mark_reviewed(timestamps)
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/run-inference", methods=["POST"])
def api_run_inference():
    """Re-run BIRDEYE inference on a single frame and update its entry.

    Shells out to the main venv's Python since the dashboard venv doesn't
    have torch/cv2.
    """
    data = request.get_json()
    ts = data.get("timestamp")
    if not ts:
        return jsonify({"error": "timestamp required"}), 400

    # Run inference via a subprocess using the main venv (which has torch/cv2)
    script = str(DATA_DIR.parent / "scripts" / "run_single_inference.py")
    python = str(DATA_DIR.parent / "venv" / "bin" / "python3")

    try:
        result = subprocess.run(
            [python, script, ts],
            capture_output=True, text=True, timeout=30,
            cwd=str(DATA_DIR.parent),
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()[:200]}), 500
        resp = json.loads(result.stdout)
        return jsonify(resp)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "inference timed out"}), 504
    except (json.JSONDecodeError, Exception) as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/api/safety-stats")
def api_safety_stats():
    """Per-classifier safety + quality breakdown for the new dashboard panel.

    Query params:
        hours: lookback window for the cloud-API ground-truth source
               (default 168 = 7 days). The corrections-side metric is
               read from the deployed model's training_runs row and is
               not windowed.
    """
    hours = float(request.args.get("hours", 168))
    db = get_db()
    return jsonify(db.get_safety_stats(hours))


@app.route("/api/monitor-stats")
def api_monitor_stats():
    """Model performance stats — powered by SQLite.

    Query params:
        hours: lookback window (default 24)
    """
    hours = float(request.args.get("hours", 24))
    db = get_db()
    return jsonify(db.get_monitor_stats(hours))


@app.route("/api/eye-state-daily-metrics")
def api_eye_state_daily_metrics():
    """Per-ET-day BIRDEYE eye-state P/R/F1 for eyes_open and eyes_closed.

    Query params:
        days: lookback in days (default 14, clamped to [1, 90])
    """
    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    days = max(1, min(90, days))
    return jsonify(get_db().get_eye_state_daily_metrics(days))


@app.route("/api/pipeline-history")
def api_pipeline_history():
    """Per-ET-day detection-method breakdown for the Pipeline History card.

    Query params:
        days: lookback in days (default 14, clamped to [1, 90])
    """
    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    days = max(1, min(90, days))
    return jsonify(get_db().get_pipeline_history(days))


@app.route("/api/frame")
def api_frame():
    frame_path = request.args.get("path", "")
    if not frame_path:
        abort(400)
    frames_dir = str(DATA_DIR / "frames")
    requested = os.path.realpath(frame_path)
    if not requested.startswith(frames_dir):
        abort(403)
    if not os.path.isfile(requested):
        abort(404)
    return send_file(requested, mimetype="image/jpeg")


# ---------------------------------------------------------------------------
# Recap (time-lapse video)
# ---------------------------------------------------------------------------
# Stitches a day's frames into an MP4 via ffmpeg's concat demuxer.
# Cache layout (under data/videos/):
#   recap_<date>_fps<N>.mp4        — the video
#   recap_<date>_fps<N>.meta.json  — {"frame_count": int, "generated_at": ISO}
# Cache is reused when the current frame count for the date still matches.

_RECAP_NAME_RE = re.compile(r"^recap_\d{4}-\d{2}-\d{2}_fps\d+\.mp4$")
_ALLOWED_FPS = {15, 30, 60}
_RECAP_TIMEOUT_SEC = 240


def _resolve_ffmpeg() -> str:
    # launchd-spawned processes get a stripped PATH, so shutil.which alone
    # won't find /usr/local/bin/ffmpeg. Look in the usual Homebrew spots too.
    for candidate in ("ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"):
        path = shutil.which(candidate) if "/" not in candidate else (candidate if os.path.isfile(candidate) else None)
        if path:
            return path
    raise RuntimeError("ffmpeg not found on PATH or in standard Homebrew locations")


def _recap_date_range_utc(date_str: str) -> tuple[str, str]:
    """ET date (YYYY-MM-DD) → [start_utc, end_utc] ISO-Z strings.

    A "date" here means the *night* that began on that ET date: 4 PM ET on
    `date` through 11 AM ET on `date + 1`. The baby is out of the bassinet
    for most of the daytime window we exclude, so trimming to the overnight
    span gives a continuous-sleep recap rather than a jumpy day montage.
    """
    y, m, d = (int(x) for x in date_str.split("-"))
    start_et = datetime(y, m, d, 16, 0, 0, tzinfo=ET)
    end_et = (start_et + timedelta(hours=19)).replace(minute=0, second=0)
    to_z = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return to_z(start_et), to_z(end_et)


def _stitch_frames(frame_paths: list[str], out_path: Path, fps: int) -> None:
    """Run ffmpeg concat demuxer. Raises RuntimeError on non-zero exit."""
    dur = 1.0 / fps
    # concat demuxer list: each file needs an entry; a trailing `file` line
    # without duration would get a 1-frame default, so we list the final
    # image twice to make the last shown frame match the others.
    lines = ["ffconcat version 1.0"]
    for p in frame_paths:
        lines.append(f"file {shlex.quote(p)}")
        lines.append(f"duration {dur:.6f}")
    lines.append(f"file {shlex.quote(frame_paths[-1])}")
    list_text = "\n".join(lines) + "\n"

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(list_text)
        list_path = tf.name
    try:
        cmd = [
            _resolve_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-vf", "scale=720:-2,fps=" + str(fps),
            "-c:v", "libx264", "-preset", "fast", "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_RECAP_TIMEOUT_SEC)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


@app.route("/api/recap/generate", methods=["POST"])
def api_recap_generate():
    body = request.get_json(silent=True) or {}
    date_str = str(body.get("date", "")).strip()
    try:
        fps = int(body.get("fps", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "fps must be an integer"}), 400
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    if fps not in _ALLOWED_FPS:
        return jsonify({"error": f"fps must be one of {sorted(_ALLOWED_FPS)}"}), 400
    force = bool(body.get("force"))

    start_utc, end_utc = _recap_date_range_utc(date_str)
    entries = get_entries(start=start_utc, end=end_utc)
    # In-bassinet only — out-of-bassinet frames make the recap visually
    # noisy (empty crib, motion blur from putdowns, lighting changes during
    # carry-away) and obscure the actual sleep narrative. Existing cached
    # MP4s will auto-invalidate because the frame_count meta will differ.
    frame_paths = [e["frame"] for e in entries
                   if e.get("babyPresent")
                   and e.get("frame") and os.path.isfile(e["frame"])]

    if not frame_paths:
        return jsonify({"status": "empty", "date": date_str, "fps": fps, "frame_count": 0})

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"recap_{date_str}_fps{fps}.mp4"
    out_path = VIDEOS_DIR / name
    meta_path = VIDEOS_DIR / f"recap_{date_str}_fps{fps}.meta.json"

    cached = False
    if out_path.exists() and meta_path.exists() and not force:
        try:
            prev = json.loads(meta_path.read_text())
            if prev.get("frame_count") == len(frame_paths) and prev.get("fps") == fps:
                cached = True
        except (OSError, json.JSONDecodeError):
            cached = False

    if not cached:
        try:
            _stitch_frames(frame_paths, out_path, fps)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "ffmpeg timed out"}), 504
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500
        meta = {
            "frame_count": len(frame_paths),
            "fps": fps,
            "date": date_str,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        meta_path.write_text(json.dumps(meta))

    size = out_path.stat().st_size
    return jsonify({
        "status": "ready",
        "cached": cached,
        "date": date_str,
        "fps": fps,
        "frame_count": len(frame_paths),
        "duration_sec": len(frame_paths) / fps,
        "size_bytes": size,
        "video_url": f"/api/recap/video?name={name}",
    })


@app.route("/api/recap/video")
def api_recap_video():
    name = request.args.get("name", "")
    if not _RECAP_NAME_RE.match(name):
        abort(400)
    path = VIDEOS_DIR / name
    if not path.is_file():
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
