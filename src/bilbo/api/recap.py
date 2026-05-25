"""Time-lapse recap video generation + retrieval.

`recap_generate` stitches a day's in-bassinet frames into an MP4 via
ffmpeg's concat demuxer and caches the result. `recap_video` returns the
Path to a cached MP4 so the HTTP layer can `send_file` it (with Range
support for video scrubbing).

Cache layout (under data/videos/):
    recap_<date>_fps<N>.mp4        — the video
    recap_<date>_fps<N>.meta.json  — {"frame_count": int, "fps": int,
                                       "date": str, "generated_at": ISO}
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bilbo.config import DATA_DIR
from bilbo.storage.db import get_entries


VIDEOS_DIR = DATA_DIR / "videos"
ET = timezone(timedelta(hours=-4))

_RECAP_NAME_RE = re.compile(r"^recap_\d{4}-\d{2}-\d{2}_fps\d+\.mp4$")
_ALLOWED_FPS = {15, 30, 60}
_RECAP_TIMEOUT_SEC = 240


def _resolve_ffmpeg() -> str:
    # launchd-spawned processes get a stripped PATH, so shutil.which alone
    # won't find /usr/local/bin/ffmpeg. Look in the usual Homebrew spots too.
    for candidate in ("ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"):
        path = shutil.which(candidate) if "/" not in candidate else (candidate if os.path.isfile(candidate) else None)
        if path:
            return path
    raise RuntimeError("ffmpeg not found on PATH or in standard Homebrew locations")


def _recap_date_range_utc(date_str: str) -> tuple[str, str]:
    """ET date (YYYY-MM-DD) → [start_utc, end_utc] ISO-Z strings.

    A "date" here means the *night* that began on that ET date: 4 PM ET
    on `date` through 11 AM ET on `date + 1`.
    """
    y, m, d = (int(x) for x in date_str.split("-"))
    start_et = datetime(y, m, d, 16, 0, 0, tzinfo=ET)
    end_et = (start_et + timedelta(hours=19)).replace(minute=0, second=0)
    to_z = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return to_z(start_et), to_z(end_et)


def _stitch_frames(frame_paths: list[str], out_path: Path, fps: int) -> None:
    """Run ffmpeg concat demuxer. Raises RuntimeError on non-zero exit."""
    dur = 1.0 / fps
    lines = ["ffconcat version 1.0"]
    for p in frame_paths:
        lines.append(f"file {shlex.quote(p)}")
        lines.append(f"duration {dur:.6f}")
    lines.append(f"file {shlex.quote(frame_paths[-1])}")
    list_text = "\n".join(lines) + "\n"

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(list_text)
        list_path = tf.name
    try:
        cmd = [
            _resolve_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-vf", "scale=720:-2,fps=" + str(fps),
            "-c:v", "libx264", "-preset", "fast", "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_RECAP_TIMEOUT_SEC)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


def recap_generate(*, date: str, fps: int = 30, force: bool = False) -> dict:
    """Generate (or reuse the cached) recap MP4 for an ET date.

    Returns a dict with `status` ∈ {ready, empty} and the same shape the
    /api/recap/generate route returned. Validation errors come back as
    {error, _status}.
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or ""):
        return {"error": "date must be YYYY-MM-DD", "_status": 400}
    if fps not in _ALLOWED_FPS:
        return {"error": f"fps must be one of {sorted(_ALLOWED_FPS)}", "_status": 400}

    start_utc, end_utc = _recap_date_range_utc(date)
    entries = get_entries(start=start_utc, end=end_utc)
    # In-bassinet only — out-of-bassinet frames make the recap visually
    # noisy (empty crib, motion blur from putdowns, lighting changes during
    # carry-away) and obscure the actual sleep narrative. Existing cached
    # MP4s will auto-invalidate because the frame_count meta will differ.
    frame_paths = [e["frame"] for e in entries
                   if e.get("babyPresent")
                   and e.get("frame") and os.path.isfile(e["frame"])]

    if not frame_paths:
        return {"status": "empty", "date": date, "fps": fps, "frame_count": 0}

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"recap_{date}_fps{fps}.mp4"
    out_path = VIDEOS_DIR / name
    meta_path = VIDEOS_DIR / f"recap_{date}_fps{fps}.meta.json"

    cached = False
    if out_path.exists() and meta_path.exists() and not force:
        try:
            prev = json.loads(meta_path.read_text())
            if prev.get("frame_count") == len(frame_paths) and prev.get("fps") == fps:
                cached = True
        except (OSError, json.JSONDecodeError):
            cached = False

    if not cached:
        try:
            _stitch_frames(frame_paths, out_path, fps)
        except subprocess.TimeoutExpired:
            return {"error": "ffmpeg timed out", "_status": 504}
        except RuntimeError as e:
            return {"error": str(e), "_status": 500}
        meta = {
            "frame_count": len(frame_paths),
            "fps": fps,
            "date": date,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        meta_path.write_text(json.dumps(meta))

    size = out_path.stat().st_size
    return {
        "status": "ready",
        "cached": cached,
        "date": date,
        "fps": fps,
        "frame_count": len(frame_paths),
        "duration_sec": len(frame_paths) / fps,
        "size_bytes": size,
        "video_url": f"/api/recap/video?name={name}",
    }


def recap_video(*, name: str) -> Path | None:
    """Resolve a recap-video filename to a Path under VIDEOS_DIR.

    Returns None if the name fails the safe-filename regex or the file
    doesn't exist. Callers should treat None as "404" / "400". The regex
    restricts the form `recap_YYYY-MM-DD_fpsN.mp4` so this can't be used
    to read arbitrary files from VIDEOS_DIR.
    """
    if not name or not _RECAP_NAME_RE.match(name):
        return None
    path = VIDEOS_DIR / name
    if not path.is_file():
        return None
    return path
