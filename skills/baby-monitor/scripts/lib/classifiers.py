"""Birdeye classifiers: baby presence + eye state (MobileNetV3-Small).

Two lightweight classifiers that replace the 3-model YOLO cascade:

  Classifier 1 — BabyPresenceClassifier
    Input:  fixed bassinet-center crop (from BASSINET_CROP config)
    Output: present / not_present

  Classifier 2 — EyeStateClassifier
    Input:  head-region crop (centered on last known head position)
    Output: eyes_open / eyes_closed / face_not_visible

Both use MobileNetV3-Small (~2.5M params, ~60ms per frame on CPU).
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

EYE_STATE_CLASSES = ["eyes_open", "eyes_closed", "face_not_visible"]


@dataclass
class EyeStateResult:
    state: str          # eyes_open | eyes_closed | face_not_visible
    confidence: float
    probabilities: dict


class EyeStateClassifier:
    """MobileNetV3-Small: bassinet crop → eyes_open / eyes_closed / face_not_visible."""

    def __init__(self, model_path: Path, device: str = "cpu"):
        self.device = device
        self.model = _build_mobilenet(num_classes=3)

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
