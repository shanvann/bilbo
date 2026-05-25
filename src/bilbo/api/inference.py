"""Single-frame BIRDEYE re-inference, invoked from the dashboard.

control-api (which imports this module) does not have torch/cv2 in its
container — so we POST to the capture container's warm-torch /infer
endpoint instead of spawning a subprocess. BILBO_CAPTURE_URL defaults to
the compose-network DNS name; override for local-dev / host runs.
"""
from __future__ import annotations

import os

import httpx


CAPTURE_URL = os.environ.get("BILBO_CAPTURE_URL", "http://capture:5557")
_INFERENCE_TIMEOUT_SEC = 30


def run_single(timestamp: str) -> dict:
    """POST /infer to the capture container. Returns the JSON dict it
    produces, or a dict with `ok=False` + `_status` on transport failure.
    """
    if not timestamp:
        return {"ok": False, "error": "timestamp required", "_status": 400}

    try:
        r = httpx.post(
            f"{CAPTURE_URL}/infer",
            json={"timestamp": timestamp},
            timeout=_INFERENCE_TIMEOUT_SEC,
        )
    except httpx.TimeoutException:
        return {"ok": False, "error": "inference timed out", "_status": 504}
    except httpx.RequestError as e:
        return {
            "ok": False,
            "error": f"capture container unreachable: {e}",
            "_status": 502,
        }

    try:
        payload = r.json()
    except ValueError:
        return {"ok": False, "error": "non-JSON response from capture", "_status": 502}

    if r.status_code >= 400 and "_status" not in payload:
        payload["_status"] = r.status_code
    return payload
