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

    Raises FrameForbidden if the resolved path escapes FRAMES_DIR,
    FrameNotFound if no file lives at the resolved location.
    """
    if not frame_path:
        raise FrameForbidden("empty path")
    frames_dir = str(FRAMES_DIR)
    requested = os.path.realpath(frame_path)
    if not requested.startswith(frames_dir):
        raise FrameForbidden(f"path outside frames dir: {frame_path}")
    if not os.path.isfile(requested):
        raise FrameNotFound(requested)
    return Path(requested)
