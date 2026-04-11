#!/usr/bin/env python3
"""Train birdeye classifiers from sleep-log.jsonl + captured frames.

No bootstrap step needed — labels come directly from the cloud API's
existing annotations in sleep-log.jsonl.

Usage:
    # Train both classifiers
    python train_classifiers.py --sleep-log ../data/sleep-log.jsonl --frames ../data/frames/

    # Train only one
    python train_classifiers.py --sleep-log ../data/sleep-log.jsonl --frames ../data/frames/ --model presence
    python train_classifiers.py --sleep-log ../data/sleep-log.jsonl --frames ../data/frames/ --model eye-state

    # With options
    python train_classifiers.py --sleep-log ../data/sleep-log.jsonl --frames ../data/frames/ \
        --epochs 40 --batch-size 32 --output models/

Label generation:
    Presence classifier:
        babyPresent == true  → "present"
        babyPresent == false → "not_present"
        Crop: fixed bassinet center (BASSINET_CROP config)

    Eye state classifier:
        state == "Awake"   → "eyes_open"
        state == "Asleep"  → "eyes_closed"
        state == "Unknown" AND babyPresent → "face_not_visible"
        headPosition from cloud API → crop center
        Crop: head region (HEAD_CROP_SIZE around head position)

    Only entries with detectionMethod == "vision-api" are used (cloud API ground truth).
    Entries from pixel-diff or birdeye are skipped.

Splitting:
    Time-block splitting (30-min blocks) to prevent data leakage from
    neighboring frames that look nearly identical.
"""

import argparse
import json
import copy
import logging
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from torchvision import models, transforms

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] train: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("train")

# Match the crop config from scripts/lib/config.py
BASSINET_CROP = {"x": 0.15, "y": 0.10, "w": 0.70, "h": 0.80}
HEAD_CROP_SIZE = 0.30
DEFAULT_HEAD_POS = {"x": 0.50, "y": 0.35}
TIME_BLOCK_MINUTES = 30


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_sleep_log(path: Path, corrections_path: Path = None, audit_path: Path = None) -> list[dict]:
    """Load sleep-log.jsonl with corrections and audit overrides applied.

    Priority: dashboard corrections > audit disagreements > original cloud API labels.
    Also includes birdeye entries that were corrected or audited (they become labeled data).
    """
    # Load corrections index: timestamp → corrected eye state or sleep state
    corrections = {}       # ts → corrected sleep state (Awake/Asleep)
    eye_corrections = {}   # ts → corrected eye state (eyes_open/eyes_closed/face_not_visible)
    if corrections_path and corrections_path.exists():
        for line in corrections_path.read_text().strip().splitlines():
            if not line:
                continue
            c = json.loads(line)
            ts = c.get("originalTimestamp")
            # Eye state corrections (direct labels for the eye classifier)
            if ts and c.get("correctedEyeState"):
                eye_corrections[ts] = c["correctedEyeState"]
            # Sleep state corrections (mapped to eye state for training)
            if ts and c.get("correctedState"):
                corrections[ts] = c["correctedState"]
        log.info("Loaded %d corrections (%d eye state, %d sleep state) from %s",
                 len(corrections) + len(eye_corrections),
                 len(eye_corrections), len(corrections), corrections_path.name)

    # Load audit index: timestamp → cloud API state (ground truth for disagreements)
    audit_labels = {}
    if audit_path and audit_path.exists():
        for line in audit_path.read_text().strip().splitlines():
            if not line:
                continue
            a = json.loads(line)
            ts = a.get("originalTimestamp")
            if ts and a.get("cloudState"):
                audit_labels[ts] = a["cloudState"]
        log.info("Loaded %d audit labels from %s", len(audit_labels), audit_path.name)

    # Map eye state labels to sleep state for the training pipeline
    EYE_TO_STATE = {"eyes_open": "Awake", "eyes_closed": "Asleep", "not_in_bassinet": "not_present"}

    entries = []
    for line in path.read_text().strip().splitlines():
        if not line:
            continue
        e = json.loads(line)
        ts = e.get("timestamp")

        # Eye state corrections (highest priority — direct eye labels from dashboard)
        if ts in eye_corrections:
            e["eyeState"] = eye_corrections[ts]
            e["state"] = EYE_TO_STATE.get(eye_corrections[ts], "Unknown")
            e["_label_source"] = "eye-correction"
            entries.append(e)
            continue

        # Sleep state corrections (human ground truth)
        if ts in corrections:
            e["state"] = corrections[ts]
            e["_label_source"] = "correction"
            entries.append(e)
            continue

        # Apply audit labels (cloud API second opinion on birdeye frames)
        if ts in audit_labels:
            e["state"] = audit_labels[ts]
            e["_label_source"] = "audit"
            entries.append(e)
            continue

        # Entries with eyeStateEdited flag (dashboard eye corrections already applied to JSONL)
        if e.get("eyeStateEdited"):
            eye = e.get("eyeState", "")
            e["state"] = EYE_TO_STATE.get(eye, "Unknown")
            e["_label_source"] = "eye-correction"
            entries.append(e)
            continue

        # Original cloud API labels
        if e.get("detectionMethod") == "vision-api":
            entries.append(e)
        elif e.get("stateEdited"):
            e["_label_source"] = "dashboard-edit"
            entries.append(e)

    return entries


