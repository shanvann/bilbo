#!/usr/bin/env python3
"""Measure whether corrected face bboxes produce better eye-state predictions.

The question this answers: for frames where you've (a) drawn a corrected
face bbox on the dashboard AND (b) confirmed the eye-state label, does the
current eye-state classifier do better on a crop from your corrected bbox
than on a crop from BIRDEYE's predicted bbox?

Method — A/B with a fixed model:
  * Load the current `eye_state` model (whatever `pipeline/models/latest`
    symlinks to).
  * For each qualifying frame, run eye-state TWICE: once on the predicted
    bbox crop, once on the corrected bbox crop. Same model, two crops.
  * Compare both predictions to the human ground-truth label from the
    entry's correction.

Important: this script is **measurement only**. It does NOT overwrite the
stored `eyeState` field. Corrections remain authoritative; this just tells
you whether the face-detector's bbox quality is currently a bottleneck for
eye-state accuracy. Auto-relabeling from here would violate the project's
manual-retraining policy.

Outputs:
  * Per-frame: writes `data.bboxImpact = {onPredicted, onCorrected,
    groundTruth, modelVersion, ranAt}` to each processed entry.
  * Aggregate: writes the summary (count, accuracyOnPredicted,
    accuracyOnCorrected, delta, perClass, modelVersion, ranAt) to the
    `state` table under key `bbox_impact`. The dashboard reads this.

Usage:
    python scripts/bbox_impact.py                 # run on all qualifying frames
    python scripts/bbox_impact.py --limit 20      # cap to 20 frames
    python scripts/bbox_impact.py --since 2026-04-10  # only frames after date
    python scripts/bbox_impact.py --force         # re-run on frames already cached
    python scripts/bbox_impact.py --dry-run       # compute but don't persist
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.db import get_db
from lib.config import MODELS_DIR


def _norm_to_pixel(bbox_norm: dict, h: int, w: int) -> tuple[int, int, int, int]:
    """Convert a normalized {x1,y1,x2,y2} bbox (in [0,1] over the bassinet
    crop) to integer pixel coordinates on that same crop."""
    return (
        int(round(bbox_norm["x1"] * w)),
        int(round(bbox_norm["y1"] * h)),
        int(round(bbox_norm["x2"] * w)),
        int(round(bbox_norm["y2"] * h)),
    )


def _deployed_version() -> str | None:
    latest = MODELS_DIR / "latest"
    if not latest.is_symlink():
        return None
    try:
        return Path(os.readlink(latest)).name
    except OSError:
        return None


def _ground_truth_eye(entry: dict) -> str | None:
    """Read the confirmed eye-state label off the entry.

    Qualifies as ground truth when either:
      1. The user edited the eye-state (eyeStateEdited=1), or
      2. The user marked the entry reviewed (reviewed=1 confirms the
         existing label as correct).

    Returns 'eyes_open' / 'eyes_closed' or None if the frame doesn't
    have a confirmable binary eye label (e.g. face_not_visible).

    db.get_entries() flattens the data JSON blob into the top-level
    entry dict, so fields are read directly off `entry`.
    """
    eye = entry.get("eyeState")
    if eye in ("eyes_open", "eyes_closed"):
        if entry.get("eyeStateEdited") or entry.get("reviewed"):
            return eye
    return None


def _resolve_frame_path(raw: str) -> Path | None:
    """Turn a stored frame path into something the current filesystem can
    open, in case the DB was populated from a different working directory."""
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return p
    here = Path(__file__).resolve().parent.parent
    candidate = (here / raw).resolve()
    if candidate.exists():
        return candidate
    return None


def find_candidates(db, since: str | None, force: bool) -> list[dict]:
    """Return entries eligible for bbox-impact analysis.

    Entries come back from db.get_entries() already flattened, so the
    bbox/flag fields live directly at the top level of each entry dict.
    """
    entries = db.get_entries()  # full history
    deployed = _deployed_version()
    candidates = []
    for entry in entries:
        ts = entry.get("timestamp")
        pred = entry.get("faceBbox")
        corr = entry.get("faceBboxCorrected")
        if not (pred and corr):
            continue
        if not all(isinstance(b, dict) and all(k in b for k in ("x1", "y1", "x2", "y2"))
                   for b in (pred, corr)):
            continue
        if not _ground_truth_eye(entry):
            continue
        if since and ts < since:
            continue
        if not force:
            cached = entry.get("bboxImpact")
            if isinstance(cached, dict) and cached.get("modelVersion") == deployed:
                # Already computed against the current model — skip.
                continue
        candidates.append(entry)
    return candidates


def run_one(entry: dict, eye_clf, crop_bassinet, crop_face, cv2) -> dict | None:
    """Run eye-state on both the predicted and corrected bbox for one frame.

    Returns the per-frame impact dict, or None if the frame is unreadable.
    """
    frame_path = _resolve_frame_path(entry.get("frame", ""))
    if frame_path is None:
        return None
    frame = cv2.imread(str(frame_path))
    if frame is None:
        return None

    bassinet = crop_bassinet(frame)
    bh, bw = bassinet.shape[:2]

    pred_norm = entry["faceBbox"]
    corr_norm = entry["faceBboxCorrected"]

    pred_px = _norm_to_pixel(pred_norm, bh, bw)
    corr_px = _norm_to_pixel(corr_norm, bh, bw)

    pred_crop = crop_face(bassinet, pred_px)
    corr_crop = crop_face(bassinet, corr_px)

    pred_result = eye_clf.classify(pred_crop)
    corr_result = eye_clf.classify(corr_crop)

    gt = _ground_truth_eye(entry)

    return {
        "onPredicted": {
            "state": pred_result.state,
            "confidence": round(pred_result.confidence, 3),
            "correct": pred_result.state == gt,
        },
        "onCorrected": {
            "state": corr_result.state,
            "confidence": round(corr_result.confidence, 3),
            "correct": corr_result.state == gt,
        },
        "groundTruth": gt,
        "modelVersion": _deployed_version(),
        "ranAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def aggregate(per_frame: list[tuple[str, dict]]) -> dict:
    """Reduce per-frame impact dicts to the summary the dashboard shows."""
    n = len(per_frame)
    if n == 0:
        return {"count": 0, "accuracyOnPredicted": None,
                "accuracyOnCorrected": None, "delta": None, "perClass": {},
                "modelVersion": _deployed_version(),
                "ranAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    correct_pred = sum(1 for _, r in per_frame if r["onPredicted"]["correct"])
    correct_corr = sum(1 for _, r in per_frame if r["onCorrected"]["correct"])

    # Per-class breakdown keyed by ground-truth class
    per_class: dict[str, dict] = {}
    for _, r in per_frame:
        cls = r["groundTruth"]
        bucket = per_class.setdefault(cls, {"n": 0, "correctOnPredicted": 0, "correctOnCorrected": 0})
        bucket["n"] += 1
        if r["onPredicted"]["correct"]:
            bucket["correctOnPredicted"] += 1
        if r["onCorrected"]["correct"]:
            bucket["correctOnCorrected"] += 1
    for bucket in per_class.values():
        bucket["accuracyOnPredicted"] = round(bucket["correctOnPredicted"] / bucket["n"], 3)
        bucket["accuracyOnCorrected"] = round(bucket["correctOnCorrected"] / bucket["n"], 3)
        bucket["delta"] = round(bucket["accuracyOnCorrected"] - bucket["accuracyOnPredicted"], 3)

    # How often does the corrected-bbox prediction differ from the
    # predicted-bbox prediction? (Tells you how much bbox *matters*
    # regardless of which one is right.)
    flip_count = sum(1 for _, r in per_frame
                     if r["onPredicted"]["state"] != r["onCorrected"]["state"])

    return {
        "count": n,
        "accuracyOnPredicted": round(correct_pred / n, 3),
        "accuracyOnCorrected": round(correct_corr / n, 3),
        "delta": round((correct_corr - correct_pred) / n, 3),
        "flipRate": round(flip_count / n, 3),
        "perClass": per_class,
        "modelVersion": _deployed_version(),
        "ranAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=None,
                    help="ISO timestamp — only process frames at or after this")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of frames to process (for quick iteration)")
    ap.add_argument("--force", action="store_true",
                    help="Recompute on frames that already have a cached result for the current model")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute but do not write to the database")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # Heavy imports only once we've parsed args — keeps --help fast.
    import cv2
    from lib.local_pipeline import _check_available, _get_classifiers
    from lib.classifiers import crop_bassinet, crop_face

    if not _check_available():
        print(json.dumps({"ok": False, "error": "birdeye deps missing"}))
        return 1
    _, eye_clf, _, _ = _get_classifiers()

    db = get_db()
    candidates = find_candidates(db, args.since, args.force)
    if args.limit:
        candidates = candidates[: args.limit]

    version = _deployed_version()
    print(f"bbox_impact: model={version} candidates={len(candidates)}", file=sys.stderr)
    if not candidates:
        if not args.dry_run:
            db.set_state("bbox_impact", aggregate([]))
        print(json.dumps({"ok": True, "processed": 0, "aggregate": aggregate([])}))
        return 0

    per_frame: list[tuple[str, dict]] = []
    skipped = 0
    t0 = time.monotonic()
    for i, entry in enumerate(candidates):
        result = run_one(entry, eye_clf, crop_bassinet, crop_face, cv2)
        if result is None:
            skipped += 1
            if args.verbose:
                print(f"  [{i+1}/{len(candidates)}] SKIP {entry['timestamp']} (unreadable frame)", file=sys.stderr)
            continue
        per_frame.append((entry["timestamp"], result))
        if not args.dry_run:
            # db.update_entry takes a flat dict of field updates and merges
            # them into the entry's data JSON — no nesting.
            db.update_entry(entry["timestamp"], {"bboxImpact": result})
        if args.verbose:
            same = "✓" if result["onPredicted"]["state"] == result["onCorrected"]["state"] else "Δ"
            print(f"  [{i+1}/{len(candidates)}] {entry['timestamp']} gt={result['groundTruth']} "
                  f"pred={result['onPredicted']['state']}({result['onPredicted']['correct']}) "
                  f"corr={result['onCorrected']['state']}({result['onCorrected']['correct']}) {same}",
                  file=sys.stderr)

    agg = aggregate(per_frame)
    if not args.dry_run:
        db.set_state("bbox_impact", agg)
    elapsed = time.monotonic() - t0

    print(json.dumps({
        "ok": True,
        "processed": len(per_frame),
        "skipped": skipped,
        "elapsedSeconds": round(elapsed, 2),
        "aggregate": agg,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
