"""Birdeye classifiers: baby presence + face detection + eye state.

  Classifier 1 — BabyPresenceClassifier (MobileNetV3-Small)
    Input:  fixed bassinet-center crop (from BASSINET_CROP config)
    Output: present / not_present

  Face detector — FaceDetector (YuNet ONNX)
    Input:  bassinet crop (after presence=True)
    Output: face bounding box or None

  Classifier 2 — EyeStateClassifier (MobileNetV3-Small)
    Input:  face crop (tight crop around detected face)
    Output: eyes_open / eyes_closed
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
from torchvision import models, transforms

from .config import (
    BASSINET_CROP,
    DEFAULT_HEAD_POS,
    FACE_CROP_PADDING,
    FACE_DETECT_MODEL,
    FACE_DETECT_NMS_THRESHOLD,
    FACE_DETECT_SCORE_THRESHOLD,
    HEAD_CROP_SIZE,
    HEAD_STATE_FILE,
)

log = logging.getLogger("monitor")


# ---------------------------------------------------------------------------
# Head state persistence
# ---------------------------------------------------------------------------

def load_head_state() -> dict:
    """Load last known head position from disk."""
    if HEAD_STATE_FILE.exists():
        try:
            state = json.loads(HEAD_STATE_FILE.read_text())
            x = state.get("x")
            y = state.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                return {"x": float(x), "y": float(y)}
        except (json.JSONDecodeError, KeyError):
            pass
    return dict(DEFAULT_HEAD_POS)


def save_head_state(x: float, y: float, source: str = "cloud-api"):
    """Persist head position to disk."""
    from datetime import datetime, timezone
    state = {
        "x": round(x, 4),
        "y": round(y, 4),
        "source": source,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    HEAD_STATE_FILE.write_text(json.dumps(state))
    log.info("birdeye: head state saved x=%.3f y=%.3f source=%s", x, y, source)


# ---------------------------------------------------------------------------
# Crop helpers
# ---------------------------------------------------------------------------

def crop_bassinet(frame: np.ndarray) -> np.ndarray:
    """Extract fixed bassinet-center crop from full frame."""
    h, w = frame.shape[:2]
    cfg = BASSINET_CROP
    x1 = int(w * cfg["x"])
    y1 = int(h * cfg["y"])
    x2 = int(w * (cfg["x"] + cfg["w"]))
    y2 = int(h * (cfg["y"] + cfg["h"]))
    return frame[y1:y2, x1:x2].copy()


def crop_head_region(frame: np.ndarray, head_pos: dict | None = None) -> np.ndarray:
    """Extract head-region crop centered on last known head position.

    head_pos: {"x": 0.0-1.0, "y": 0.0-1.0} normalized coordinates.
    Returns a square crop of HEAD_CROP_SIZE fraction of frame dimensions.
    """
    h, w = frame.shape[:2]
    pos = head_pos or load_head_state()

    cx = int(w * pos["x"])
    cy = int(h * pos["y"])

    # Square crop side = HEAD_CROP_SIZE * max(w, h)
    side = int(max(w, h) * HEAD_CROP_SIZE)
    half = side // 2

    # Clamp to frame bounds
    x1 = max(0, cx - half)
    y1 = max(0, cy - half)
    x2 = min(w, cx + half)
    y2 = min(h, cy + half)

    crop = frame[y1:y2, x1:x2].copy()

    # Pad to square if we hit an edge
    ch, cw = crop.shape[:2]
    if ch != cw:
        target = max(ch, cw)
        padded = np.zeros((target, target, 3), dtype=crop.dtype)
        padded[:ch, :cw] = crop
        crop = padded

    return crop


# ---------------------------------------------------------------------------
# Model building
# ---------------------------------------------------------------------------

def _build_mobilenet(num_classes: int, large: bool = False) -> nn.Module:
    """Build MobileNetV3 with custom head."""
    if large:
        model = models.mobilenet_v3_large(weights=None)
    else:
        model = models.mobilenet_v3_small(weights=None)
    in_features = model.classifier[3].in_features
    model.classifier[3] = nn.Linear(in_features, num_classes)
    return model


_INFERENCE_TRANSFORM = transforms.Compose([
    transforms.ToPILImage(),
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

_EYE_STATE_INFERENCE_TRANSFORM = transforms.Compose([
    transforms.ToPILImage(),
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])


# ---------------------------------------------------------------------------
# Classifier 1: Baby presence
# ---------------------------------------------------------------------------

PRESENCE_CLASSES = ["not_present", "present"]


@dataclass
class PresenceResult:
    present: bool
    confidence: float
    probabilities: dict


class BabyPresenceClassifier:
    """MobileNetV3-Small: bassinet crop → present / not_present."""

    def __init__(self, model_path: Path, device: str = "cpu"):
        self.device = device
        self.model = _build_mobilenet(num_classes=2)

        if model_path.exists():
            state = torch.load(model_path, map_location=device, weights_only=True)
            self.model.load_state_dict(state)
            log.info("birdeye: presence classifier loaded from %s", model_path)
        else:
            log.warning("birdeye: presence model not found at %s, using untrained", model_path)

        self.model.to(device)
        self.model.eval()

    def classify(self, bassinet_crop: np.ndarray) -> PresenceResult:
        rgb = cv2.cvtColor(bassinet_crop, cv2.COLOR_BGR2RGB)
        tensor = _INFERENCE_TRANSFORM(rgb).unsqueeze(0).to(self.device)

        with torch.no_grad():
            logits = self.model(tensor)
            probs = torch.softmax(logits, dim=1)[0]

        prob_dict = {name: round(float(probs[i]), 4) for i, name in enumerate(PRESENCE_CLASSES)}
        pred_idx = int(probs.argmax())
        pred_conf = float(probs[pred_idx])

        return PresenceResult(
            present=(pred_idx == 1),
            confidence=pred_conf,
            probabilities=prob_dict,
        )


# ---------------------------------------------------------------------------
# Classifier 2: Eye state
# ---------------------------------------------------------------------------

EYE_STATE_CLASSES = ["eyes_open", "eyes_closed"]


@dataclass
class EyeStateResult:
    state: str          # eyes_open | eyes_closed
    confidence: float
    probabilities: dict


class EyeStateClassifier:
    """MobileNetV3-Small: bassinet crop → eyes_open / eyes_closed."""

    def __init__(self, model_path: Path, device: str = "cpu"):
        self.device = device
        self.model = _build_mobilenet(num_classes=2)

        if model_path.exists():
            state = torch.load(model_path, map_location=device, weights_only=True)
            self.model.load_state_dict(state)
            log.info("birdeye: eye state classifier loaded from %s", model_path)
        else:
            log.warning("birdeye: eye state model not found at %s, using untrained", model_path)

        self.model.to(device)
        self.model.eval()

    def classify(self, crop: np.ndarray) -> EyeStateResult:
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        tensor = _EYE_STATE_INFERENCE_TRANSFORM(rgb).unsqueeze(0).to(self.device)

        with torch.no_grad():
            logits = self.model(tensor)
            probs = torch.softmax(logits, dim=1)[0]

        prob_dict = {name: round(float(probs[i]), 4) for i, name in enumerate(EYE_STATE_CLASSES)}
        pred_idx = int(probs.argmax())
        pred_class = EYE_STATE_CLASSES[pred_idx]
        pred_conf = float(probs[pred_idx])

        return EyeStateResult(
            state=pred_class,
            confidence=pred_conf,
            probabilities=prob_dict,
        )


# ---------------------------------------------------------------------------
# Face detector (YuNet)
# ---------------------------------------------------------------------------

@dataclass
class FaceDetectResult:
    bbox: tuple  # (x1, y1, x2, y2) in bassinet crop pixel coordinates
    confidence: float
    normalized_bbox: dict  # {x1, y1, x2, y2} as fractions 0-1 of bassinet crop


class FaceDetector:
    """YuNet face detector — finds the baby's face in the bassinet crop."""

    _INPUT_SIZE = (320, 320)

    def __init__(self, model_path: Path = FACE_DETECT_MODEL):
        if not model_path.exists():
            raise FileNotFoundError(f"YuNet model not found: {model_path}")
        self._detector = cv2.FaceDetectorYN.create(
            str(model_path),
            "",
            self._INPUT_SIZE,
            FACE_DETECT_SCORE_THRESHOLD,
            FACE_DETECT_NMS_THRESHOLD,
        )
        log.info("birdeye: face detector loaded from %s", model_path)

    def detect(self, bassinet_crop: np.ndarray) -> FaceDetectResult | None:
        """Detect the highest-confidence face in the bassinet crop.

        Returns FaceDetectResult or None if no face above threshold.
        """
        h, w = bassinet_crop.shape[:2]

        # YuNet expects a specific input size
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(bassinet_crop)

        if faces is None or len(faces) == 0:
            return None

        # Pick highest confidence face
        best = max(faces, key=lambda f: f[14])  # index 14 = confidence
        conf = float(best[14])

        if conf < FACE_DETECT_SCORE_THRESHOLD:
            return None

        # YuNet returns (x, y, w, h, ..., confidence) — convert to (x1, y1, x2, y2)
        fx, fy, fw, fh = int(best[0]), int(best[1]), int(best[2]), int(best[3])
        x1, y1 = max(0, fx), max(0, fy)
        x2, y2 = min(w, fx + fw), min(h, fy + fh)

        # Reject tiny false positives (face should be at least 5% of crop in each dimension)
        if (x2 - x1) < w * 0.05 or (y2 - y1) < h * 0.05:
            return None

        return FaceDetectResult(
            bbox=(x1, y1, x2, y2),
            confidence=conf,
            normalized_bbox={
                "x1": round(x1 / w, 4),
                "y1": round(y1 / h, 4),
                "x2": round(x2 / w, 4),
                "y2": round(y2 / h, 4),
            },
        )


