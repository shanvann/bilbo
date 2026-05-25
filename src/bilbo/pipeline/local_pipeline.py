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

Single source of truth for callers
----------------------------------

`run_birdeye_inference(frame_path)` and `birdeye_result_to_shadow_blob`
are the public entry points used by both `monitor.py` (live capture
pipeline) and `run_single_inference.py` (dashboard re-run button).
Anything that wants "what does BIRDEYE produce for this frame" must
go through these so the two paths cannot drift.
"""

import logging
import os
import sys
import time
from pathlib import Path

from bilbo.config import (
    MODELS_DIR,
    PRESENCE_MODEL,
    EYE_STATE_MODEL,
    EYE_STATE_INPUT_SIZE,
    FACE_DETECT_MODEL_PT,
    FACE_DETECT_PT_CONFIDENCE_THRESHOLD,
)

log = logging.getLogger("monitor")

# Lazy-loaded singletons
_presence_clf = None
_eye_state_clf = None
_face_detector = None      # primary (trainable or YuNet)
_face_detector_fallback = None  # YuNet fallback when trainable is primary
_available = None  # None = not yet checked

# Path the singletons were loaded against — used by maybe_reload_classifiers()
# to detect when a retrain has flipped the `latest` symlink to a new version.
_loaded_model_version: str | None = None

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
    global _presence_clf, _eye_state_clf, _face_detector, _face_detector_fallback
    if _presence_clf is not None and _eye_state_clf is not None and _face_detector is not None:
        return _presence_clf, _eye_state_clf, _face_detector, _face_detector_fallback

    from bilbo.pipeline.classifiers import BabyPresenceClassifier, EyeStateClassifier, FaceDetector, TrainableFaceDetector

    device = "cpu"
    presence_path = PRESENCE_MODEL
    eye_path = EYE_STATE_MODEL

    log.info("%s: loading classifiers (device=%s)", BIRDEYE, device)
    t0 = time.monotonic()

    _presence_clf = BabyPresenceClassifier(presence_path, device)
    _eye_state_clf = EyeStateClassifier(eye_path, device)

    # Face detector: try trainable model first, fall back to YuNet
    try:
        _face_detector = TrainableFaceDetector(
            FACE_DETECT_MODEL_PT, device,
            confidence_threshold=FACE_DETECT_PT_CONFIDENCE_THRESHOLD,
        )
        _face_detector_fallback = FaceDetector()
        log.info("%s: using trainable face detector (YuNet as fallback)", BIRDEYE)
    except (FileNotFoundError, Exception) as e:
        log.info("%s: trainable face detector not available (%s), using YuNet", BIRDEYE, e)
        _face_detector = FaceDetector()
        _face_detector_fallback = None

    log.info("%s: classifiers loaded in %.2fs", BIRDEYE, time.monotonic() - t0)

    global _loaded_model_version
    _loaded_model_version = _read_deployed_model_version()

    return _presence_clf, _eye_state_clf, _face_detector, _face_detector_fallback


def maybe_reload_classifiers() -> bool:
    """Drop cached classifier singletons if `pipeline/models/latest` flipped.

    Called once per tick by the capture loop so a retrain (which writes a new
    versioned dir and swaps the symlink) takes effect within the next minute
    without restarting the container. Returns True if a reload was triggered.
    """
    global _presence_clf, _eye_state_clf, _face_detector, _face_detector_fallback, _loaded_model_version
    if _loaded_model_version is None:
        # Classifiers haven't been loaded yet (lazy); nothing to reload.
        return False
    current = _read_deployed_model_version()
    if current == _loaded_model_version:
        return False
    log.info(
        "%s: model symlink flipped (%s -> %s); dropping classifier singletons",
        BIRDEYE, _loaded_model_version, current,
    )
    _presence_clf = None
    _eye_state_clf = None
    _face_detector = None
    _face_detector_fallback = None
    _loaded_model_version = None
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def try_local_analysis(frame_path: Path) -> dict | None:
    """Run the three-stage pipeline on a captured frame.

    Stages: presence → face detection → eye state.

    Returns a flat entry dict.  When confident the dict represents a full
    classification.  When a stage fails (no face, low confidence) the dict
    still contains partial results (presence, timings, face bbox if available)
    plus a ``"fallback"`` key describing why the cloud API should still run.
    The caller uses ``"fallback"`` to decide whether to use the result as
    production or only as shadow data.

    Returns ``None`` only for hard errors (deps missing, model load failure,
    unreadable frame) where no useful data was produced.
    """
    if not _check_available():
        return None

    try:
        presence_clf, eye_clf, face_detector, face_fallback = _get_classifiers()
    except Exception as e:
        log.error("%s: classifier load failed: %s", BIRDEYE, e)
        return None

    from bilbo.pipeline.classifiers import crop_bassinet, crop_face
    from bilbo.config import EYE_STATE_CONFIDENCE_THRESHOLD
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
    if face_result is None and face_fallback is not None:
        face_result = face_fallback.detect(bassinet_crop)
        if face_result is not None:
            log.info("%s: face_detect primary missed, fallback found face conf=%.3f",
                     BIRDEYE, face_result.confidence)
    timings["face_detect"] = time.monotonic() - t_face

    if face_result is None:
        timings["total"] = time.monotonic() - t0
        log.info("%s: FALLBACK no_face_detected (%.2fs) -> cloud API",
                 BIRDEYE, timings["total"])
        entry = _build_entry("Unknown", presence, None, timings)
        entry["fallback"] = "no_face_detected"
        return entry

    log.info("%s: face_detect %.3fs  conf=%.3f bbox=%s",
             BIRDEYE, timings["face_detect"], face_result.confidence,
             face_result.normalized_bbox)

    # --- Stage 3: eye state (face crop, 2-class) ---
    t2 = time.monotonic()
    face_crop = crop_face(bassinet_crop, face_result.bbox)
    # Use the prod crop size from config (flipped 2026-04-14 from 224 → 448).
    # Passing it explicitly keeps the call site honest about which resolution
    # the deployed checkpoint was trained at — a mismatch here is silent but
    # catastrophic (features land in the wrong spatial positions).
    eye_result = eye_clf.classify(face_crop, crop_size=EYE_STATE_INPUT_SIZE)
    timings["eye_state"] = time.monotonic() - t2

    log.info("%s: eye_state %.3fs  state=%s conf=%.3f probs=%s (face crop)",
             BIRDEYE, timings["eye_state"], eye_result.state, eye_result.confidence,
             eye_result.probabilities)

    timings["total"] = time.monotonic() - t0

    # Low confidence → fall back to cloud API but still return partial result
    if eye_result.confidence < EYE_STATE_CONFIDENCE_THRESHOLD:
        log.info("%s: FALLBACK low_confidence %.3f < %.3f (%.2fs) -> cloud API",
                 BIRDEYE, eye_result.confidence, EYE_STATE_CONFIDENCE_THRESHOLD,
                 timings["total"])
        entry = _build_entry("Unknown", presence, eye_result, timings,
                             face_bbox=face_result.normalized_bbox,
                             face_confidence=face_result.confidence)
        entry["fallback"] = "low_confidence"
        return entry

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


# ---------------------------------------------------------------------------
# Shared inference entry points
# ---------------------------------------------------------------------------
#
# These two functions are the SINGLE SOURCE OF TRUTH for "run BIRDEYE on
# a frame" and "produce the shadow audit blob from a BIRDEYE result".
# Both monitor.py (live capture) and run_single_inference.py (dashboard
# re-run button) call them, so the two paths cannot drift on what the
# model output looks like or how it's mapped to storage.

def _read_deployed_model_version() -> str | None:
    """Return the version string the `latest` symlink points at, or None."""
    latest = MODELS_DIR / "latest"
    if not latest.is_symlink():
        return None
    try:
        return Path(os.readlink(latest)).name
    except OSError:
        return None


def run_birdeye_inference(frame_path: Path) -> dict | None:
    """Run BIRDEYE on a frame and return a fully-shaped entry dict.

    This is the single shared inference entry point used by both the
    live capture pipeline and the dashboard re-run button. Anything
    that wants "what does BIRDEYE produce for this frame" should call
    this — do not call ``try_local_analysis`` directly from callers,
    that's an implementation detail.

    Returns the dict from ``try_local_analysis`` annotated with
    ``shadowModelVersion`` (the deployed model version from the
    ``pipeline/models/latest`` symlink). The dict has a ``fallback``
    key set to a string when BIRDEYE bailed (``no_face_detected``,
    ``low_confidence``) — callers inspect it to decide whether to fall
    back to the cloud API for the primary entry fields.

    Returns ``None`` only on hard error (deps missing, model load
    failure, unreadable frame). On hard error the caller has no
    BIRDEYE data at all and must fall back to the cloud API.
    """
    result = try_local_analysis(frame_path)
    if result is None:
        return None
    version = _read_deployed_model_version()
    if version:
        result["shadowModelVersion"] = version
    return result


def birdeye_result_to_shadow_blob(result: dict | None) -> dict:
    """Map a BIRDEYE inference result to the legacy ``shadow`` sub-dict shape.

    The schema's indexed ``shadow_birdeye_present`` and
    ``shadow_birdeye_eye`` columns are populated by
    ``db._derive_shadow_columns()``, which reads from the entry's
    ``shadow`` sub-dict. Pre-flip this dict was BIRDEYE-vs-cloud
    comparison data; post-flip it's an immutable audit trail of what
    the model produced for each frame, kept separate from the
    user-facing primary fields (which can be corrected via the
    dashboard without overwriting the model's output).

    Both monitor.py and run_single_inference.py write the same shape
    so the indexed columns and the dashboard's frame viewer see a
    consistent structure regardless of which path produced the entry.
    """
    if not result:
        return {}
    state = result.get("state", "Unknown")
    if not result.get("babyPresent", False):
        state = "not_present"
    return {
        "birdeyeState": state,
        "eyeState": result.get("eyeState"),
        "presenceConfidence": result.get("presenceConfidence"),
        "eyeConfidence": result.get("eyeConfidence"),
        "birdeyeTimings": result.get("birdeyeTimings"),
        "fallback": result.get("fallback"),
    }
