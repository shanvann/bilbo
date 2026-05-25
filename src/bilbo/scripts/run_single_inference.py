#!/usr/bin/env python3
"""Re-run BIRDEYE inference on a single frame by timestamp.

Called by the dashboard's /api/run-inference endpoint via subprocess
(the dashboard venv intentionally has no torch/cv2). Updates the
entry's `shadow` audit blob and indexed shadow_birdeye_* columns
with the new model output. Leaves the user-facing primary fields
(eye_state, baby_present) untouched so corrections survive a re-run.

This script is a thin wrapper. All the actual work — running BIRDEYE,
mapping results to the shadow blob shape — lives in
`lib.local_pipeline.run_birdeye_inference` and `birdeye_result_to_shadow_blob`,
which `monitor.py` (the live capture pipeline) also calls. Keep this
file thin so the two paths cannot drift.

Usage: python run_single_inference.py <timestamp>
Output: JSON on stdout
"""

import json
import sys
from pathlib import Path

from bilbo.storage.db import get_db
from bilbo.pipeline.local_pipeline import (
    birdeye_result_to_shadow_blob,
    run_birdeye_inference,
)


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "timestamp required"}))
        return 1

    ts = sys.argv[1]
    db = get_db()
    entries = db.get_entries(start=ts, end=ts)
    if not entries:
        print(json.dumps({"ok": False, "error": "entry not found"}))
        return 1

    entry = entries[0]
    frame_path = entry.get("frame", "")
    if not frame_path or not Path(frame_path).exists():
        print(json.dumps({"ok": False, "error": "frame file not found"}))
        return 1

    result = run_birdeye_inference(Path(frame_path))
    if result is None:
        print(json.dumps({"ok": True, "result": None, "reason": "hard_error"}))
        return 0

    shadow = birdeye_result_to_shadow_blob(result)

    # Compute retrainAgreed against the user's correction (if any).
    # This is the only thing this script does that monitor.py doesn't —
    # the dashboard cares whether the *current* model now agrees with
    # what a human corrected to, separate from what the model said at
    # capture time.
    retrain_agreed = None
    gt_eye = entry.get("eyeState")
    if entry.get("eyeStateEdited") and gt_eye:
        bird_eye = result.get("eyeState")
        if gt_eye == "not_in_bassinet":
            retrain_agreed = not result.get("babyPresent", True)
        elif bird_eye:
            retrain_agreed = (bird_eye == gt_eye)

    updates = {"shadow": shadow}
    if result.get("shadowModelVersion"):
        updates["shadowModelVersion"] = result["shadowModelVersion"]
    if result.get("faceBbox"):
        updates["faceBbox"] = result["faceBbox"]
    if result.get("faceConfidence") is not None:
        updates["faceConfidence"] = result["faceConfidence"]
    if retrain_agreed is not None:
        updates["retrainAgreed"] = retrain_agreed

    db.update_entry(ts, updates)

    print(json.dumps({
        "ok": True,
        "shadow": shadow,
        "faceBbox": result.get("faceBbox"),
        "faceConfidence": result.get("faceConfidence"),
        "retrainAgreed": retrain_agreed,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
