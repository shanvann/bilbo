#!/usr/bin/env python3
"""Baby Monitor Dashboard — Flask backend."""

import csv
import json
import os
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
    for e in entries:
        if prev is None:
            prev = e
            continue
        changed = (
            e.get("babyPresent") != prev.get("babyPresent")
            or e.get("state") != prev.get("state")
        )
        if changed:
            # Determine event type
            if not prev.get("babyPresent") and e.get("babyPresent"):
                event_type = "Placed in bassinet"
            elif prev.get("babyPresent") and not e.get("babyPresent"):
                event_type = "Removed from bassinet"
            elif e.get("state") == "Asleep" and prev.get("state") != "Asleep":
                event_type = "Fell asleep"
            elif e.get("state") == "Awake" and prev.get("state") == "Asleep":
                event_type = "Woke up"
            else:
                event_type = f"{prev.get('state', '?')} → {e.get('state', '?')}"

            events.append({
                "timestamp": e["timestamp"],
                "type": event_type,
                "position": e.get("sleepPosition"),
            })
        prev = e

    # Add durations between consecutive events
    for i in range(len(events) - 1):
        ts1 = parse_ts(events[i]["timestamp"])
        ts2 = parse_ts(events[i + 1]["timestamp"])
        if ts1 and ts2:
            events[i]["duration"] = humanize_duration(ts2 - ts1)

    # Return last 20, most recent first
    events.reverse()
    return jsonify({"events": events[:20]})


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
