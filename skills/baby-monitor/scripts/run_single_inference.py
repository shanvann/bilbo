#!/usr/bin/env python3
"""Run BIRDEYE inference on a single frame by timestamp.

Called by the dashboard's /api/run-inference endpoint via subprocess
(the dashboard venv doesn't have torch/cv2).

Usage: python run_single_inference.py <timestamp>
Output: JSON on stdout
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.db import get_db
from lib.local_pipeline import try_local_analysis
import lib.local_pipeline as _lp


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

    # Force reload classifiers to pick up latest model
    _lp._presence_clf = None
    _lp._eye_state_clf = None
    _lp._face_detector = None
    _lp._face_detector_fallback = None
    _lp._available = None

    result = try_local_analysis(Path(frame_path))

    if result is None:
        print(json.dumps({"ok": True, "result": None, "reason": "hard_error"}))
        return 0

    fallback = result.get("fallback")
    birdeye_state = result.get("state", "Unknown")
    if not result.get("babyPresent", False):
        birdeye_state = "not_present"

    # Build shadow dict
    prod_state = entry.get("state", "Unknown")
    if not entry.get("babyPresent", False):
        prod_state = "not_present"
    agreed = birdeye_state.lower() == prod_state.lower()

    shadow = {
        "birdeyeState": birdeye_state,
        "prodState": prod_state,
        "agreed": agreed,
        "presenceConfidence": result.get("presenceConfidence"),
        "eyeConfidence": result.get("eyeConfidence"),
        "eyeState": result.get("eyeState"),
        "birdeyeTimings": result.get("birdeyeTimings"),
        "fallback": fallback,
    }

    # Check if correction exists and whether inference now agrees
    gt_eye = entry.get("eyeState")
    retrain_agreed = None
    if entry.get("eyeStateEdited") and gt_eye:
        bird_eye = result.get("eyeState")
        if gt_eye == "not_in_bassinet":
            retrain_agreed = not result.get("babyPresent", True)
        elif bird_eye:
            retrain_agreed = (bird_eye == gt_eye)

    # Update entry in DB
    updates = {"shadow": shadow}
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
