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
import logging
import sys
import time
from collections import Counter
from datetime import datetime
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

def load_sleep_log(path: Path) -> list[dict]:
    """Load sleep-log.jsonl, return list of entries with vision-api labels."""
    entries = []
    for line in path.read_text().strip().splitlines():
        if not line:
            continue
        e = json.loads(line)
        # Only use cloud API labels as ground truth
        if e.get("detectionMethod") != "vision-api":
            continue
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
    """Head-region crop → eyes_open / eyes_closed / face_not_visible.

    Labels derived from cloud API state:
        Awake → eyes_open (0)
        Asleep → eyes_closed (1)
        Unknown/Drowsy (with babyPresent) → face_not_visible (2)
    """

    CLASS_NAMES = ["eyes_open", "eyes_closed", "face_not_visible"]
    STATE_MAP = {
        "Awake": 0,      # eyes_open
        "Asleep": 1,     # eyes_closed
        "Unknown": 2,    # face_not_visible
        "Drowsy": 2,     # face_not_visible
    }

    def __init__(self, entries: list[dict], frames_dir: Path, transform=None):
        self.samples = []
        self.transform = transform
        skipped = 0

        for e in entries:
            # Only baby-present frames
            if not e.get("babyPresent", False):
                continue

            fname = Path(e.get("frame", "")).name
            fpath = frames_dir / fname
            if not fpath.exists():
                skipped += 1
                continue

            state = e.get("state", "Unknown")
            label = self.STATE_MAP.get(state, 2)  # default to face_not_visible

            # Get head position from cloud API (if available)
            head_pos = e.get("headPosition")
            if not isinstance(head_pos, dict) or "x" not in head_pos:
                head_pos = dict(DEFAULT_HEAD_POS)

            self.samples.append((fpath, label, head_pos))

        if skipped > 0:
            log.info("EyeStateDataset: skipped %d entries (missing frames)", skipped)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, label, head_pos = self.samples[idx]
        frame = cv2.imread(str(path))
        crop = crop_head_region(frame, head_pos)
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        if self.transform:
            rgb = self.transform(rgb)
        return rgb, label


class FaceCropDataset(Dataset):
    """Pre-cropped face images from directory structure.

    Expects: base_dir/{eyes_open,eyes_closed,eyes_unclear}/*.jpg
    Maps: eyes_open→0, eyes_closed→1, eyes_unclear→2 (face_not_visible)
    These are manually validated crops — no head-region extraction needed.
    """

    CLASS_MAP = {"eyes_open": 0, "eyes_closed": 1, "eyes_unclear": 2}

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

def get_train_transform():
    return transforms.Compose([
        transforms.ToPILImage(),
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(15),
        transforms.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.2, hue=0.05),
        transforms.RandomGrayscale(p=0.3),  # simulate IR
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


def get_val_transform():
    return transforms.Compose([
        transforms.ToPILImage(),
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def build_model(num_classes: int):
    model = models.mobilenet_v3_small(
        weights=models.MobileNet_V3_Small_Weights.IMAGENET1K_V1,
    )
    in_features = model.classifier[3].in_features
    model.classifier[3] = nn.Linear(in_features, num_classes)
    return model


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
):
    """Train a MobileNetV3-Small classifier with early stopping."""
    device = "cpu"
    model = build_model(num_classes).to(device)

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

    best_val_loss = float("inf")
    best_state = None
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
        val_preds = []
        val_labels = []

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

        log.info("Epoch %d/%d — train_loss=%.4f train_acc=%.3f val_loss=%.4f val_acc=%.3f",
                 epoch, epochs, train_avg, train_acc, val_avg, val_acc)

        if val_avg < best_val_loss:
            best_val_loss = val_avg
            best_state = model.state_dict().copy()
            patience_counter = 0
            log.info("  → new best model saved (val_loss=%.4f)", val_avg)
        else:
            patience_counter += 1
            if patience_counter >= patience:
                log.info("Early stopping at epoch %d", epoch)
                break

    # Save best model
    if best_state is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(best_state, output_path)
        log.info("Best model saved to %s", output_path)

    # Final val confusion matrix
    if val_preds:
        log.info("Validation confusion matrix:")
        for true_cls in range(num_classes):
            row = []
            for pred_cls in range(num_classes):
                count = sum(1 for t, p in zip(val_labels, val_preds) if t == true_cls and p == pred_cls)
                row.append(count)
            log.info("  %15s: %s", class_names[true_cls], row)

    return best_val_loss


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
    parser.add_argument("--model", default="all", choices=["all", "presence", "eye-state"])
    parser.add_argument("--face-crops", help="Dir with validated face crops: {eyes_open,eyes_closed,eyes_unclear}/")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--patience", type=int, default=10)
    args = parser.parse_args()

    sleep_log_path = Path(args.sleep_log)
    frames_dir = Path(args.frames)
    output_dir = Path(args.output)

    entries = load_sleep_log(sleep_log_path)
    log.info("Loaded %d cloud-API-labeled entries from %s", len(entries), sleep_log_path.name)

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

        train_classifier(
            train_ds, val_ds,
            num_classes=2,
            class_names=PresenceDataset.CLASS_NAMES,
            output_path=output_dir / "presence_classifier.pt",
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
            patience=args.patience,
        )

    # --- Eye state classifier ---
    if args.model in ("all", "eye-state"):
        log.info("=" * 60)
        log.info("Training EYE STATE classifier (eyes_open / eyes_closed / face_not_visible)")
        log.info("=" * 60)

        eye_train_ds = EyeStateDataset(train_entries, frames_dir, get_train_transform())
        val_ds = EyeStateDataset(val_entries, frames_dir, get_val_transform())

        # Merge in manually validated face crops if provided
        if args.face_crops:
            face_crops_dir = Path(args.face_crops)
            face_crop_ds = FaceCropDataset(face_crops_dir, get_train_transform())
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

        # Weight eyes_open higher to penalize awake→asleep errors
        train_classifier(
            train_ds, val_ds,
            num_classes=3,
            class_names=EyeStateDataset.CLASS_NAMES,
            output_path=output_dir / "eye_state_classifier.pt",
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
            patience=args.patience,
            class_weights=[2.0, 1.0, 1.0],  # penalize missing eyes_open
        )

    log.info("Training complete. Models saved to %s/", output_dir)


if __name__ == "__main__":
    main()
