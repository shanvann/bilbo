"""Capture container's main process.

A Flask service on :5557 that:

1. Runs `bilbo.monitor.main()` with --loop in a background thread (the
   per-minute RTSP capture + BIRDEYE + alerts pipeline).
2. Runs `bilbo.watchdog.run_loop()` in another background thread (the
   stale-DB-entry Telegram alert).
3. Exposes POST /infer for ad-hoc inference. control-api forwards the
   dashboard's /api/run-inference button here so the request reuses the
   warm torch model instead of cold-loading in a subprocess.
4. Exposes GET /healthz for the container orchestrator.

The :5557 listener is intentionally internal to the compose network — it
is NOT published to the host. Only control-api talks to it.
"""
from __future__ import annotations

import logging
import sys
from threading import Thread

from flask import Flask, jsonify, request

from bilbo import watchdog
from bilbo.scripts import run_single_inference

app = Flask(__name__)
log = logging.getLogger("capture-service")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/infer")
def infer():
    body = request.get_json(silent=True) or {}
    timestamp = body.get("timestamp")
    if not timestamp:
        return jsonify({"ok": False, "error": "timestamp required"}), 400
    try:
        result = run_single_inference.run(timestamp)
    except Exception as e:  # noqa: BLE001
        log.exception("infer: failed for ts=%s", timestamp)
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    status = 200 if result.get("ok") else 404 if result.get("error") == "entry not found" else 500
    return jsonify(result), status


def _start_monitor_thread() -> Thread:
    """Run `bilbo.monitor.main` with --loop in a background thread.

    Re-imports inside the thread so the heavy torch/cv2 imports don't slow
    down Flask startup; the singletons in bilbo.pipeline.local_pipeline are
    process-wide and shared with the /infer handler.
    """
    def _run():
        # Inject --loop into argv so monitor.main()'s argparse picks it up.
        # KeyboardInterrupt + system exits inside the loop are swallowed by
        # monitor's own except handlers; an unexpected exception escaping all
        # the way back here is logged but does NOT kill the Flask process —
        # the watchdog will alert if capture stops producing entries.
        sys.argv = ["bilbo-monitor", "--loop"]
        from bilbo import monitor
        try:
            monitor.main()
        except SystemExit:
            pass
        except Exception:
            log.exception("monitor loop crashed; capture container is "
                          "still alive but not producing frames — watchdog "
                          "will alert")
    t = Thread(target=_run, name="bilbo-monitor", daemon=True)
    t.start()
    return t


def _start_watchdog_thread(interval: int = 120) -> Thread:
    def _run():
        try:
            watchdog.run_loop(interval=interval)
        except Exception:
            log.exception("watchdog loop crashed")
    t = Thread(target=_run, name="bilbo-watchdog", daemon=True)
    t.start()
    return t


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)sZ] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log.info("capture-service: starting monitor + watchdog threads")
    _start_monitor_thread()
    _start_watchdog_thread()
    log.info("capture-service: listening on :5557 (POST /infer, GET /healthz)")
    app.run(host="0.0.0.0", port=5557, debug=False)


if __name__ == "__main__":
    main()
