#!/usr/bin/env python3
"""Baby Monitor Dashboard — Flask backend."""

import csv
import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

app = Flask(__name__, static_folder="static")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SLEEP_LOG = DATA_DIR / "sleep-log.jsonl"
ACTIVITY_CSV = DATA_DIR / "activity-log.csv"
CORRECTIONS_LOG = DATA_DIR / "corrections.jsonl"
ET = timezone(timedelta(hours=-4))  # America/New_York (EDT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_jsonl():
    entries = []
    with open(SLEEP_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


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
    entries = load_jsonl()
    if not entries:
        return jsonify({"error": "no data"}), 404

    last = entries[-1]
    ts = parse_ts(last.get("timestamp"))
    now = datetime.now(timezone.utc)

    # Walk backwards to find when current state started
    current_present = last.get("babyPresent")
    current_state = last.get("state")
    state_start = ts

    for e in reversed(entries[:-1]):
        if e.get("babyPresent") != current_present or e.get("state") != current_state:
            break
        state_start = parse_ts(e.get("timestamp")) or state_start

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
    entries = load_jsonl()

    if date_str:
        # Show full day in ET: midnight to midnight
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=et)
        day_end = day_start + timedelta(hours=24)
        cutoff = day_start.astimezone(timezone.utc)
        end_cutoff = day_end.astimezone(timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        end_cutoff = datetime.now(timezone.utc)

    timeline = []
    for e in entries:
        ts = parse_ts(e.get("timestamp"))
        if ts and ts >= cutoff and ts < end_cutoff:
            timeline.append({
                "timestamp": e["timestamp"],
                "babyPresent": e.get("babyPresent"),
                "state": e.get("state"),
                "eyeState": e.get("eyeState"),
                "eyeStateEdited": e.get("eyeStateEdited", False),
                "eyeStateCorrectedAt": e.get("eyeStateCorrectedAt"),
                "detectionMethod": e.get("detectionMethod"),
                "frame": e.get("frame"),
                "alerts": e.get("alerts", []),
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
    entries = load_jsonl()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

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
    """Recent state transitions."""
    entries = load_jsonl()
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

    # Add durations between consecutive events
    for i in range(len(events) - 1):
        ts1 = parse_ts(events[i]["timestamp"])
        ts2 = parse_ts(events[i + 1]["timestamp"])
        if ts1 and ts2:
            events[i]["duration"] = humanize_duration(ts2 - ts1)

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

    if not ts:
        return jsonify({"error": "timestamp required"}), 400

    lines = SLEEP_LOG.read_text().strip().splitlines()
    updated = False
    original_entry = None
    new_lines = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("timestamp") == ts:
            original_entry = json.loads(line)  # snapshot before edit
            if new_state:
                entry["state"] = new_state
                entry["stateEdited"] = True
            if new_position:
                entry["sleepPosition"] = new_position
                entry["positionEdited"] = True
            if new_eye_state:
                entry["eyeState"] = new_eye_state
                entry["eyeStateEdited"] = True
                entry["eyeStateCorrectedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            updated = True
        new_lines.append(json.dumps(entry))

    if not updated:
        return jsonify({"error": "entry not found"}), 404

    SLEEP_LOG.write_text("\n".join(new_lines) + "\n")

    # Log correction for retraining
    if original_entry:
        correction = {
            "correctedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        with open(CORRECTIONS_LOG, "a") as f:
            f.write(json.dumps(correction) + "\n")

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Training manager — tracks running process, supports start/abort/status
# ---------------------------------------------------------------------------
MODELS_DIR = DATA_DIR.parent / "pipeline" / "models"
TRAINING_LOG = MODELS_DIR / "training-log.jsonl"
TRAINING_STATE_FILE = DATA_DIR / "training-state.json"

_train_process = None  # subprocess.Popen when running
_train_lock = threading.Lock()


def _read_training_state() -> dict:
    if TRAINING_STATE_FILE.exists():
        try:
            return json.loads(TRAINING_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_training_state(state: dict):
    TRAINING_STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_last_training_log() -> dict | None:
    if not TRAINING_LOG.exists():
        return None
    lines = TRAINING_LOG.read_text().strip().splitlines()
    if not lines:
        return None
    return json.loads(lines[-1])


@app.route("/api/training-status")
def api_training_status():
    """Return current training run status + last completed training details."""
    global _train_process

    state = _read_training_state()
    last_log = _get_last_training_log()

    # Check if a tracked process is still running
    running = False
    with _train_lock:
        if _train_process is not None:
            if _train_process.poll() is None:
                running = True
            else:
                # Process finished — update state
                exit_code = _train_process.returncode
                state["status"] = "completed" if exit_code == 0 else "failed"
                state["finishedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                state["exitCode"] = exit_code
                _write_training_state(state)
                _train_process = None

    # Count pending corrections (made after last training)
    last_trained_ts = last_log.get("timestamp") if last_log else None
    pending_corrections = 0
    total_corrections = 0
    if CORRECTIONS_LOG.exists():
        for line in CORRECTIONS_LOG.read_text().strip().splitlines():
            if not line:
                continue
            total_corrections += 1
            c = json.loads(line)
            if last_trained_ts and c.get("correctedAt", "") > last_trained_ts:
                pending_corrections += 1
            elif not last_trained_ts:
                pending_corrections += 1

    result = {
        # Current run
        "running": running,
        "runStatus": state.get("status", "idle"),
        "trigger": state.get("trigger"),
        "startedAt": state.get("startedAt"),
        "finishedAt": state.get("finishedAt"),
        "exitCode": state.get("exitCode"),
        # Last completed training
        "lastTrained": last_log.get("timestamp") if last_log else None,
        "version": last_log.get("version") if last_log else None,
        "lastMetrics": last_log.get("metrics") if last_log else None,
        "lastLabelSources": last_log.get("label_sources") if last_log else None,
        "lastEntriesTotal": last_log.get("entries_total") if last_log else None,
        # Corrections
        "pendingCorrections": pending_corrections,
        "totalCorrections": total_corrections,
    }
    return jsonify(result)


@app.route("/api/retrain", methods=["POST"])
def api_retrain():
    """Start model retraining in the background.

    Body (optional): {"trigger": "dashboard" | "manual" | "scheduled"}
    """
    global _train_process

    with _train_lock:
        if _train_process is not None and _train_process.poll() is None:
            return jsonify({"ok": False, "error": "Training already in progress"}), 409

    data = request.get_json(silent=True) or {}
    trigger = data.get("trigger", "dashboard")

    monitor_py = str(DATA_DIR.parent / "scripts" / "monitor.py")
    python = str(DATA_DIR.parent / "venv" / "bin" / "python3")

    # Record start state
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {
        "status": "running",
        "trigger": trigger,
        "startedAt": now,
        "finishedAt": None,
        "exitCode": None,
    }
    _write_training_state(state)

    def run_retrain():
        global _train_process
        proc = subprocess.Popen(
            [python, monitor_py, "--retrain"],
            cwd=str(DATA_DIR.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with _train_lock:
            _train_process = proc
        proc.wait()
        # Update state on completion
        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        s = _read_training_state()
        s["status"] = "completed" if proc.returncode == 0 else "failed"
        s["finishedAt"] = finished
        s["exitCode"] = proc.returncode
        _write_training_state(s)
        with _train_lock:
            _train_process = None

    thread = threading.Thread(target=run_retrain, daemon=True)
    thread.start()

    return jsonify({"ok": True, "startedAt": now, "trigger": trigger})


@app.route("/api/retrain/abort", methods=["POST"])
def api_retrain_abort():
    """Abort a running training process."""
    global _train_process
    import signal  # not worth a top-level import for this one use

    with _train_lock:
        if _train_process is None or _train_process.poll() is not None:
            return jsonify({"ok": False, "error": "No training in progress"}), 404

        _train_process.send_signal(signal.SIGTERM)
        try:
            _train_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _train_process.kill()

        state = _read_training_state()
        state["status"] = "aborted"
        state["finishedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["exitCode"] = _train_process.returncode
        _write_training_state(state)
        _train_process = None

    return jsonify({"ok": True, "status": "aborted"})


@app.route("/api/monitor-stats")
def api_monitor_stats():
    """Model performance stats for the dashboard, computed from sleep-log.jsonl.

    Query params:
        hours: lookback window (default 24)
    """
    from collections import Counter

    hours = float(request.args.get("hours", 24))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    entries = []
    for e in load_jsonl():
        ts = parse_ts(e.get("timestamp"))
        if ts and ts >= cutoff:
            e["_ts"] = ts
            entries.append(e)

    if not entries:
        return jsonify({"total": 0})

    total = len(entries)
    methods = Counter(e.get("detectionMethod", "unknown") for e in entries)

    birdeye = [e for e in entries if e.get("detectionMethod") == "birdeye"]
    cloud = [e for e in entries if e.get("detectionMethod") in ("vision-api", "openai-vision")]
    pixel_diff = [e for e in entries if e.get("detectionMethod") == "pixel-diff"]

    # Birdeye confidence + timing stats
    presence_confs = [e["presenceConfidence"] for e in birdeye if e.get("presenceConfidence") is not None]
    eye_confs = [e["eyeConfidence"] for e in birdeye if e.get("eyeConfidence") is not None]
    timings = [e["birdeyeTimings"]["total"] for e in birdeye
               if isinstance(e.get("birdeyeTimings"), dict) and "total" in e["birdeyeTimings"]]

    def stats(vals):
        if not vals:
            return None
        s = sorted(vals)
        return {
            "avg": round(sum(s) / len(s), 3),
            "min": round(s[0], 3),
            "max": round(s[-1], 3),
            "p50": round(s[len(s) // 2], 3),
        }

    # Birdeye state distribution
    birdeye_states = Counter()
    for e in birdeye:
        if not e.get("babyPresent", False):
            birdeye_states["not_present"] += 1
        else:
            birdeye_states[e.get("state", "Unknown")] += 1

    # Cloud models
    cloud_models = Counter(e.get("modelUsed", "unknown") for e in cloud)

    # Cost
    api_saved = len(birdeye) + len(pixel_diff)

    # Gaps > 10 min
    gap_count = 0
    for i in range(1, len(entries)):
        if (entries[i]["_ts"] - entries[i - 1]["_ts"]).total_seconds() > 600:
            gap_count += 1

    # Shadow birdeye agreement rate (birdeye ran in parallel with prod)
    shadow_entries = [e for e in entries if isinstance(e.get("shadow"), dict)]
    shadow_agreed = sum(1 for e in shadow_entries if e["shadow"].get("agreed"))
    shadow_disagreed = len(shadow_entries) - shadow_agreed

    return jsonify({
        "hours": hours,
        "total": total,
        "methods": {
            "birdeye": len(birdeye),
            "cloud_api": len(cloud),
            "pixel_diff": len(pixel_diff),
            "shadow_disagreed": shadow_disagreed,
        },
        "birdeyeRate": round(len(birdeye) / total, 3) if total else 0,
        "birdeyeStates": dict(birdeye_states),
        "confidence": {
            "presence": stats(presence_confs),
            "eye": stats(eye_confs),
        },
        "timing": stats(timings),
        "cloudModels": dict(cloud_models),
        "cost": {
            "apiCalls": len(cloud),
            "apiAvoided": api_saved,
            "estCost": round(len(cloud) * 0.01, 2),
            "estSaved": round(api_saved * 0.01, 2),
        },
        "gaps": gap_count,
        "shadow": {
            "total": len(shadow_entries),
            "agreed": shadow_agreed,
            "disagreed": shadow_disagreed,
            "agreementRate": round(shadow_agreed / len(shadow_entries), 3) if shadow_entries else None,
        },
    })


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5555, debug=True)
