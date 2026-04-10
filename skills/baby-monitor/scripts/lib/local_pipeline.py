"""BIRDEYE — local two-classifier pipeline for baby sleep/wake detection.

    Baby
    IR-aware
    Recognition &
    Detection of
    EYE-state

Two MobileNetV3-Small classifiers (no YOLO, no bounding boxes):

  Classifier 1: fixed bassinet crop → baby present / not_present
  Classifier 2: head-region crop   → eyes_open / eyes_closed / face_not_visible

Head position is adaptive: when birdeye falls back to the cloud API,
the API returns the head's approximate location, which is persisted to
data/head-state.json and used to position the head crop on the next tick.

Fallback triggers (returns None → cloud API):
  - face_not_visible (baby turned face-down, head rotated)
  - Pipeline dependencies not installed
  - Model files missing or load error
  - Any runtime error during inference
"""

import logging
import os
import sys
import time
from pathlib import Path

from .config import (
    PRESENCE_MODEL,
    EYE_STATE_MODEL,
)

log = logging.getLogger("monitor")

# Lazy-loaded singletons
_presence_clf = None
_eye_state_clf = None
_available = None  # None = not yet checked

BIRDEYE = "birdeye"


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_available() -> bool:
    global _available
    if _available is not None:
        return _available

    missing = []
    for mod in ("cv2", "torch", "torchvision"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        log.warning("%s: disabled — missing deps: %s", BIRDEYE, ", ".join(missing))
        _available = False
        return False

    _available = True
    log.info("%s: deps OK, classifiers available", BIRDEYE)
    return True


# ---------------------------------------------------------------------------
# Classifier loading (lazy singletons)
# ---------------------------------------------------------------------------

def _get_classifiers():
    global _presence_clf, _eye_state_clf
    if _presence_clf is not None and _eye_state_clf is not None:
        return _presence_clf, _eye_state_clf

    from .classifiers import BabyPresenceClassifier, EyeStateClassifier

    device = "cpu"
    presence_path = PRESENCE_MODEL
    eye_path = EYE_STATE_MODEL

    log.info("%s: loading classifiers (device=%s)", BIRDEYE, device)
    t0 = time.monotonic()

    _presence_clf = BabyPresenceClassifier(presence_path, device)
    _eye_state_clf = EyeStateClassifier(eye_path, device)

    log.info("%s: classifiers loaded in %.2fs", BIRDEYE, time.monotonic() - t0)
    return _presence_clf, _eye_state_clf


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def try_local_analysis(frame_path: Path) -> dict | None:
    """Run the two-classifier pipeline on a captured frame.

    Returns a flat entry dict when confident (present/not_present/awake/asleep),
    or None to trigger cloud API fallback.
    """
    if not _check_available():
        return None

    try:
        presence_clf, eye_clf = _get_classifiers()
    except Exception as e:
        log.error("%s: classifier load failed: %s", BIRDEYE, e)
        return None

    from .classifiers import crop_bassinet
    import cv2

    timings = {}

    # --- Load frame ---
    t0 = time.monotonic()
    frame = cv2.imread(str(frame_path))
    if frame is None:
        log.error("%s: cannot read frame: %s", BIRDEYE, frame_path)
        return None
    timings["load"] = time.monotonic() - t0

    # --- Classifier 1: baby presence ---
    t1 = time.monotonic()
    bassinet_crop = crop_bassinet(frame)
    presence = presence_clf.classify(bassinet_crop)
    timings["presence"] = time.monotonic() - t1

    log.info("%s: presence  %.3fs  present=%s conf=%.3f probs=%s",
             BIRDEYE, timings["presence"], presence.present, presence.confidence,
             presence.probabilities)

    if not presence.present:
        timings["total"] = time.monotonic() - t0
        log.info("%s: RESULT not_present conf=%.3f (%.2fs) -> cloud API skipped",
                 BIRDEYE, presence.confidence, timings["total"])
        return _build_entry("not_present", presence, None, timings)

    # --- Classifier 2: eye state (bassinet crop) ---
    # Load head position for recording in entry (not used for cropping)
    from .classifiers import load_head_state
    head_pos = load_head_state()

    t2 = time.monotonic()
    bass_crop = crop_bassinet(frame)
    eye_result = eye_clf.classify(bass_crop)
    timings["eye_state"] = time.monotonic() - t2

    log.info("%s: eye_state %.3fs  state=%s conf=%.3f probs=%s (bassinet crop)",
             BIRDEYE, timings["eye_state"], eye_result.state, eye_result.confidence,
             eye_result.probabilities)

    timings["total"] = time.monotonic() - t0

    if eye_result.state == "eyes_open":
        log.info("%s: RESULT Awake conf=%.3f (%.2fs) -> cloud API skipped",
                 BIRDEYE, eye_result.confidence, timings["total"])
        return _build_entry("Awake", presence, eye_result, timings, head_pos)

    if eye_result.state == "eyes_closed":
        log.info("%s: RESULT Asleep conf=%.3f (%.2fs) -> cloud API skipped",
                 BIRDEYE, eye_result.confidence, timings["total"])
        return _build_entry("Asleep", presence, eye_result, timings, head_pos)

    # face_not_visible → fall back to cloud API (which will also update head position)
    log.info("%s: FALLBACK face_not_visible conf=%.3f (%.2fs) -> cloud API",
             BIRDEYE, eye_result.confidence, timings["total"])
    return None


# ---------------------------------------------------------------------------
# Entry builder
# ---------------------------------------------------------------------------

def _build_entry(state, presence, eye_result, timings, head_pos=None):
    """Build a flat entry dict compatible with the existing sleep-log schema."""
    baby_present = state != "not_present"

    entry = {
        "babyPresent": baby_present,
        "state": state,
        "detectionMethod": BIRDEYE,
        "modelUsed": "local/mobilenet+mobilenet",
        # Fields birdeye doesn't produce
        "sleepPosition": "Unknown",
        "objectsInBassinet": "Unknown",
        "swaddle": "Unknown",
        "headCovering": "Unknown",
        "lighting": "Unknown",
        "bodyPosture": "Unknown",
        "pacifierEngaged": "Unknown",
        "bassinetCondition": "Unknown",
        "hazards": "Unknown",
        "bassinetLocation": "Unknown",
        "captureMode": "Unknown",
        "cameraTimestamp": None,
    }

    # Presence classifier metadata
    entry["presenceConfidence"] = round(presence.confidence, 3)

    # Eye state metadata (when baby present and classifier ran)
    if eye_result is not None:
        entry["eyeState"] = eye_result.state
        entry["eyeConfidence"] = round(eye_result.confidence, 3)

    if head_pos is not None:
        entry["headPosition"] = {"x": round(head_pos["x"], 4), "y": round(head_pos["y"], 4)}

    entry["birdeyeTimings"] = {k: round(v, 3) for k, v in timings.items()}

    return entry