# ---------------------------------------------------------------------------
# Face detector (trainable MobileNetV3)
# ---------------------------------------------------------------------------

def _build_face_detector_model() -> nn.Module:
    """MobileNetV3-Small with 5-output regression head for face detection."""
    model = models.mobilenet_v3_small(weights=None)
    # Replace classifier: features → avgpool → classifier
    # MobileNetV3-Small last channel = 576
    model.classifier = nn.Sequential(
        nn.Linear(576, 256),
        nn.Hardswish(),
        nn.Dropout(0.2),
        nn.Linear(256, 5),  # x1, y1, x2, y2, confidence
    )
    return model


class TrainableFaceDetector:
    """MobileNetV3-Small face detector: bassinet crop → face bbox + confidence.

    Same interface as FaceDetector (YuNet) so it's a drop-in replacement.
    Trained on user-corrected bboxes + YuNet auto-detections.
    """

    def __init__(self, model_path: Path, device: str = "cpu",
                 confidence_threshold: float = 0.5):
        self.device = device
        self.confidence_threshold = confidence_threshold
        self.model = _build_face_detector_model()

        if model_path.exists():
            state = torch.load(model_path, map_location=device, weights_only=True)
            self.model.load_state_dict(state)
            log.info("birdeye: trainable face detector loaded from %s", model_path)
        else:
            raise FileNotFoundError(f"Trainable face detector model not found: {model_path}")

        self.model.to(device)
        self.model.eval()

    def detect(self, bassinet_crop: np.ndarray) -> FaceDetectResult | None:
        """Detect face in bassinet crop. Returns FaceDetectResult or None."""
        h, w = bassinet_crop.shape[:2]
        rgb = cv2.cvtColor(bassinet_crop, cv2.COLOR_BGR2RGB)
        tensor = _INFERENCE_TRANSFORM(rgb).unsqueeze(0).to(self.device)

        with torch.no_grad():
            output = self.model(tensor)
            output = torch.sigmoid(output[0])  # all 5 outputs through sigmoid

        conf = float(output[4])
        if conf < self.confidence_threshold:
            return None

        # Normalized coords (0-1 fractions of bassinet crop)
        nx1, ny1, nx2, ny2 = float(output[0]), float(output[1]), float(output[2]), float(output[3])

        # Clamp and ensure valid ordering
        nx1, nx2 = min(nx1, nx2), max(nx1, nx2)
        ny1, ny2 = min(ny1, ny2), max(ny1, ny2)
        nx1 = max(0.0, min(1.0, nx1))
        ny1 = max(0.0, min(1.0, ny1))
        nx2 = max(0.0, min(1.0, nx2))
        ny2 = max(0.0, min(1.0, ny2))

        # Reject tiny predictions (less than 3% in either dimension)
        if (nx2 - nx1) < 0.03 or (ny2 - ny1) < 0.03:
            return None

        # Convert to pixel coords
        x1 = int(nx1 * w)
        y1 = int(ny1 * h)
        x2 = int(nx2 * w)
        y2 = int(ny2 * h)

        return FaceDetectResult(
            bbox=(x1, y1, x2, y2),
            confidence=conf,
            normalized_bbox={
                "x1": round(nx1, 4),
                "y1": round(ny1, 4),
                "x2": round(nx2, 4),
                "y2": round(ny2, 4),
            },
        )


def crop_face(bassinet_crop: np.ndarray, bbox: tuple,
              padding: float = FACE_CROP_PADDING) -> np.ndarray:
    """Extract a padded face crop from the bassinet crop.

    bbox: (x1, y1, x2, y2) in pixel coordinates.
    padding: fraction to expand on each side (0.3 = 30%).
    """
    h, w = bassinet_crop.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1

    # Expand by padding
    pad_x = int(bw * padding)
    pad_y = int(bh * padding)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)

    crop = bassinet_crop[y1:y2, x1:x2].copy()

    # Ensure non-empty
    if crop.size == 0:
        return bassinet_crop

    return crop
