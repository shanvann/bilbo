"""Single-frame BIRDEYE re-inference, invoked from the dashboard.

For now this shells out to `python -m bilbo.scripts.run_single_inference <ts>`
because the dashboard venv intentionally has no torch/cv2. Step 8 will
replace the subprocess hop with a POST to capture:5557/infer.
"""
from __future__ import annotations

import json
import subprocess
import sys


_INFERENCE_TIMEOUT_SEC = 30


def run_single(timestamp: str) -> dict:
    """Re-run BIRDEYE on the frame for `timestamp`. Returns the parsed
    JSON dict the script prints on stdout, or a dict with `ok=False`
    and an `error` key on failure.
    """
    if not timestamp:
        return {"ok": False, "error": "timestamp required"}

    cmd = [sys.executable, "-m", "bilbo.scripts.run_single_inference", timestamp]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_INFERENCE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "inference timed out", "_status": 504}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200], "_status": 500}

    if result.returncode != 0:
        return {
            "ok": False,
            "error": (result.stderr or "").strip()[:200],
            "_status": 500,
        }
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": str(e)[:200], "_status": 500}
