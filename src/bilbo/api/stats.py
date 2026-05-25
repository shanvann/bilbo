"""Aggregated read-only stats endpoints for the dashboard.

Everything in here is computed from SQLite / CSV — no writes, no model
inference. The functions return Python primitives that match the previous
JSON response bodies key-for-key.
"""
from __future__ import annotations

import csv
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bilbo.config import DATA_DIR, FRAMES_DIR, JSONL_FILE, WATCHDOG_STATE_FILE
from bilbo.storage.db import get_connection, get_db

ET = timezone(timedelta(hours=-4))

# Pipeline-health thresholds (kept inline so they're easy to retune).
PIPELINE_FRESH_SEC = 5 * 60
PIPELINE_STALE_SEC = 15 * 60
PIPELINE_GAP_THRESHOLD_MIN = 10
PIPELINE_NOMINAL_INTERVAL_SEC = 60


# ---------------------------------------------------------------------------
# Helpers shared with the dashboard module
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str | None):
    if not ts_str:
        return None
    ts_str = ts_str.strip()
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def _parse_csv_dt(dt_str: str | None):
    if not dt_str or not dt_str.strip():
        return None
    try:
        naive = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=ET).astimezone(timezone.utc)
    except ValueError:
        return None


def _humanize_duration(td: timedelta) -> str:
    total_sec = int(td.total_seconds())
    if total_sec < 0:
        return "0m"
    hours, rem = divmod(total_sec, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _load_csv_rows() -> list[dict]:
    activity_csv = DATA_DIR / "activity-log.csv"
    if not activity_csv.exists():
        return []
    rows: list[dict] = []
    with open(activity_csv, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Status — most-recent-frame snapshot for the hero card
# ---------------------------------------------------------------------------

def status() -> dict:
    """Latest entry + current run duration for the dashboard hero card.

    Returns {_status: 404} when there's no data yet.
    """
    db = get_db()
    last = db.get_last_entry()
    if not last:
        return {"error": "no data", "_status": 404}

    ts = _parse_ts(last.get("timestamp"))
    now = datetime.now(timezone.utc)

    current_present = bool(last.get("babyPresent"))
    current_state = last.get("state")
    run_start_ts = db.find_current_run_start(current_present, current_state)
    state_start = _parse_ts(run_start_ts) or ts

    duration = now - state_start if state_start else timedelta(0)

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

    return {
        "display": display,
        "icon": icon,
        "duration": _humanize_duration(duration),
        "durationSeconds": int(duration.total_seconds()),
        "timestamp": last.get("timestamp"),
        "frame": last.get("frame"),
        "position": last.get("sleepPosition"),
        "alerts": last.get("alerts", []),
        "captureMode": last.get("captureMode"),
        "secondsSinceCapture": int((now - ts).total_seconds()) if ts else None,
    }


# ---------------------------------------------------------------------------
# Sleep stats (daily totals + longest stretches)
# ---------------------------------------------------------------------------

def sleep_stats(*, days: int = 7) -> dict:
    """Per-ET-day sleep summary (total hours, longest sleep, longest
    in-bassinet stretch, stretch count) for the last `days` days."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    entries = get_db().get_entries(hours=days * 24)

    # Sleep segments: present AND state in {Asleep, Unknown}. Unknown is
    # included because the vision model often can't tell — baby is still
    # in bassinet sleeping.
    segments: list[tuple[datetime, datetime]] = []
    seg_start = seg_end = None
    for e in entries:
        ts = _parse_ts(e.get("timestamp"))
        if not ts or ts < cutoff:
            continue
        state = e.get("state")
        present = e.get("babyPresent")
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

    # In-bassinet segments (any state).
    bassinet_segments: list[tuple[datetime, datetime]] = []
    bseg_start = bseg_end = None
    for e in entries:
        ts = _parse_ts(e.get("timestamp"))
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

    # CSV Sleep rows as fallback for days with sparse JSONL data.
    from collections import Counter
    jsonl_day_counts: Counter = Counter()
    for e in entries:
        ts = _parse_ts(e.get("timestamp"))
        if ts and ts >= cutoff:
            jsonl_day_counts[ts.astimezone(ET).date().isoformat()] += 1

    JSONL_MIN_ENTRIES = 100  # ~7 h of coverage at 4-min intervals

    for row in _load_csv_rows():
        if row.get("Type") != "Sleep":
            continue
        start = _parse_csv_dt(row.get("Start"))
        end = _parse_csv_dt(row.get("End"))
        if start and end and start >= cutoff:
            csv_date = start.astimezone(ET).date().isoformat()
            if jsonl_day_counts.get(csv_date, 0) < JSONL_MIN_ENTRIES:
                segments.append((start, end))

    daily: dict = {}
    for start, end in segments:
        et_date = start.astimezone(ET).date().isoformat()
        dur = (end - start).total_seconds()
        if dur <= 0:
            continue
        d = daily.setdefault(et_date, {
            "total": 0, "longestSleep": 0,
            "longestBassinet": 0, "stretches": 0,
        })
        d["total"] += dur
        d["longestSleep"] = max(d["longestSleep"], dur)
        d["stretches"] += 1

    for start, end in bassinet_segments:
        et_date = start.astimezone(ET).date().isoformat()
        dur = (end - start).total_seconds()
        if dur <= 0:
            continue
        d = daily.setdefault(et_date, {
            "total": 0, "longestSleep": 0,
            "longestBassinet": 0, "stretches": 0,
        })
        d["longestBassinet"] = max(d["longestBassinet"], dur)

    out = []
    for date_str in sorted(daily.keys()):
        d = daily[date_str]
        out.append({
            "date": date_str,
            "totalHours": round(d["total"] / 3600, 1),
            "longestSleepHours": round(d["longestSleep"] / 3600, 1),
            "longestBassinetHours": round(d["longestBassinet"] / 3600, 1),
            "stretches": d["stretches"],
        })

    return {"days": out}


# ---------------------------------------------------------------------------
# Daily in-bassinet vs out-of-bassinet hours
# ---------------------------------------------------------------------------

def bassinet_daily(*, days: int = 7) -> dict:
    db = get_db()
    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    raw = db.get_entries(
        start=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    daily: dict = {}
    for i in range(len(raw) - 1):
        e = raw[i]
        next_e = raw[i + 1]
        ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        next_ts = datetime.fromisoformat(next_e["timestamp"].replace("Z", "+00:00"))
        dur = (next_ts - ts).total_seconds()
        if dur <= 0 or dur > 3600:  # skip gaps > 1h
            continue

        date_str = ts.astimezone(et).strftime("%Y-%m-%d")
        d = daily.setdefault(date_str, {
            "asleep": 0, "awake": 0, "falling_asleep": 0,
            "unknown_in": 0, "out": 0,
        })

        if not e.get("babyPresent"):
            d["out"] += dur
            continue

        state = e.get("state")
        if state == "Asleep":
            d["asleep"] += dur
        elif state == "Awake":
            d["awake"] += dur
        elif state == "FallingAsleep":
            d["falling_asleep"] += dur
        else:
            d["unknown_in"] += dur

    out = []
    for date_str in sorted(daily.keys()):
        d = daily[date_str]
        in_total = d["asleep"] + d["awake"] + d["falling_asleep"] + d["unknown_in"]
        total = in_total + d["out"]
        out.append({
            "date": date_str,
            "asleepHours": round(d["asleep"] / 3600, 1),
            "awakeHours": round(d["awake"] / 3600, 1),
            "fallingAsleepHours": round(d["falling_asleep"] / 3600, 1),
            "unknownInHours": round(d["unknown_in"] / 3600, 1),
            "inHours": round(in_total / 3600, 1),
            "outHours": round(d["out"] / 3600, 1),
            "inPct": round(in_total / total * 100) if total > 0 else 0,
        })

    return {"days": out}


# ---------------------------------------------------------------------------
# Sleep-trend heat grid (per-night 15-min slot dominance)
# ---------------------------------------------------------------------------

def sleep_trend(*, days: int = 14) -> dict:
    """Per-night 15-min slot grid for the Sleep Analysis tab.

    Each "night" spans 4 PM ET on date D through 11 AM ET on date D+1
    (19 h, 76 slots of 15 min). The night is labelled by the date it
    started in ET. For each slot we pick the dominant cell state by
    summed duration of entries falling in the slot.
    """
    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")

    days = max(1, min(60, days))

    SLOT_MIN = 15
    NIGHT_START_HOUR = 16   # 4 PM
    NIGHT_END_HOUR = 11     # 11 AM next day
    SPAN_HOURS = (24 - NIGHT_START_HOUR) + NIGHT_END_HOUR  # 19
    SLOTS_PER_NIGHT = SPAN_HOURS * (60 // SLOT_MIN)        # 76

    now_et = datetime.now(timezone.utc).astimezone(et)
    today_et = now_et.date()
    most_recent = today_et if now_et.hour >= NIGHT_START_HOUR else today_et - timedelta(days=1)
    night_dates = [most_recent - timedelta(days=i) for i in range(days)]

    earliest_start = datetime.combine(
        night_dates[-1],
        datetime.min.time().replace(hour=NIGHT_START_HOUR),
        tzinfo=et,
    ).astimezone(timezone.utc)
    latest_end = datetime.combine(
        night_dates[0] + timedelta(days=1),
        datetime.min.time().replace(hour=NIGHT_END_HOUR),
        tzinfo=et,
    ).astimezone(timezone.utc)

    db = get_db()
    raw = db.get_entries(
        start=earliest_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=latest_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    def categorize(entry):
        if not entry.get("babyPresent"):
            return "out"
        s = entry.get("state")
        if s == "Asleep" or s == "FallingAsleep":
            return "asleep"
        if s == "Awake":
            return "awake"
        return "unknown"

    night_keys = {d.strftime("%Y-%m-%d") for d in night_dates}
    buckets: dict[str, dict[int, dict[str, float]]] = {
        k: {} for k in night_keys
    }

    for i, e in enumerate(raw):
        ts_utc = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        if i + 1 < len(raw):
            next_ts = datetime.fromisoformat(raw[i + 1]["timestamp"].replace("Z", "+00:00"))
            dur = (next_ts - ts_utc).total_seconds()
        else:
            dur = 60.0
        if dur <= 0 or dur > 3600:
            dur = 60.0  # treat gaps as a single capture

        ts_et = ts_utc.astimezone(et)
        h = ts_et.hour
        if h >= NIGHT_START_HOUR:
            night_date = ts_et.date()
            minutes_into_night = (h - NIGHT_START_HOUR) * 60 + ts_et.minute
        elif h < NIGHT_END_HOUR:
            night_date = ts_et.date() - timedelta(days=1)
            minutes_into_night = (24 - NIGHT_START_HOUR + h) * 60 + ts_et.minute
        else:
            continue  # midday gap (11 AM–4 PM)

        slot_idx = minutes_into_night // SLOT_MIN
        if slot_idx < 0 or slot_idx >= SLOTS_PER_NIGHT:
            continue

        key = night_date.strftime("%Y-%m-%d")
        if key not in buckets:
            continue

        cat = categorize(e)
        slot = buckets[key].setdefault(slot_idx, {})
        slot[cat] = slot.get(cat, 0.0) + dur

    nights_out = []
    for d in night_dates:
        key = d.strftime("%Y-%m-%d")
        cells = []
        for slot_idx in range(SLOTS_PER_NIGHT):
            slot = buckets[key].get(slot_idx)
            if not slot:
                cells.append("none")
                continue
            cat = max(slot.items(), key=lambda kv: kv[1])[0]
            cells.append(cat)
        nights_out.append({
            "date": key,
            "label": d.strftime("%a %b %-d"),
            "cells": cells,
        })

    AGG_THRESHOLDS = [("p50", 0.5), ("p90", 0.9)]
    agg_cells: dict[str, list] = {name: [] for name, _ in AGG_THRESHOLDS}
    for slot_idx in range(SLOTS_PER_NIGHT):
        totals: dict[str, float] = {}
        for d in night_dates:
            slot = buckets[d.strftime("%Y-%m-%d")].get(slot_idx)
            if not slot:
                continue
            for cat, sec in slot.items():
                totals[cat] = totals.get(cat, 0.0) + sec
        if not totals:
            for name, _ in AGG_THRESHOLDS:
                agg_cells[name].append({"cat": "none", "share": {}})
            continue
        total_sec = sum(totals.values())
        share = {k: round(v / total_sec, 3) for k, v in totals.items()}
        top_cat, top_sec = max(totals.items(), key=lambda kv: kv[1])
        top_share = top_sec / total_sec
        for name, threshold in AGG_THRESHOLDS:
            cat = top_cat if top_share >= threshold else "mixed"
            agg_cells[name].append({"cat": cat, "share": share})

    return {
        "slotMinutes": SLOT_MIN,
        "slotsPerNight": SLOTS_PER_NIGHT,
        "startHour": NIGHT_START_HOUR,
        "endHour": NIGHT_END_HOUR,
        "nights": nights_out,
        "p50": {
            "label": f"P50 ({len(night_dates)}n)",
            "cells": agg_cells["p50"],
        },
        "p90": {
            "label": f"P90 ({len(night_dates)}n)",
            "cells": agg_cells["p90"],
        },
    }


# ---------------------------------------------------------------------------
# Activity CSV (feeds, diapers)
# ---------------------------------------------------------------------------

def feeds(*, days: int = 1) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for row in _load_csv_rows():
        if row.get("Type") != "Feed":
            continue
        dt = _parse_csv_dt(row.get("Start"))
        if dt and dt >= cutoff:
            out.append({
                "start": row.get("Start", ""),
                "end": row.get("End", ""),
                "duration": row.get("Duration", ""),
                "condition": row.get("Start Condition", ""),
                "location": row.get("Start Location", ""),
                "endCondition": row.get("End Condition", ""),
                "notes": row.get("Notes", ""),
            })
    return {"feeds": out, "count": len(out)}


def diapers(*, days: int = 1) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for row in _load_csv_rows():
        if row.get("Type") != "Diaper":
            continue
        dt = _parse_csv_dt(row.get("Start"))
        if dt and dt >= cutoff:
            out.append({
                "start": row.get("Start", ""),
                "color": row.get("Duration", ""),
                "consistency": row.get("Start Condition", ""),
                "contents": row.get("End Condition", ""),
            })
    return {"diapers": out, "count": len(out)}


# ---------------------------------------------------------------------------
# Events — state transitions
# ---------------------------------------------------------------------------

def events(*, hours: float | None = 72.0, count: int = 20,
           type_filter: str = "all") -> dict:
    """Recent state transitions.

    `hours` of None / <=0 means "all time" (matches the dashboard's
    semantic for hours=0).
    """
    db = get_db()
    use_hours = hours if (hours is not None and hours > 0) else None
    entries = db.get_entries(hours=use_hours)

    out: list[dict] = []
    prev = None

    def effective_state(e):
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

        out.append({
            "timestamp": e["timestamp"],
            "type": event_type,
        })
        prev = e

    # Durations between consecutive (unfiltered) events.
    for i in range(len(out) - 1):
        ts1 = _parse_ts(out[i]["timestamp"])
        ts2 = _parse_ts(out[i + 1]["timestamp"])
        if ts1 and ts2:
            out[i]["duration"] = _humanize_duration(ts2 - ts1)

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
                return not any(s in t for s in
                               ("Placed", "Removed", "Fell asleep", "Woke"))
            return True
        out = [e for e in out if _matches(e["type"])]

    out.reverse()
    return {"events": out[:count]}


# ---------------------------------------------------------------------------
# Model performance + per-classifier safety
# ---------------------------------------------------------------------------

def safety_stats(*, hours: float = 168) -> dict:
    return get_db().get_safety_stats(hours)


def monitor_stats(*, hours: float = 24) -> dict:
    return get_db().get_monitor_stats(hours)


def eye_state_daily_metrics(*, days: int = 14) -> dict:
    days = max(1, min(90, days))
    return get_db().get_eye_state_daily_metrics(days)


def pipeline_history(*, days: int = 14) -> dict:
    days = max(1, min(90, days))
    return get_db().get_pipeline_history(days)


# ---------------------------------------------------------------------------
# Pipeline health + classification-rate (the System tab)
# ---------------------------------------------------------------------------

def _parse_launchctl_list_baby_monitor() -> list[dict]:
    """Parse `launchctl list` rows for baby-monitor jobs.

    Each row is `<pid>\\t<lastExit>\\t<label>`. Returns an empty list if
    launchctl isn't on PATH or returned an error.
    """
    try:
        proc = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    kinds = {
        "com.baby-monitor": "scheduled",
        "com.baby-monitor-watchdog": "scheduled",
        "com.baby-monitor-dashboard": "persistent",
        "com.baby-monitor-retrain": "scheduled",
    }

    jobs: list[dict] = []
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
    order = [
        "com.baby-monitor",
        "com.baby-monitor-watchdog",
        "com.baby-monitor-dashboard",
        "com.baby-monitor-retrain",
    ]
    jobs.sort(key=lambda j: order.index(j["label"]) if j["label"] in order else 99)
    return jobs


def pipeline_health() -> dict:
    """Operational health of the baby-monitor capture pipeline.

    Surfaces capture freshness, gap timeline, detection-method mix,
    launchd job state, and the watchdog's view of the current outage.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    get_db()  # ensure init_db() has run before raw queries
    conn = get_connection()

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

    actual_24h = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE timestamp > ?", (cutoff_str,),
    ).fetchone()[0]
    nominal_per_24h = (24 * 3600) // PIPELINE_NOMINAL_INTERVAL_SEC

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
    gap_items.sort(key=lambda g: g["end"], reverse=True)

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

    launchd_jobs = _parse_launchctl_list_baby_monitor()

    watchdog: dict | None = None
    if WATCHDOG_STATE_FILE.is_file():
        try:
            wd = json.loads(WATCHDOG_STATE_FILE.read_text())
            watchdog = {
                "outageStartedAt": wd.get("outage_started_at"),
                "outageActive": bool(wd.get("outage_started_at")),
                "lastAlertAt": wd.get("last_alert_at"),
                "lastAlertKind": wd.get("last_alert_kind"),
            }
        except (OSError, json.JSONDecodeError):
            watchdog = None

    return {
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
    }


def classification_rate(*, hours: int = 24, bucket_min: int = 60) -> dict:
    """Per-bucket classification outcomes for the System-tab chart.

    Bucket = 1 hour by default; `bucket_min` overrides. Returns
    {_status: 400} on non-integer inputs (matches dashboard behavior).
    """
    hours = max(1, min(hours, 168))  # 1h .. 7d
    bucket_min = max(5, min(bucket_min, 360))  # 5min .. 6h

    get_db()
    conn = get_connection()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    bucket_sec = bucket_min * 60
    if bucket_sec >= 3600 and 3600 % bucket_sec == 0:
        anchor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        anchor = now.replace(second=0, microsecond=0)
        offset = (anchor.minute * 60) % bucket_sec
        anchor = anchor + timedelta(seconds=bucket_sec - offset) if offset else anchor

    n_buckets = max(1, int((hours * 3600) // bucket_sec))
    starts = [anchor - timedelta(seconds=(n_buckets - i) * bucket_sec) for i in range(n_buckets)]

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

    return {
        "asOf": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours": hours,
        "bucketMin": bucket_min,
        "nominalPerBucket": nominal_per_bucket,
        "buckets": bins,
    }