def crop_bassinet(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    cfg = BASSINET_CROP
    x1 = int(w * cfg["x"])
    y1 = int(h * cfg["y"])
    x2 = int(w * (cfg["x"] + cfg["w"]))
    y2 = int(h * (cfg["y"] + cfg["h"]))
    return frame[y1:y2, x1:x2]


def crop_head_region(frame: np.ndarray, head_pos: dict) -> np.ndarray:
    h, w = frame.shape[:2]
    cx = int(w * head_pos.get("x", DEFAULT_HEAD_POS["x"]))
    cy = int(h * head_pos.get("y", DEFAULT_HEAD_POS["y"]))
    side = int(max(w, h) * HEAD_CROP_SIZE)
    half = side // 2
    x1 = max(0, cx - half)
    y1 = max(0, cy - half)
    x2 = min(w, cx + half)
    y2 = min(h, cy + half)
    crop = frame[y1:y2, x1:x2]
    ch, cw = crop.shape[:2]
    if ch != cw:
        target = max(ch, cw)
        padded = np.zeros((target, target, 3), dtype=crop.dtype)
        padded[:ch, :cw] = crop
        crop = padded
    return crop


# ---------------------------------------------------------------------------
# Time-block splitting
# ---------------------------------------------------------------------------

def time_block_split(entries: list[dict], val_frac: float = 0.15, test_frac: float = 0.10):
    """Split entries into train/val/test using time blocks to prevent leakage."""
    # Parse timestamps and assign block IDs
    blocks = {}
    for e in entries:
        ts_str = e.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            # Block = floor to TIME_BLOCK_MINUTES
            block_id = int(dt.timestamp()) // (TIME_BLOCK_MINUTES * 60)
        except (ValueError, AttributeError):
            block_id = 0
        if block_id not in blocks:
            blocks[block_id] = []
        blocks[block_id].append(e)

    # Sort blocks by time
    sorted_blocks = sorted(blocks.items())
    n_blocks = len(sorted_blocks)

    # Assign blocks to splits (chronological: train | val | test)
    n_test = max(1, int(n_blocks * test_frac))
    n_val = max(1, int(n_blocks * val_frac))
    n_train = n_blocks - n_val - n_test

    train, val, test = [], [], []
    for i, (_, block_entries) in enumerate(sorted_blocks):
        if i < n_train:
            train.extend(block_entries)
        elif i < n_train + n_val:
            val.extend(block_entries)
        else:
            test.extend(block_entries)

    return train, val, test


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

class PresenceDataset(Dataset):
    """Fixed bassinet crop → present/not_present."""

    CLASS_NAMES = ["not_present", "present"]

    def __init__(self, entries: list[dict], frames_dir: Path, transform=None):
        self.samples = []
        self.transform = transform
        skipped = 0

        for e in entries:
            fname = Path(e.get("frame", "")).name
            fpath = frames_dir / fname
            if not fpath.exists():
                skipped += 1
                continue
            # Eye state correction overrides babyPresent:
            # "not_in_bassinet" correction → not_present (label 0)
            # Any other eye correction (eyes_open, eyes_closed) → present (label 1)
            if e.get("eyeStateEdited") and e.get("eyeState") == "not_in_bassinet":
                label = 0
            elif e.get("eyeStateEdited") and e.get("eyeState") in ("eyes_open", "eyes_closed"):
                label = 1
            else:
                label = 1 if e.get("babyPresent", False) else 0
            self.samples.append((fpath, label))

        if skipped > 0:
            log.info("PresenceDataset: skipped %d entries (missing frames)", skipped)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, label = self.samples[idx]
        frame = cv2.imread(str(path))
        crop = crop_bassinet(frame)
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        if self.transform:
            rgb = self.transform(rgb)
        return rgb, label


class EyeStateDataset(Dataset):
    """Face crop → eyes_open / eyes_closed (2-class).

    Uses YuNet face detection on the bassinet crop to produce a tight face
    crop for the eye-state classifier. Entries with a corrected face bbox
    (from the dashboard) use that instead of auto-detection.

    Labels derived from cloud API state:
        Awake → eyes_open (0)
        Asleep → eyes_closed (1)
        Unknown/Drowsy → skipped (ambiguous, not useful for training)
    """

    CLASS_NAMES = ["eyes_open", "eyes_closed"]
    STATE_MAP = {
        "Awake": 0,      # eyes_open
        "Asleep": 1,     # eyes_closed
    }

    def __init__(self, entries: list[dict], frames_dir: Path, transform=None,
                 face_detector=None):
        from lib.classifiers import FaceDetector, crop_face as _crop_face
        self.samples = []  # (fpath, label, bbox_or_none)
        self.transform = transform
        self._crop_face = _crop_face

        if face_detector is None:
            face_detector = FaceDetector()

        skipped_missing = 0
        skipped_no_face = 0
        used_corrected = 0
        used_detected = 0

        for e in entries:
            if not e.get("babyPresent", False):
                continue

            state = e.get("state", "Unknown")
            label = self.STATE_MAP.get(state)
            if label is None:
                continue

            fname = Path(e.get("frame", "")).name
            fpath = frames_dir / fname
            if not fpath.exists():
                skipped_missing += 1
                continue

            # Priority 1: corrected face bbox from dashboard
            corrected_bbox = e.get("faceBboxCorrected")
            if corrected_bbox and all(k in corrected_bbox for k in ("x1", "y1", "x2", "y2")):
                self.samples.append((fpath, label, corrected_bbox))
                used_corrected += 1
                continue

            # Priority 2: auto-detect face
            frame = cv2.imread(str(fpath))
            if frame is None:
                skipped_missing += 1
                continue
            bass = crop_bassinet(frame)
            result = face_detector.detect(bass)
            if result is not None:
                self.samples.append((fpath, label, result.normalized_bbox))
                used_detected += 1
            else:
                skipped_no_face += 1

        log.info("EyeStateDataset: %d samples (detected=%d, corrected=%d), "
                 "skipped %d missing frames, %d no face detected",
                 len(self.samples), used_detected, used_corrected,
                 skipped_missing, skipped_no_face)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, label, norm_bbox = self.samples[idx]
        frame = cv2.imread(str(path))
        bass = crop_bassinet(frame)
        h, w = bass.shape[:2]

        # Convert normalized bbox to pixel coords
        x1 = int(norm_bbox["x1"] * w)
        y1 = int(norm_bbox["y1"] * h)
        x2 = int(norm_bbox["x2"] * w)
        y2 = int(norm_bbox["y2"] * h)

        crop = self._crop_face(bass, (x1, y1, x2, y2))
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        if self.transform:
            rgb = self.transform(rgb)
        return rgb, label


class FaceCropDataset(Dataset):
    """Pre-cropped face images from directory structure.

    Expects: base_dir/{eyes_open,eyes_closed}/*.jpg
    Maps: eyes_open→0, eyes_closed→1
    eyes_unclear is skipped (ambiguous for 2-class classifier).
    """

    CLASS_MAP = {"eyes_open": 0, "eyes_closed": 1}

    def __init__(self, base_dir: Path, transform=None):
        self.samples = []
        self.transform = transform

        for class_name, label in self.CLASS_MAP.items():
            class_dir = base_dir / class_name
            if not class_dir.exists():
                continue
            for img_path in sorted(class_dir.glob("*.jpg")):
                self.samples.append((img_path, label))

        counts = Counter(label for _, label in self.samples)
        names = {v: k for k, v in self.CLASS_MAP.items()}
        log.info("FaceCropDataset: %d images from %s — %s",
                 len(self.samples), base_dir,
                 {names.get(k, k): v for k, v in sorted(counts.items())})

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, label = self.samples[idx]
        img = cv2.imread(str(path))
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        if self.transform:
            rgb = self.transform(rgb)
        return rgb, label


class CombinedDataset(Dataset):
    """Concatenate multiple datasets."""

    def __init__(self, *datasets):
        self.datasets = datasets
        self.samples = []
        for ds in datasets:
            self.samples.extend(ds.samples)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        # Find which dataset this index belongs to
        offset = 0
        for ds in self.datasets:
            if idx < offset + len(ds):
                return ds[idx - offset]
            offset += len(ds)
        raise IndexError(idx)


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

def get_train_transform(size=224):
    return transforms.Compose([
        transforms.ToPILImage(),
        transforms.Resize((size, size)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(15),
        transforms.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.2, hue=0.05),
        transforms.RandomGrayscale(p=0.3),  # simulate IR
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


def get_val_transform(size=224):
    return transforms.Compose([
        transforms.ToPILImage(),
        transforms.Resize((size, size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


EYE_STATE_INPUT_SIZE = 224


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def build_model(num_classes: int, large: bool = False):
    if large:
        model = models.mobilenet_v3_large(
            weights=models.MobileNet_V3_Large_Weights.IMAGENET1K_V1,
        )
    else:
        model = models.mobilenet_v3_small(
            weights=models.MobileNet_V3_Small_Weights.IMAGENET1K_V1,
        )
    in_features = model.classifier[3].in_features
    model.classifier[3] = nn.Linear(in_features, num_classes)
    return model


def _macro_f1(val_preds: list[int], val_labels: list[int], num_classes: int) -> float:
    """Macro-averaged F1 across all classes (equal weight per class).

    Used as the best-model selection criterion during training so we don't
    pick a checkpoint that just predicts the majority class — val_loss alone
    is biased toward majority-class accuracy under heavy class imbalance.
    """
    f1s = []
    for cls_idx in range(num_classes):
        tp = sum(1 for t, p in zip(val_labels, val_preds) if t == cls_idx and p == cls_idx)
        fp = sum(1 for t, p in zip(val_labels, val_preds) if t != cls_idx and p == cls_idx)
        fn = sum(1 for t, p in zip(val_labels, val_preds) if t == cls_idx and p != cls_idx)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        if precision + recall > 0:
            f1s.append(2 * precision * recall / (precision + recall))
        else:
            f1s.append(0.0)
    return sum(f1s) / len(f1s) if f1s else 0.0


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------

def train_classifier(
    train_ds: Dataset,
    val_ds: Dataset,
    num_classes: int,
    class_names: list[str],
    output_path: Path,
    epochs: int = 30,
    batch_size: int = 32,
    lr: float = 0.001,
    patience: int = 10,
    class_weights: list[float] | None = None,
    large: bool = False,
):
    """Train a MobileNetV3 classifier with early stopping."""
    device = "cpu"
    model = build_model(num_classes, large=large).to(device)

    # Weighted sampling to handle class imbalance
    labels = [s[1] for s in train_ds.samples]
    class_counts = Counter(labels)
    log.info("Train class distribution: %s",
             {class_names[k]: v for k, v in sorted(class_counts.items())})

    sample_weights = [1.0 / class_counts[l] for l in labels]
    sampler = WeightedRandomSampler(sample_weights, len(sample_weights))

    train_loader = DataLoader(train_ds, batch_size=batch_size, sampler=sampler, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=0)

    # Loss with optional class weights
    if class_weights:
        weight_tensor = torch.tensor(class_weights, dtype=torch.float32).to(device)
        criterion = nn.CrossEntropyLoss(weight=weight_tensor)
    else:
        criterion = nn.CrossEntropyLoss()

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    # Best-model selection: macro-averaged F1 (equal weight per class), NOT
    # val_loss. Under heavy class imbalance the weighted CE loss can be lowest
    # for a near-mode-collapsed checkpoint, while macro-F1 directly rewards
    # learning every class. See v_20260409_171207 for the failure mode this
    # avoids: best_val_loss picked an epoch whose deployed weights had
    # awake→asleep miss rate = 18/18 (100%).
    best_macro_f1 = -1.0
    best_val_loss = float("inf")  # tracked for reporting only
    best_val_acc = 0.0             # tracked for reporting only
    best_state = None
    best_val_preds: list[int] = []
    best_val_labels: list[int] = []
    best_epoch = 0
    patience_counter = 0

    for epoch in range(1, epochs + 1):
        # Train
        model.train()
        train_loss = 0.0
        train_correct = 0
        train_total = 0

        for inputs, targets in train_loader:
            inputs, targets = inputs.to(device), targets.to(device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()

            train_loss += loss.item() * inputs.size(0)
            train_correct += (outputs.argmax(1) == targets).sum().item()
            train_total += inputs.size(0)

        scheduler.step()

        # Validate
        model.eval()
        val_loss = 0.0
        val_correct = 0
        val_total = 0
        val_preds: list[int] = []
        val_labels: list[int] = []

        with torch.no_grad():
            for inputs, targets in val_loader:
                inputs, targets = inputs.to(device), targets.to(device)
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                val_loss += loss.item() * inputs.size(0)
                val_correct += (outputs.argmax(1) == targets).sum().item()
                val_total += inputs.size(0)
                val_preds.extend(outputs.argmax(1).cpu().tolist())
                val_labels.extend(targets.cpu().tolist())

        train_avg = train_loss / max(train_total, 1)
        val_avg = val_loss / max(val_total, 1)
        train_acc = train_correct / max(train_total, 1)
        val_acc = val_correct / max(val_total, 1)
        macro_f1 = _macro_f1(val_preds, val_labels, num_classes)

        log.info("Epoch %d/%d — train_loss=%.4f train_acc=%.3f "
                 "val_loss=%.4f val_acc=%.3f macro_f1=%.3f",
                 epoch, epochs, train_avg, train_acc, val_avg, val_acc, macro_f1)

        if macro_f1 > best_macro_f1:
            best_macro_f1 = macro_f1
            best_val_loss = val_avg
            best_val_acc = val_acc
            # NOTE: deepcopy is REQUIRED. state_dict().copy() is a shallow
            # copy whose tensor values are references to the live
            # Parameter.data — subsequent optimizer.step() calls would
            # silently mutate the saved snapshot in place. Earlier model
            # versions (e.g. v_20260409_171207) shipped with this bug and
            # ended up deploying the LAST epoch's weights regardless of
            # which epoch was nominally "best".
            best_state = copy.deepcopy(model.state_dict())
            best_val_preds = list(val_preds)
            best_val_labels = list(val_labels)
            best_epoch = epoch
            patience_counter = 0
            log.info("  → new best model saved (macro_f1=%.3f, val_loss=%.4f, val_acc=%.3f)",
                     macro_f1, val_avg, val_acc)
        else:
            patience_counter += 1
            if patience_counter >= patience:
                log.info("Early stopping at epoch %d (best epoch %d, macro_f1=%.3f)",
                         epoch, best_epoch, best_macro_f1)
                break

    # Save best model
    if best_state is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(best_state, output_path)
        log.info("Best model saved to %s (epoch %d)", output_path, best_epoch)

    # All post-loop metrics describe the SAVED best model, not whichever
    # epoch happened to run last. Swap in the snapshot we took above.
    val_preds = best_val_preds
    val_labels = best_val_labels

    # --- Compute final metrics on best model ---
    metrics = {
        "best_epoch": best_epoch,
        "total_epochs": epoch,
        "best_val_loss": round(best_val_loss, 4),
        "best_macro_f1": round(best_macro_f1, 4),
        "val_accuracy": round(best_val_acc, 4),
        "val_total": len(val_labels),
        "train_total": len(train_ds),
    }

    # Confusion matrix
    if val_preds:
        confusion = {}
        for true_cls in range(num_classes):
            row = {}
            for pred_cls in range(num_classes):
                count = sum(1 for t, p in zip(val_labels, val_preds) if t == true_cls and p == pred_cls)
                if count > 0:
                    row[class_names[pred_cls]] = count
            confusion[class_names[true_cls]] = row
        metrics["confusion_matrix"] = confusion

        log.info("Validation confusion matrix:")
        for true_cls in range(num_classes):
            row = [sum(1 for t, p in zip(val_labels, val_preds) if t == true_cls and p == pred_cls)
                   for pred_cls in range(num_classes)]
            log.info("  %15s: %s", class_names[true_cls], row)

        # Per-class precision, recall, f1
        per_class = {}
        for cls_idx, cls_name in enumerate(class_names):
            tp = sum(1 for t, p in zip(val_labels, val_preds) if t == cls_idx and p == cls_idx)
            fp = sum(1 for t, p in zip(val_labels, val_preds) if t != cls_idx and p == cls_idx)
            fn = sum(1 for t, p in zip(val_labels, val_preds) if t == cls_idx and p != cls_idx)
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
            per_class[cls_name] = {
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "f1": round(f1, 3),
                "support": tp + fn,
            }
        metrics["per_class"] = per_class

        # Presence classifier: in/out misclassification
        if "not_present" in class_names and "present" in class_names:
            np_idx = class_names.index("not_present")
            p_idx = class_names.index("present")
            # Out labeled as in (false positive — thinks baby is there when they're not)
            out_as_in = sum(1 for t, p in zip(val_labels, val_preds) if t == np_idx and p == p_idx)
            total_out = sum(1 for t in val_labels if t == np_idx)
            # In labeled as out (false negative — misses the baby)
            in_as_out = sum(1 for t, p in zip(val_labels, val_preds) if t == p_idx and p == np_idx)
            total_in = sum(1 for t in val_labels if t == p_idx)
            metrics["out_labeled_as_in"] = f"{out_as_in}/{total_out}"
            metrics["in_labeled_as_out"] = f"{in_as_out}/{total_in}"
            metrics["class_split"] = {
                "not_present": total_out,
                "present": total_in,
                "pct_present": round(total_in / max(total_out + total_in, 1) * 100, 1),
            }
            metrics["total_val_labels"] = total_out + total_in
            log.info("Presence: out→in: %d/%d, in→out: %d/%d, split: %d/%d (%.0f%% present)",
                     out_as_in, total_out, in_as_out, total_in,
                     total_in, total_out, metrics["class_split"]["pct_present"])

        # Critical metric for eye state classifier: awake→asleep miss rate
        if "eyes_open" in class_names and "eyes_closed" in class_names:
            open_idx = class_names.index("eyes_open")
            closed_idx = class_names.index("eyes_closed")
            open_as_closed = sum(1 for t, p in zip(val_labels, val_preds)
                                 if t == open_idx and p == closed_idx)
            total_open = sum(1 for t in val_labels if t == open_idx)
            miss_rate = open_as_closed / total_open if total_open > 0 else 0
            metrics["awake_asleep_miss_rate"] = round(miss_rate, 3)
            metrics["awake_asleep_misses"] = f"{open_as_closed}/{total_open}"
            log.info("Critical: awake→asleep misses: %d/%d (%.1f%%)",
                     open_as_closed, total_open, miss_rate * 100)

            # Asleep→awake false alarms (eyes_closed predicted as eyes_open)
            closed_as_open = sum(1 for t, p in zip(val_labels, val_preds)
                                 if t == closed_idx and p == open_idx)
            total_closed = sum(1 for t in val_labels if t == closed_idx)
            false_alarm_rate = closed_as_open / total_closed if total_closed > 0 else 0
            metrics["asleep_awake_false_alarm_rate"] = round(false_alarm_rate, 3)
            metrics["asleep_awake_false_alarms"] = f"{closed_as_open}/{total_closed}"
            log.info("False alarms: asleep→awake: %d/%d (%.1f%%)",
                     closed_as_open, total_closed, false_alarm_rate * 100)

    return metrics


# ---------------------------------------------------------------------------
# Face detector training (MobileNetV3-Small + bbox regression)
# ---------------------------------------------------------------------------

class FaceDetectorDataset(Dataset):
    """Dataset for trainable face detector.

    Positive samples: frames with faceBbox or faceBboxCorrected (target = bbox + conf=1)
    Negative samples: frames with babyPresent=False (target = zeros + conf=0)

    faceBboxCorrected takes priority over faceBbox (user corrections > YuNet auto).
    """

    def __init__(self, entries: list[dict], frames_dir: Path, transform=None):
        self.transform = transform
        self.samples = []  # (path, bbox_or_none)  bbox = {x1,y1,x2,y2} normalized

        skipped_missing = 0
        positives = 0
        negatives = 0

        for e in entries:
            fname = Path(e.get("frame", "")).name
            fpath = frames_dir / fname
            if not fpath.exists():
                skipped_missing += 1
                continue

            # Priority 1: user-corrected bbox
            bbox = e.get("faceBboxCorrected")
            if bbox and all(k in bbox for k in ("x1", "y1", "x2", "y2")):
                self.samples.append((fpath, bbox))
                positives += 1
                continue

            # Priority 2: auto-detected bbox (from YuNet or backfill)
            bbox = e.get("faceBbox")
            if bbox and all(k in bbox for k in ("x1", "y1", "x2", "y2")):
                self.samples.append((fpath, bbox))
                positives += 1
                continue

            # Negative: baby not present (no face expected)
            if not e.get("babyPresent", False):
                self.samples.append((fpath, None))
                negatives += 1

        log.info("FaceDetectorDataset: %d samples (pos=%d, neg=%d), skipped %d missing",
                 len(self.samples), positives, negatives, skipped_missing)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, bbox = self.samples[idx]
        frame = cv2.imread(str(path))
        bass = crop_bassinet(frame)
        rgb = cv2.cvtColor(bass, cv2.COLOR_BGR2RGB)

        # Random horizontal flip (must flip bbox too)
        flipped = False
        if self.transform and random.random() < 0.5:
            rgb = np.fliplr(rgb).copy()
            flipped = True

        if self.transform:
            tensor = self.transform(rgb)
        else:
            tensor = transforms.ToTensor()(rgb)

        if bbox is not None:
            x1, y1, x2, y2 = bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"]
            if flipped:
                x1, x2 = 1.0 - x2, 1.0 - x1
            target = torch.tensor([x1, y1, x2, y2, 1.0], dtype=torch.float32)
        else:
            target = torch.tensor([0.0, 0.0, 0.0, 0.0, 0.0], dtype=torch.float32)

        return tensor, target


def _face_detect_transform_train():
    """Transform for face detector training — no spatial augmentation (bbox-aware flip is manual)."""
    return transforms.Compose([
        transforms.ToPILImage(),
        transforms.Resize((224, 224)),
        transforms.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.2, hue=0.05),
        transforms.RandomGrayscale(p=0.3),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


def _face_detect_transform_val():
    return transforms.Compose([
        transforms.ToPILImage(),
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


def _iou(pred_bbox, target_bbox):
    """Compute IoU between two (x1, y1, x2, y2) tensors."""
    x1 = torch.max(pred_bbox[0], target_bbox[0])
    y1 = torch.max(pred_bbox[1], target_bbox[1])
    x2 = torch.min(pred_bbox[2], target_bbox[2])
    y2 = torch.min(pred_bbox[3], target_bbox[3])

    inter = torch.clamp(x2 - x1, min=0) * torch.clamp(y2 - y1, min=0)
    area_pred = (pred_bbox[2] - pred_bbox[0]) * (pred_bbox[3] - pred_bbox[1])
    area_target = (target_bbox[2] - target_bbox[0]) * (target_bbox[3] - target_bbox[1])
    union = area_pred + area_target - inter

    return inter / (union + 1e-6)


def train_face_detector(
    train_ds: FaceDetectorDataset,
    val_ds: FaceDetectorDataset,
    output_path: Path,
    epochs: int = 40,
    batch_size: int = 32,
    lr: float = 0.0005,
    patience: int = 12,
):
    """Train the MobileNetV3-Small face detector with bbox regression."""
    device = "cpu"

    # Build model with ImageNet pre-trained weights
    model = models.mobilenet_v3_small(
        weights=models.MobileNet_V3_Small_Weights.IMAGENET1K_V1,
    )
    model.classifier = nn.Sequential(
        nn.Linear(576, 256),
        nn.Hardswish(),
        nn.Dropout(0.2),
        nn.Linear(256, 5),
    )
    model.to(device)

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=0)

    bbox_criterion = nn.SmoothL1Loss(reduction="none")
    conf_criterion = nn.BCEWithLogitsLoss()

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_metric = -1.0  # combined metric: mean IoU on positives + conf accuracy
    best_state = None
    best_epoch = 0
    patience_counter = 0
    best_metrics_snapshot = {}

    for epoch in range(1, epochs + 1):
        model.train()
        train_loss = 0.0
        train_n = 0

        for inputs, targets in train_loader:
            inputs = inputs.to(device)
            targets = targets.to(device)

            optimizer.zero_grad()
            outputs = model(inputs)  # raw logits (5 outputs)

            # Split targets
            target_bbox = targets[:, :4]
            target_conf = targets[:, 4]
            has_face = target_conf > 0.5  # positive mask

            # Confidence loss (all samples)
            loss_conf = conf_criterion(outputs[:, 4], target_conf)

            # Bbox loss (only positive samples)
            if has_face.any():
                pred_bbox = torch.sigmoid(outputs[has_face, :4])
                loss_bbox = bbox_criterion(pred_bbox, target_bbox[has_face]).mean()
            else:
                loss_bbox = torch.tensor(0.0)

            loss = loss_conf + 2.0 * loss_bbox  # weight bbox loss higher
            loss.backward()
            optimizer.step()

            train_loss += loss.item() * inputs.size(0)
            train_n += inputs.size(0)

        scheduler.step()

        # Validate
        model.eval()
        val_ious = []
        val_conf_correct = 0
        val_conf_total = 0
        val_loss = 0.0
        val_n = 0

        with torch.no_grad():
            for inputs, targets in val_loader:
                inputs = inputs.to(device)
                targets = targets.to(device)

                outputs = model(inputs)
                target_bbox = targets[:, :4]
                target_conf = targets[:, 4]
                has_face = target_conf > 0.5

                # Loss
                loss_conf = conf_criterion(outputs[:, 4], target_conf)
                if has_face.any():
                    pred_bbox = torch.sigmoid(outputs[has_face, :4])
                    loss_bbox = bbox_criterion(pred_bbox, target_bbox[has_face]).mean()
                else:
                    loss_bbox = torch.tensor(0.0)
                val_loss += (loss_conf + 2.0 * loss_bbox).item() * inputs.size(0)
                val_n += inputs.size(0)

                # IoU on positives
                pred_bbox_all = torch.sigmoid(outputs[:, :4])
                pred_conf = torch.sigmoid(outputs[:, 4])

                for i in range(inputs.size(0)):
                    if has_face[i]:
                        iou_val = _iou(pred_bbox_all[i], target_bbox[i]).item()
                        val_ious.append(iou_val)

                # Confidence accuracy
                pred_binary = (pred_conf > 0.5).float()
                val_conf_correct += (pred_binary == target_conf).sum().item()
                val_conf_total += inputs.size(0)

        mean_iou = sum(val_ious) / len(val_ious) if val_ious else 0.0
        conf_acc = val_conf_correct / max(val_conf_total, 1)
        val_avg_loss = val_loss / max(val_n, 1)
        # Combined metric: IoU matters most, confidence accuracy is secondary
        combined = 0.7 * mean_iou + 0.3 * conf_acc

        log.info("Epoch %d/%d — train_loss=%.4f val_loss=%.4f mean_iou=%.3f conf_acc=%.3f combined=%.3f",
                 epoch, epochs, train_loss / max(train_n, 1), val_avg_loss,
                 mean_iou, conf_acc, combined)

        if combined > best_metric:
            best_metric = combined
            best_state = copy.deepcopy(model.state_dict())
            best_epoch = epoch
            patience_counter = 0
            best_metrics_snapshot = {
                "mean_iou": round(mean_iou, 4),
                "conf_accuracy": round(conf_acc, 4),
                "val_loss": round(val_avg_loss, 4),
                "combined": round(combined, 4),
                "iou_samples": len(val_ious),
            }
            log.info("  → new best model (combined=%.3f, iou=%.3f, conf_acc=%.3f)",
                     combined, mean_iou, conf_acc)
        else:
            patience_counter += 1
            if patience_counter >= patience:
                log.info("Early stopping at epoch %d (best epoch %d)", epoch, best_epoch)
                break

    # Save
    if best_state is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(best_state, output_path)
        log.info("Face detector saved to %s (epoch %d)", output_path, best_epoch)

    metrics = {
        "best_epoch": best_epoch,
        "total_epochs": epoch,
        **best_metrics_snapshot,
        "train_total": len(train_ds),
        "val_total": len(val_ds),
    }
    return metrics


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Train birdeye classifiers from sleep-log labels",
    )
    parser.add_argument("--sleep-log", required=True, help="Path to sleep-log.jsonl")
    parser.add_argument("--frames", required=True, help="Directory containing frame images")
    parser.add_argument("--output", default="../pipeline/models", help="Output directory for .pt files")
    parser.add_argument("--model", default="all", choices=["all", "presence", "eye-state", "face-detect"])
    parser.add_argument("--face-crops", help="Dir with validated face crops: {eyes_open,eyes_closed,eyes_unclear}/")
    parser.add_argument("--corrections", help="Path to corrections.jsonl (dashboard edits)")
    parser.add_argument("--audit", help="Path to audit-log.jsonl (audit disagreements)")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--patience", type=int, default=10)
    args = parser.parse_args()

    # Deterministic training: same seed → same model init, same WeightedRandomSampler
    # draw order, same Python/numpy randomness. Required for clean baseline-vs-fix
    # comparisons (the time-block split is already deterministic, but model init
    # and the sampler are not). CPU-only, so no cudnn determinism flags needed.
    SEED = 42
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)

    run_started_at = datetime.now(timezone.utc)
    run_started_monotonic = time.monotonic()

    sleep_log_path = Path(args.sleep_log)
    frames_dir = Path(args.frames)
    output_dir = Path(args.output)

    corrections_path = Path(args.corrections) if args.corrections else None
    audit_path = Path(args.audit) if args.audit else None

    entries = load_sleep_log(sleep_log_path, corrections_path, audit_path)
    log.info("Loaded %d labeled entries from %s", len(entries), sleep_log_path.name)

    # Time-block split
    train_entries, val_entries, test_entries = time_block_split(entries)
    log.info("Split: train=%d, val=%d, test=%d (time-block, %d-min blocks)",
             len(train_entries), len(val_entries), len(test_entries), TIME_BLOCK_MINUTES)

    # --- Presence classifier ---
    if args.model in ("all", "presence"):
        log.info("=" * 60)
        log.info("Training PRESENCE classifier (present / not_present)")
        log.info("=" * 60)

        train_ds = PresenceDataset(train_entries, frames_dir, get_train_transform())
        val_ds = PresenceDataset(val_entries, frames_dir, get_val_transform())
        log.info("Presence: train=%d, val=%d", len(train_ds), len(val_ds))

        presence_metrics = train_classifier(
            train_ds, val_ds,
            num_classes=2,
            class_names=PresenceDataset.CLASS_NAMES,
            output_path=output_dir / "presence_classifier.pt",
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
            patience=args.patience,
        )
    else:
        presence_metrics = None

    # --- Eye state classifier ---
    if args.model in ("all", "eye-state"):
        log.info("=" * 60)
        log.info("Training EYE STATE classifier (eyes_open / eyes_closed)")
        log.info("=" * 60)

        eye_train_ds = EyeStateDataset(train_entries, frames_dir, get_train_transform(EYE_STATE_INPUT_SIZE))
        val_ds = EyeStateDataset(val_entries, frames_dir, get_val_transform(EYE_STATE_INPUT_SIZE))

        # Merge in manually validated face crops if provided
        if args.face_crops:
            face_crops_dir = Path(args.face_crops)
            face_crop_ds = FaceCropDataset(face_crops_dir, get_train_transform(EYE_STATE_INPUT_SIZE))
            if len(face_crop_ds) > 0:
                train_ds = CombinedDataset(eye_train_ds, face_crop_ds)
                log.info("Eye state: %d from sleep-log + %d from face crops = %d total train",
                         len(eye_train_ds), len(face_crop_ds), len(train_ds))
            else:
                train_ds = eye_train_ds
                log.warning("Face crops dir %s had no images, using sleep-log data only", face_crops_dir)
        else:
            train_ds = eye_train_ds

        log.info("Eye state: train=%d, val=%d", len(train_ds), len(val_ds))

        # Inverse-frequency class weights, computed from the actual training set.
        # This is the recipe that produced v_20260409_181021 — our best eye-state
        # model so far (macro_f1 = 0.564, awake→asleep miss rate = 50%).
        # Earlier exploration tried sampler-only rebalancing (class_weights=None)
        # combined with a headPos filter, which produced a much worse model
        # (v_20260409_191248: macro_f1 = 0.310, miss rate 94%) because the
        # filter dropped almost all training data. Keep this recipe until we
        # have a better-grounded experiment.
        # Formula: weight[i] = total / (num_classes * count[i]); same as
        # sklearn class_weight="balanced". Floor count at 1 (div-by-zero
        # guard) and floor weight at 1.0 (don't downweight majority).
        eye_class_counts = Counter(s[1] for s in train_ds.samples)
        eye_total = sum(eye_class_counts.values())
        eye_num_classes = len(EyeStateDataset.CLASS_NAMES)
        eye_class_weights = [
            eye_total / (eye_num_classes * max(eye_class_counts.get(i, 0), 1))
            for i in range(eye_num_classes)
        ]
        eye_class_weights = [max(w, 1.0) for w in eye_class_weights]
        log.info(
            "Eye state: train class distribution: %s",
            {EyeStateDataset.CLASS_NAMES[i]: eye_class_counts.get(i, 0)
             for i in range(eye_num_classes)},
        )
        log.info(
            "Eye state: inverse-frequency class weights: %s",
            {EyeStateDataset.CLASS_NAMES[i]: round(eye_class_weights[i], 2)
             for i in range(eye_num_classes)},
        )

        eye_metrics = train_classifier(
            train_ds, val_ds,
            num_classes=eye_num_classes,
            class_names=EyeStateDataset.CLASS_NAMES,
            output_path=output_dir / "eye_state_classifier.pt",
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
            patience=args.patience,
            class_weights=eye_class_weights,
            large=False,
        )
    else:
        eye_metrics = None

    # --- Face detector ---
    if args.model in ("all", "face-detect"):
        log.info("=" * 60)
        log.info("Training FACE DETECTOR (MobileNetV3 bbox regression)")
        log.info("=" * 60)

        # Load entries from SQLite for bbox data (more complete than JSONL)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from lib.db import get_db
        db = get_db()
        all_db_entries = db.get_entries()

        face_train_ds = FaceDetectorDataset(
            [e for i, e in enumerate(all_db_entries) if i % 5 != 0],  # 80% train
            frames_dir,
            _face_detect_transform_train(),
        )
        face_val_ds = FaceDetectorDataset(
            [e for i, e in enumerate(all_db_entries) if i % 5 == 0],  # 20% val
            frames_dir,
            _face_detect_transform_val(),
        )
        log.info("Face detector: train=%d, val=%d", len(face_train_ds), len(face_val_ds))

        if len(face_train_ds) < 50:
            log.warning("Not enough face detector training data (%d samples), skipping", len(face_train_ds))
            face_metrics = None
        else:
            face_metrics = train_face_detector(
                face_train_ds, face_val_ds,
                output_path=output_dir / "face_detector.pt",
                epochs=args.epochs,
                batch_size=args.batch_size,
                lr=0.0005,
                patience=12,
            )
    else:
        face_metrics = None

    # --- Version the models ---
    version_id = datetime.now().strftime("v_%Y%m%d_%H%M%S")
    version_dir = output_dir / version_id
    version_dir.mkdir(parents=True, exist_ok=True)

    # Copy trained models into versioned directory
    import shutil
    for pt_file in output_dir.glob("*.pt"):
        shutil.copy2(pt_file, version_dir / pt_file.name)

    # Update "latest" symlink
    latest_link = output_dir / "latest"
    if latest_link.is_symlink() or latest_link.exists():
        latest_link.unlink()
    latest_link.symlink_to(version_id)
    log.info("Model version: %s (symlinked as latest)", version_id)

    # Prune old versions (keep last 20)
    MAX_VERSIONS = 20
    version_dirs = sorted(
        [d for d in output_dir.iterdir() if d.is_dir() and d.name.startswith("v_")],
        key=lambda d: d.name,
    )
    if len(version_dirs) > MAX_VERSIONS:
        for old in version_dirs[:-MAX_VERSIONS]:
            shutil.rmtree(old)
            log.info("Pruned old model version: %s", old.name)

    # Count label sources
    source_counts = Counter(e.get("_label_source", "cloud-api") for e in entries)

    # Log training run to training-log.jsonl
    training_log = output_dir / "training-log.jsonl"
    run_finished_at = datetime.now(timezone.utc)
    duration_seconds = round(time.monotonic() - run_started_monotonic, 2)
    log_entry = {
        "version": version_id,
        "timestamp": run_finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "started_at": run_started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": run_finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": duration_seconds,
        "entries_total": len(entries),
        "label_sources": dict(source_counts),
        "split": {
            "train": len(train_entries),
            "val": len(val_entries),
            "test": len(test_entries),
        },
        "models_trained": args.model,
        "config": {
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "lr": args.lr,
            "patience": args.patience,
            "face_crops": args.face_crops is not None,
            "corrections": args.corrections is not None,
            "audit": args.audit is not None,
        },
        "metrics": {
            "presence": presence_metrics,
            "eye_state": eye_metrics,
            "face_detect": face_metrics,
        },
    }
    with open(training_log, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    log.info("Training complete. Version %s saved to %s/", version_id, output_dir)
    print(f"\nModel version: {version_id}")
    print(f"Training log:  {training_log}")
    print(f"Versions kept: {len([d for d in output_dir.iterdir() if d.is_dir() and d.name.startswith('v_')])}/{MAX_VERSIONS}")


if __name__ == "__main__":
    main()
