"""Frame capture (ffmpeg) and disk limit enforcement."""

import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import CAPTURE_TIMEOUT, FRAMES_DIR, MAX_FRAMES_KB
from .training_state import is_running as _training_is_running

log = logging.getLogger("monitor")


def capture_frame(rtsp_url: str) -> Path:
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = FRAMES_DIR / f"frame_{timestamp}.jpg"

    log.info("capture: starting -> %s", output)
    log.debug("capture: rtsp_url=%s timeout=%ds", rtsp_url.split("@")[-1], CAPTURE_TIMEOUT)
    t0 = time.monotonic()
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "fatal",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1",
            "-q:v", "2",
            str(output),
        ]
        log.debug("capture: cmd=%s", " ".join(cmd[:6]) + " ...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CAPTURE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.error("capture: FAILED - ffmpeg timed out after %ds", CAPTURE_TIMEOUT)
        raise RuntimeError("ffmpeg timed out")

    elapsed = time.monotonic() - t0

    if not output.exists():
        msg = result.stderr.strip() or "no output file produced"
        log.error("capture: FAILED after %.1fs - exit_code=%d stderr=%s", elapsed, result.returncode, msg)
        raise RuntimeError(f"capture failed: {msg}")

    size_kb = output.stat().st_size // 1024
    log.info("capture: success (%dKB, %.1fs) -> %s", size_kb, elapsed, output)
    if size_kb < 10:
        log.warning("capture: frame suspiciously small (%dKB) — possible corrupt image", size_kb)
    return output


def enforce_disk_limit():
    # Issue #5: skip pruning while a training run is active. Long trainings
    # iterate through `self.samples` populated at __init__; if retention
    # prunes frames out from under the dataloader, __getitem__ hits None
    # images and has to recurse-resample (slow). Disk overshoot during a
    # training run is bounded — at 1 capture/min × ~600 KB, a 6-hour run
    # adds ~210 MB to the 10 GB cap. is_running() self-cleans on dead PID.
    if _training_is_running():
        log.debug("cleanup: training in progress, skipping prune")
        return

    frames = sorted(FRAMES_DIR.glob("frame_*.jpg"), key=lambda p: p.stat().st_mtime)
    total_kb = sum(f.stat().st_size for f in frames) // 1024
    log.debug("cleanup: %d frames, %dKB total, limit %dKB", len(frames), total_kb, MAX_FRAMES_KB)
    if total_kb <= MAX_FRAMES_KB:
        log.debug("cleanup: within limit, nothing to do")
        return
    log.info("cleanup: frames dir at %dKB, exceeds %dKB limit", total_kb, MAX_FRAMES_KB)
    deleted = 0
    for f in frames:
        if total_kb <= MAX_FRAMES_KB:
            break
        size = f.stat().st_size // 1024
        f.unlink()
        total_kb -= size
        deleted += 1
        log.debug("cleanup: deleted %s (%dKB)", f.name, size)
    log.info("cleanup: deleted %d frames, now at %dKB", deleted, total_kb)
