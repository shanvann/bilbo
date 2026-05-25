"""Training-run state: status query, start (subprocess), abort.

Keeps the existing PID-based behavior from `bilbo.training_state`. Step 9
will swap the subprocess hop for a Docker control-plane RPC, at which point
the function signatures here stay the same and only the body of `retrain`
changes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from bilbo.config import DATA_DIR, MODELS_DIR
from bilbo.storage.db import get_db
from bilbo.training_state import (
    abort as _abort,
    get_status as _get_status,
    is_running as _is_running,
    mark_completed,
    mark_subprocess_started,
)


_TRAINING_LOG = MODELS_DIR / "training-log.jsonl"


def _last_training_logs(n: int = 2) -> list[dict]:
    """Fallback to the file-backed log if the SQLite training_runs table
    hasn't been populated yet (eg. on a fresh DB after migration)."""
    if not _TRAINING_LOG.exists():
        return []
    lines = _TRAINING_LOG.read_text().strip().splitlines()
    if not lines:
        return []
    out = []
    for line in reversed(lines[-n:]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def training_status() -> dict:
    """Current run state + last/prev completed training summaries.

    Shape matches the previous /api/training-status response so the
    dashboard's app.js can stay untouched.
    """
    db = get_db()
    state = _get_status()  # PID-checked, auto-cleans stale

    logs = db.get_last_training_runs(2)
    if not logs:
        logs = _last_training_logs(2)
    last_log = logs[0] if logs else None
    prev_log = logs[1] if len(logs) > 1 else None

    last_trained_ts = last_log.get("timestamp") if last_log else None
    total_corrections, pending_corrections = db.get_pending_corrections_count(last_trained_ts)
    duration_stats = db.get_training_duration_stats()
    last_trained_per_classifier = db.get_last_trained_per_classifier()

    return {
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
    }


def retrain(*, trigger: str = "dashboard", skip_face_detect: bool = False) -> dict:
    """Spawn a retrain subprocess (the same `monitor.py --retrain` command
    the CLI uses) and register its PID.

    Returns {ok, pid, trigger} on success, {ok: False, error, _status} on
    failure (eg. another run is already in progress).
    """
    if _is_running():
        return {"ok": False, "error": "Training already in progress", "_status": 409}

    retrain_stdout = open(DATA_DIR / "retrain-dashboard-stdout.log", "w")
    retrain_stderr = open(DATA_DIR / "retrain-dashboard-stderr.log", "w")
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    cmd = [sys.executable, "-u", "-m", "bilbo.monitor", "--retrain"]
    if skip_face_detect:
        cmd.append("--skip-face-detect")
    proc = subprocess.Popen(
        cmd,
        stdout=retrain_stdout,
        stderr=retrain_stderr,
        env=env,
        start_new_session=True,  # survive dashboard restarts
    )
    mark_subprocess_started(proc.pid, trigger)

    # Reap the child in the background so the state file flips to
    # completed/failed once the run is done.
    def _reap():
        proc.wait()
        retrain_stdout.close()
        retrain_stderr.close()
        mark_completed(proc.returncode)

    threading.Thread(target=_reap, daemon=True).start()

    return {"ok": True, "pid": proc.pid, "trigger": trigger}


def retrain_abort() -> dict:
    """Send SIGTERM (then SIGKILL after a grace period) to the running
    training subprocess. Returns {ok, status} or 404 if nothing's running.
    """
    if not _is_running():
        return {"ok": False, "error": "No training in progress", "_status": 404}
    killed = _abort()
    return {"ok": killed, "status": "aborted" if killed else "not found"}
