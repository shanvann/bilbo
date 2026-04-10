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
_face_detector = None
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
    global _presence_clf, _eye_state_clf, _face_detector
    if _presence_clf is not None and _eye_state_clf is not None and _face_detector is not None:
        return _presence_clf, _eye_state_clf, _face_detector

    from .classifiers import BabyPresenceClassifier, EyeStateClassifier, FaceDetector

    device = "cpu"
    presence_path = PRESENCE_MODEL
    eye_path = EYE_STATE_MODEL

    log.info("%s: loading classifiers (device=%s)", BIRDEYE, device)
    t0 = time.monotonic()

    _presence_clf = BabyPresenceClassifier(presence_path, device)
    _eye_state_clf = EyeStateClassifier(eye_path, device)
    _face_detector = FaceDetector()

    log.info("%s: classifiers loaded in %.2fs", BIRDEYE, time.monotonic() - t0)
    return _presence_clf, _eye_state_clf, _face_detector


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def try_local_analysis(frame_path: Path) -> dict | None:
    """Run the three-stage pipeline on a captured frame.

    Stages: presence → face detection → eye state.
    Returns a flat entry dict when confident, or None to trigger cloud API fallback.
    """
    if not _check_available():
        return None

    try:
        presence_clf, eye_clf, face_detector = _get_classifiers()
    except Exception as e:
        log.error("%s: classifier load failed: %s", BIRDEYE, e)
        return None

    from .classifiers import crop_bassinet, crop_face
    from .config import EYE_STATE_CONFIDENCE_THRESHOLD
    import cv2

    timings = {}

    # --- Load frame ---
    t0 = time.monotonic()
    frame = cv2.imread(str(frame_path))
    if frame is None:
        log.error("%s: cannot read frame: %s", BIRDEYE, frame_path)
        return None
    timings["load"] = time.monotonic() - t0

    # --- Stage 1: baby presence (bassinet crop) ---
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

    # --- Stage 2: face detection (on bassinet crop) ---
    t_face = time.monotonic()
    face_result = face_detector.detect(bassinet_crop)
    timings["face_detect"] = time.monotonic() - t_face

    if face_result is None:
        timings["total"] = time.monotonic() - t0
        log.info("%s: FALLBACK no_face_detected (%.2fs) -> cloud API",
                 BIRDEYE, timings["total"])
        return None

    log.info("%s: face_detect %.3fs  conf=%.3f bbox=%s",
             BIRDEYE, timings["face_detect"], face_result.confidence,
             face_result.normalized_bbox)

    # --- Stage 3: eye state (face crop, 2-class) ---
    t2 = time.monotonic()
    face_crop = crop_face(bassinet_crop, face_result.bbox)
    eye_result = eye_clf.classify(face_crop)
    timings["eye_state"] = time.monotonic() - t2

    log.info("%s: eye_state %.3fs  state=%s conf=%.3f probs=%s (face crop)",
             BIRDEYE, timings["eye_state"], eye_result.state, eye_result.confidence,
             eye_result.probabilities)

    timings["total"] = time.monotonic() - t0

    # Low confidence → fall back to cloud API
    if eye_result.confidence < EYE_STATE_CONFIDENCE_THRESHOLD:
        log.info("%s: FALLBACK low_confidence %.3f < %.3f (%.2fs) -> cloud API",
                 BIRDEYE, eye_result.confidence, EYE_STATE_CONFIDENCE_THRESHOLD,
                 timings["total"])
        return None

    state = "Awake" if eye_result.state == "eyes_open" else "Asleep"
    log.info("%s: RESULT %s conf=%.3f (%.2fs) -> cloud API skipped",
             BIRDEYE, state, eye_result.confidence, timings["total"])
    return _build_entry(state, presence, eye_result, timings,
                        face_bbox=face_result.normalized_bbox,
                        face_confidence=face_result.confidence)


# ---------------------------------------------------------------------------
# Entry builder
# ---------------------------------------------------------------------------

def _build_entry(state, presence, eye_result, timings,
                  face_bbox=None, face_confidence=None):
    """Build a flat entry dict compatible with the existing sleep-log schema."""
    baby_present = state != "not_present"

    entry = {
        "babyPresent": baby_present,
        "state": state,
        "detectionMethod": BIRDEYE,
        "modelUsed": "local/mobilenet+yunet+mobilenet",
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

    # Face detection metadata
    if face_bbox is not None:
        entry["faceBbox"] = face_bbox
        entry["faceConfidence"] = round(face_confidence, 3)

    entry["birdeyeTimings"] = {k: round(v, 3) for k, v in timings.items()}

    return entry
