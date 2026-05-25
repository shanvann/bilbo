"""Per-frame entry operations: timeline, single-entry edits, review marker.

The functions return Python primitives (dict / list); the HTTP layer is
responsible for jsonify-ing them. Bodies that need shaping (eg. the
correction-mirror logic in `update_entry`) match the previous dashboard
behavior 1:1 — the JSONL backup is updated alongside SQLite and a
correction row is logged only when a label field actually changed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bilbo.api import inference as _inference
from bilbo.config import CORRECTIONS_FILE, DATA_DIR, JSONL_FILE
from bilbo.storage.db import get_db

ET = timezone(timedelta(hours=-4))


def _load_csv_feeds(cutoff: datetime) -> list[dict]:
    """Return Feed rows from data/activity-log.csv whose Start >= cutoff.

    The CSV is a sibling backup to the JSONL/SQLite log — used to overlay
    user-logged feed events on the timeline.
    """
    import csv

    feeds: list[dict] = []
    activity_csv = DATA_DIR / "activity-log.csv"
    if not activity_csv.exists():
        return feeds
    with open(activity_csv, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Type") != "Feed":
                continue
            dt = _parse_csv_dt(row.get("Start", ""))
            if dt and dt >= cutoff:
                feeds.append({
                    "timestamp": dt.isoformat(),
                    "type": "Feed",
                    "condition": row.get("Start Condition", ""),
                    "location": row.get("Start Location", ""),
                    "notes": row.get("Notes", ""),
                })
    return feeds


def _parse_csv_dt(dt_str: str) -> datetime | None:
    if not dt_str or not dt_str.strip():
        return None
    try:
        naive = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=ET).astimezone(timezone.utc)
    except ValueError:
        return None


def timeline(*, date: str | None = None, hours: int = 24) -> dict:
    """Timeline entries + same-window feeds.

    Mirrors the /api/timeline route. When `date` (ET YYYY-MM-DD) is set,
    the window is 4 PM ET on that date → 11 AM ET the next day (19 h).
    Otherwise pulls the last `hours` of UTC entries.
    """
    db = get_db()

    if date:
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        night_start = datetime.strptime(date, "%Y-%m-%d").replace(
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

    timeline_rows = []
    for e in raw_entries:
        shadow = e.get("shadow") or {}
        timeline_rows.append({
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

    feeds = _load_csv_feeds(cutoff)
    return {"entries": timeline_rows, "feeds": feeds}


def update_entry(*, timestamp: str | None, state: str | None = None,
                 position: str | None = None, eye_state: str | None = None,
                 face_bbox: dict | None | object = ...) -> dict:
    """Update fields on the entry identified by `timestamp`.

    `face_bbox` uses a sentinel default (...) to distinguish "unset" from
    "explicit null" (which clears the correction). Returns
    {ok: True} on success, {ok: False, error, status} on failure.
    """
    if not timestamp:
        return {"ok": False, "error": "timestamp required", "_status": 400}

    db = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    updates: dict = {}
    if state:
        updates["state"] = state
        updates["stateEdited"] = True
    if position:
        updates["sleepPosition"] = position
        updates["positionEdited"] = True
    if eye_state:
        updates["eyeState"] = eye_state
        updates["eyeStateEdited"] = True
        updates["eyeStateCorrectedAt"] = now
        # Keep babyPresent (and the derived state) consistent with the
        # eyeState correction. The timeline classifier reads babyPresent
        # first (app.js::stateCategory) — without this lockstep flip,
        # correcting to "not_in_bassinet" leaves babyPresent=true and the
        # block keeps rendering as "Unknown (in bassinet)". The temporal
        # smoother will re-derive state on the next backfill_state.py run.
        if eye_state == "not_in_bassinet":
            updates["babyPresent"] = False
            updates["state"] = "not_present"
        elif eye_state in ("eyes_open", "eyes_closed", "face_not_visible"):
            updates["babyPresent"] = True
    if face_bbox is not ...:
        # None clears the correction, dict sets it.
        updates["faceBboxCorrected"] = face_bbox if face_bbox else None

    if not db.update_entry(timestamp, updates):
        return {"ok": False, "error": "entry not found", "_status": 404}

    # Mirror the change into the JSONL backup.
    original_entry = None
    if JSONL_FILE.exists():
        lines = JSONL_FILE.read_text().strip().splitlines()
        new_lines = []
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if entry.get("timestamp") == timestamp:
                original_entry = json.loads(line)
                entry.update(updates)
            new_lines.append(json.dumps(entry))
        JSONL_FILE.write_text("\n".join(new_lines) + "\n")

    # Log a correction row only when a *label* field changed. Bbox-only
    # edits feed the face detector via entries.faceBboxCorrected, not the
    # corrections table. Historically we wrote phantom rows with null
    # corrected_state / corrected_eye_state here, which polluted the
    # pending-corrections view with "?" entries.
    label_changed = bool(state or eye_state or position)
    if original_entry and label_changed:
        correction = {
            "correctedAt": now,
            "originalTimestamp": timestamp,
            "frame": original_entry.get("frame"),
            "originalState": original_entry.get("state"),
            "correctedState": state,
            "originalEyeState": original_entry.get("eyeState"),
            "correctedEyeState": eye_state,
            "originalPosition": original_entry.get("sleepPosition"),
            "correctedPosition": position,
            "detectionMethod": original_entry.get("detectionMethod"),
            "source": "dashboard",
        }
        db.insert_correction(correction)
        with open(CORRECTIONS_FILE, "a") as f:
            f.write(json.dumps(correction) + "\n")

    return {"ok": True}


def mark_reviewed(*, timestamps: list[str]) -> dict:
    """Mark a batch of entries as reviewed (human-confirmed ground truth)."""
    if not timestamps:
        return {"ok": False, "error": "timestamps required", "_status": 400}
    updated = get_db().mark_reviewed(timestamps)
    return {"ok": True, "updated": updated}


def run_inference(*, timestamp: str | None) -> dict:
    """Re-run BIRDEYE on a single entry. Thin wrapper around
    bilbo.api.inference.run_single."""
    if not timestamp:
        return {"ok": False, "error": "timestamp required", "_status": 400}
    return _inference.run_single(timestamp)
