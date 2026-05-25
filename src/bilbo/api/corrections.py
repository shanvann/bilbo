"""Pending-correction inbox + resolve/discard for the "?" phantom rows
left over from the pre-2026-04-19 bug that logged corrections on bbox-only
updates.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from bilbo.config import CORRECTIONS_FILE, JSONL_FILE
from bilbo.storage.db import get_db


_ALLOWED_EYE_STATES = {"eyes_open", "eyes_closed", "face_not_visible", "not_in_bassinet"}


def pending_corrections() -> dict:
    """Pending corrections not yet used in training, with an eye-state
    transition breakdown for the inbox header."""
    db = get_db()
    logs = db.get_last_training_runs(1)
    last_trained_ts = logs[0].get("timestamp") if logs else None
    corrections = db.get_pending_corrections(last_trained_ts)

    eye_changes: dict[str, int] = {}
    for c in corrections:
        orig = c.get("originalEyeState") or "unknown"
        corr = c.get("correctedEyeState") or "unknown"
        key = f"{orig} → {corr}"
        eye_changes[key] = eye_changes.get(key, 0) + 1

    return {
        "corrections": corrections,
        "count": len(corrections),
        "lastTrained": last_trained_ts,
        "eyeStateChanges": eye_changes,
    }


def correction_resolve(*, correction_id: int | None, eye_state: str | None) -> dict:
    """Resolve a phantom correction by filling in its eye-state label.

    Updates both the correction row (corrected_eye_state + corrected_at)
    and the matching entry (eyeState, eyeStateEdited=1), keeping SQLite +
    JSONL in sync.
    """
    if not isinstance(correction_id, int):
        return {"ok": False, "error": "id (int) required", "_status": 400}
    if eye_state not in _ALLOWED_EYE_STATES:
        return {
            "ok": False,
            "error": f"eyeState must be one of {sorted(_ALLOWED_EYE_STATES)}",
            "_status": 400,
        }

    db = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved = db.resolve_correction(correction_id, eye_state, now)
    if resolved is None:
        return {"ok": False, "error": "correction not found", "_status": 404}

    ts = resolved["originalTimestamp"]
    entry_updates = {
        "eyeState": eye_state,
        "eyeStateEdited": True,
        "eyeStateCorrectedAt": now,
    }
    db.update_entry(ts, entry_updates)

    # Keep JSONL backup in sync with SQLite. Missing line is tolerated —
    # SQLite is authoritative; a JSONL row can be absent for older entries
    # that were only ever dual-written after migration.
    try:
        lines = JSONL_FILE.read_text().strip().splitlines()
        new_lines = []
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if entry.get("timestamp") == ts:
                entry.update(entry_updates)
            new_lines.append(json.dumps(entry))
        JSONL_FILE.write_text("\n".join(new_lines) + "\n")
    except FileNotFoundError:
        pass

    # Append to corrections.jsonl backup so the append-only log reflects
    # the resolution (the original phantom row is already in there with
    # nulls).
    with open(CORRECTIONS_FILE, "a") as f:
        f.write(json.dumps({
            "correctedAt": now,
            "originalTimestamp": ts,
            "correctedEyeState": eye_state,
            "source": "dashboard-resolve",
            "resolvedCorrectionId": correction_id,
        }) + "\n")

    return {"ok": True, "id": correction_id, "eyeState": eye_state}


def correction_discard(*, correction_id: int | None) -> dict:
    """Delete a phantom correction row outright. corrections.jsonl is
    append-only, so this does not rewrite it — SQLite is authoritative for
    training label reads."""
    if not isinstance(correction_id, int):
        return {"ok": False, "error": "id (int) required", "_status": 400}
    if not get_db().delete_correction(correction_id):
        return {"ok": False, "error": "correction not found", "_status": 404}
    return {"ok": True, "id": correction_id}
