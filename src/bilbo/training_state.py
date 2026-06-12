"""Training-run state coordination.

Source of truth: a Docker container named `bilbo-training`. The dashboard's
"Retrain" button (via control-api) spawns it via the Docker socket; this
module wraps the four operations the rest of the codebase needs:

  is_running()      → bool
  start(args, ...)  → container ID
  abort()           → bool (True if a running container was stopped)
  get_status()      → dict (running flag + last completion metadata)

A small JSON state file (`data/training-state.json`) caches the last
completion summary — the container itself disappears after `auto_remove`,
so without this file get_status() would forget the previous run's exit
code, trigger, and timestamps the moment it finishes.

Legacy in-process paths (cli.py's `bilbo-monitor --retrain` running on a
host without Docker) keep the same state file plus a PID, so the
dashboard's training-status panel renders correctly in both modes. The
docker-vs-pid distinction is invisible to the caller.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime, timezone

from bilbo.config import DATA_DIR, TRAINING_STATE_FILE

log = logging.getLogger("monitor")

CONTAINER_NAME = "bilbo-training"
IMAGE = os.environ.get("BILBO_TRAINING_IMAGE", "bilbo:latest")


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> dict:
    if TRAINING_STATE_FILE.exists():
        try:
            return json.loads(TRAINING_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(state: dict) -> None:
    TRAINING_STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Docker (lazy import so host-dev without `docker` installed still works)
# ---------------------------------------------------------------------------

def _docker_client():
    try:
        import docker  # noqa: WPS433
    except ImportError:
        return None
    try:
        return docker.from_env()
    except Exception:  # noqa: BLE001
        # Docker SDK installed but the daemon socket isn't mounted /
        # reachable (typical on host-dev or in the dashboard image).
        return None


def _container():
    """Return the bilbo-training container if it exists, else None."""
    client = _docker_client()
    if client is None:
        return None
    try:
        return client.containers.get(CONTAINER_NAME)
    except Exception:  # noqa: BLE001  — docker.errors.NotFound + transport errors
        return None


# ---------------------------------------------------------------------------
# Legacy PID helper (for in-process cli.py runs)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_running() -> bool:
    """True if a training run is in progress in EITHER mode (container or PID)."""
    c = _container()
    if c is not None and getattr(c, "status", "") == "running":
        return True

    state = _read()
    if state.get("status") != "running":
        return False
    pid = state.get("pid")
    if pid and _pid_alive(int(pid)):
        return True

    # state says running but neither container nor PID is alive — clean up
    state["status"] = "failed"
    state["finishedAt"] = _now()
    state.setdefault("exitCode", -1)
    _write(state)
    return False


def get_status() -> dict:
    """Combine live container/PID state with the persisted last-completion
    summary so the dashboard panel renders consistently across runs."""
    state = _read()

    c = _container()
    if c is not None and getattr(c, "status", "") == "running":
        # Container is the authoritative source while it's alive.
        attrs = c.attrs if hasattr(c, "attrs") else {}
        return {
            "status": "running",
            "containerId": c.id,
            "pid": None,
            "trigger": state.get("trigger", "dashboard"),
            "startedAt": attrs.get("State", {}).get("StartedAt") or state.get("startedAt"),
            "finishedAt": None,
            "exitCode": None,
        }

    # No live container — fall through to PID check / persisted state.
    if state.get("status") == "running":
        pid = state.get("pid")
        if not pid or not _pid_alive(int(pid)):
            state["status"] = "failed"
            state["finishedAt"] = _now()
            state.setdefault("exitCode", -1)
            _write(state)
    return state


def start(args: list[str] | None = None, *, trigger: str = "dashboard") -> dict:
    """Spawn the bilbo-training container.

    `args` is appended after `bilbo-train`. Returns `{"ok": True, "containerId": ...}`
    on success or `{"ok": False, "error": ..., "_status": ...}` on failure.
    Caller is responsible for the "already running" precondition check via
    `is_running()` first (control-api does this).
    """
    client = _docker_client()
    if client is None:
        return {
            "ok": False,
            "error": "Docker socket not reachable from this container",
            "_status": 503,
        }

    host_data = os.environ.get("BILBO_HOST_DATA")
    host_models = os.environ.get("BILBO_HOST_MODELS")
    if not host_data or not host_models:
        return {
            "ok": False,
            "error": (
                "BILBO_HOST_DATA / BILBO_HOST_MODELS not set — "
                "control-api needs the host paths to bind-mount into "
                "the training container"
            ),
            "_status": 500,
        }

    volumes = {
        host_data:   {"bind": "/app/data", "mode": "rw"},
        host_models: {"bind": "/app/pipeline/models", "mode": "rw"},
    }
    env = {k: v for k, v in os.environ.items() if k.startswith(("OPENAI_", "BILBO_", "TELEGRAM_", "RTSP_"))}
    # The training container picks up the host env file from its bind-mounted
    # data/ — but the secrets are also fine to pass via env directly.
    # DATABASE_URL doesn't match the prefixes above, but the training run
    # writes its training_runs row to Postgres, so pass it through explicitly.
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        env["DATABASE_URL"] = db_url

    command = ["bilbo-train", *(args or [])]
    # Join the same Docker network as control-api so the `postgres` hostname in
    # DATABASE_URL resolves (the container is spawned outside compose, on the
    # default bridge, unless we attach it explicitly).
    run_kwargs = dict(
        command=command,
        name=CONTAINER_NAME,
        volumes=volumes,
        environment=env,
        detach=True,
        auto_remove=True,
    )
    network = os.environ.get("BILBO_TRAINING_NETWORK")
    if network:
        run_kwargs["network"] = network
    try:
        container = client.containers.run(IMAGE, **run_kwargs)
    except Exception as e:  # noqa: BLE001 — docker.errors plus transport
        return {
            "ok": False,
            "error": f"docker run failed: {type(e).__name__}: {e}",
            "_status": 500,
        }

    state = {
        "status": "running",
        "containerId": container.id,
        "pid": None,
        "trigger": trigger,
        "startedAt": _now(),
        "finishedAt": None,
        "exitCode": None,
    }
    _write(state)
    log.info("training: docker container %s started (trigger=%s)", container.id[:12], trigger)
    return {"ok": True, "containerId": container.id, "trigger": trigger}


def abort() -> bool:
    """Stop a running training run (container or PID). True if something was killed."""
    c = _container()
    if c is not None and getattr(c, "status", "") == "running":
        try:
            c.stop(timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("training: docker stop raised %s", e)
        state = _read()
        state["status"] = "aborted"
        state["finishedAt"] = _now()
        state["exitCode"] = -15
        _write(state)
        return True

    state = _read()
    pid = state.get("pid")
    if pid and _pid_alive(int(pid)):
        try:
            os.kill(int(pid), signal.SIGTERM)
            for _ in range(10):
                time.sleep(1)
                if not _pid_alive(int(pid)):
                    break
            else:
                os.kill(int(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        state["status"] = "aborted"
        state["finishedAt"] = _now()
        state["exitCode"] = -15
        _write(state)
        return True

    return False


# ---------------------------------------------------------------------------
# Legacy in-process helpers (used by cli.py when bilbo-monitor --retrain runs
# directly on the host without Docker). They write the same state file so
# `is_running()` / `get_status()` work uniformly across both modes.
# ---------------------------------------------------------------------------

def mark_started(trigger: str = "cli") -> None:
    state = {
        "status": "running",
        "pid": os.getpid(),
        "trigger": trigger,
        "startedAt": _now(),
        "finishedAt": None,
        "exitCode": None,
    }
    _write(state)
    log.info("training: in-process started (pid=%d, trigger=%s)", os.getpid(), trigger)


def mark_subprocess_started(pid: int, trigger: str = "dashboard") -> None:
    """Deprecated: pre-Docker dashboard flow recorded a subprocess PID here.

    The Docker `start()` path supersedes this. Kept for the host-dev case
    where someone shells out to `python -m bilbo.monitor --retrain`
    explicitly.
    """
    state = {
        "status": "running",
        "pid": pid,
        "trigger": trigger,
        "startedAt": _now(),
        "finishedAt": None,
        "exitCode": None,
    }
    _write(state)
    log.info("training: subprocess started (pid=%d, trigger=%s)", pid, trigger)


def mark_completed(exit_code: int = 0) -> None:
    state = _read()
    state["status"] = "completed" if exit_code == 0 else "failed"
    state["finishedAt"] = _now()
    state["exitCode"] = exit_code
    _write(state)
    log.info("training: %s (exit_code=%d)", state["status"], exit_code)
