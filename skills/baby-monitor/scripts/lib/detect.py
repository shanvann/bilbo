"""Pixel-diff empty bassinet detection and edge safety alert check."""

import logging
import subprocess
from pathlib import Path

from .config import PIXEL_DIFF_THRESHOLD, PIXEL_DIFF_TIMEOUT
from .storage import get_last_entry

log = logging.getLogger("monitor")


def compute_diff_score(frame_path: Path, reference_path: Path) -> float:
    """Compute average pixel difference between center crops of two frames.

    Uses ffmpeg to crop center 60%, scale to 320x180, compute per-pixel
    difference in grayscale, and return the average value (0-255).
    Low score = frames are very similar (bassinet still empty).
    High score = something changed (baby placed).
    Returns -1 on error.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "fatal",
        "-i", str(frame_path),
        "-i", str(reference_path),
        "-filter_complex",
        "[0:v]crop=1152:648:384:216,scale=320:180[a];"
        "[1:v]crop=1152:648:384:216,scale=320:180[b];"
        "[a][b]blend=all_mode=difference,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=PIXEL_DIFF_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("pixel-diff: ffmpeg timed out after %ds", PIXEL_DIFF_TIMEOUT)
        return -1
    if result.returncode != 0 or not result.stdout:
        log.warning("pixel-diff: ffmpeg failed (exit=%d, stdout=%d bytes)",
                    result.returncode, len(result.stdout))
        return -1
    pixels = result.stdout
    return sum(pixels) / len(pixels)


def detect_empty_bassinet(frame_path: Path) -> tuple[bool, float]:
    """Check if bassinet is empty using pixel-diff against previous frame.

    Returns (is_empty, diff_score).
    Only returns is_empty=True if:
      1. Previous JSONL entry exists and was empty (babyPresent=false)
      2. Diff score between current and previous frame is below threshold
    On any error or uncertainty, returns (False, score) -> API will be called.
    """
    prev = get_last_entry()
    if not prev:
        log.debug("pixel-diff: no previous entry, skipping detection")
        return False, -1

    if prev.get("babyPresent", True):
        log.debug("pixel-diff: previous frame had baby, skipping detection")
        return False, -1

    prev_frame = prev.get("frame", "")
    if not prev_frame or not Path(prev_frame).exists():
        log.debug("pixel-diff: previous frame file missing, skipping detection")
        return False, -1

    score = compute_diff_score(frame_path, Path(prev_frame))
    if score < 0:
        log.debug("pixel-diff: computation failed, defaulting to API")
        return False, score

    is_empty = score < PIXEL_DIFF_THRESHOLD
    log.info("pixel-diff: score=%.2f threshold=%d → %s",
             score, PIXEL_DIFF_THRESHOLD, "EMPTY (skip API)" if is_empty else "CHANGED (call API)")
    return is_empty, score


def make_empty_entry(frame_path: Path, diff_score: float) -> dict:
    """Create a JSONL entry for an empty bassinet (API skipped)."""
    return {
        "babyPresent": False,
        "sleepPosition": "Unknown",
        "objectsInBassinet": "Unknown",
        "swaddle": "Unknown",
        "headCovering": "Unknown",
        "lighting": "Unknown",
        "state": "not_present",
        "bodyPosture": "Unknown",
        "pacifierEngaged": "Unknown",
        "bassinetCondition": "Unknown",
        "hazards": "Unknown",
        "captureMode": "Unknown",
        "cameraTimestamp": None,
        "detectionMethod": "pixel-diff",
        "diffScore": round(diff_score, 2),
        "alerts": [],
    }
