"""Training state management — PID-based, works across CLI/dashboard/cron.

State file: data/training-state.json
{
    "status": "running" | "completed" | "failed" | "aborted" | "idle",
    "pid": 12345,
    "trigger": "dashboard" | "cli" | "scheduled",
    "startedAt": "2026-04-08T23:42:50Z",
    "finishedAt": "2026-04-08T23:57:14Z",
    "exitCode": 0
}

Any process can check if training is running by reading the PID and
checking if it's alive. No in-memory state needed.
"""

import json
import logging
import os
import signal
from datetime import datetime, timezone

from .config import TRAINING_STATE_FILE

log = logging.getLogger("monitor")


def _read() -> dict:
    if TRAINING_STATE_FILE.exists():
        try:
            return json.loads(TRAINING_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(state: dict):
    TRAINING_STATE_FILE.write_text(json.dumps(state, indent=2))


def _pid_alive(pid: int) -> bool:
    """Check if a process with given PID is still running (not zombie)."""
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False

    # Check for zombie — kill(0) succeeds on zombies but they're not running
    try:
        result = os.waitpid(pid, os.WNOHANG)
        if result[0] != 0:
            # Process was reaped — it was a zombie
            return False
    except ChildProcessError:
        # Not our child — check /proc or ps
        try:
            import subprocess
            out = subprocess.check_output(["ps", "-p", str(pid), "-o", "stat="],
                                          stderr=subprocess.DEVNULL, text=True).strip()
            if out.startswith("Z"):
                return False  # zombie
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    return True


def is_running() -> bool:
    """Check if a training process is currently running (by PID)."""
    state = _read()
    if state.get("status") != "running":
        return False
    pid = state.get("pid")
    if not pid:
        return False
    if _pid_alive(pid):
        return True
    # PID is dead but status says running — stale, clean up
    state["status"] = "failed"
    state["finishedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["exitCode"] = -1
    _write(state)
    return False


def get_status() -> dict:
    """Get current training state, cleaning up stale PIDs."""
    state = _read()
    # If status says running, verify the PID is actually alive
    if state.get("status") == "running":
        pid = state.get("pid")
        if not pid or not _pid_alive(pid):
            state["status"] = "failed"
            state["finishedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            state["exitCode"] = -1
            _write(state)
    return state


def mark_started(trigger: str = "cli"):
    """Mark training as started with the current process PID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {
        "status": "running",
        "pid": os.getpid(),
        "trigger": trigger,
        "startedAt": now,
        "finishedAt": None,
        "exitCode": None,
    }
    _write(state)
    log.info("training: started (pid=%d, trigger=%s)", os.getpid(), trigger)


def mark_subprocess_started(pid: int, trigger: str = "dashboard"):
    """Mark training as started with a subprocess PID (for dashboard)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {
        "status": "running",
        "pid": pid,
        "trigger": trigger,
        "startedAt": now,
        "finishedAt": None,
        "exitCode": None,
    }
    _write(state)
    log.info("training: subprocess started (pid=%d, trigger=%s)", pid, trigger)


def mark_completed(exit_code: int = 0):
    """Mark training as completed."""
    state = _read()
    state["status"] = "completed" if exit_code == 0 else "failed"
    state["finishedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["exitCode"] = exit_code
    _write(state)
    log.info("training: %s (exit_code=%d)", state["status"], exit_code)


def abort() -> bool:
    """Abort a running training process. Returns True if killed."""
    state = _read()
    pid = state.get("pid")
    if not pid or state.get("status") != "running":
        return False

    if not _pid_alive(pid):
        # Already dead
        state["status"] = "failed"
        state["finishedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["exitCode"] = -1
        _write(state)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        # Give it a moment
        import time
        for _ in range(10):
            time.sleep(1)
            if not _pid_alive(pid):
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass

    state["status"] = "aborted"
    state["finishedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["exitCode"] = -15
    _write(state)
    log.info("training: aborted (pid=%d)", pid)
    return True
