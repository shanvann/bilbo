"""Frame-file access helpers for the API layer.

`get_frame_path` resolves a requested path against the frames directory and
guarantees the result lives under it (no directory traversal). Callers that
need the raw bytes use `send_file` on the returned Path.
"""
from __future__ import annotations

import os
from pathlib import Path

from bilbo.config import FRAMES_DIR


class FrameNotFound(Exception):
    pass


class FrameForbidden(Exception):
    pass


def get_frame_path(frame_path: str) -> Path:
    """Resolve `frame_path` to an absolute Path under FRAMES_DIR.

    Pre-refactor DB rows store host paths from the old `~/.openclaw/.../data/frames/`
    location, which don't resolve under the container's FRAMES_DIR. Since
    FRAMES_DIR is flat (capture writes `frame_<ts>.jpg` directly into it),
    we fall back to `FRAMES_DIR / basename` whenever the literal path is
    outside FRAMES_DIR. The basename strip neutralizes any traversal in
    the input.

    Raises FrameForbidden only when the input is empty.
    Raises FrameNotFound if no file exists at either the literal path or
    the basename-resolved fallback.
    """
    if not frame_path:
        raise FrameForbidden("empty path")
    frames_dir = str(FRAMES_DIR)
    requested = os.path.realpath(frame_path)
    if requested.startswith(frames_dir) and os.path.isfile(requested):
        return Path(requested)
    fallback = FRAMES_DIR / os.path.basename(frame_path)
    if fallback.is_file():
        return fallback
    raise FrameNotFound(frame_path)
