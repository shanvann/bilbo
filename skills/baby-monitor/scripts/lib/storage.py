"""JSONL read/write helpers and alert state file management."""

import json
import logging
from pathlib import Path

from .config import JSONL_FILE

log = logging.getLogger("monitor")


def get_last_entry() -> dict | None:
    """Read the last line from the JSONL log, or None if unavailable."""
    if not JSONL_FILE.exists():
        return None
    try:
        # Read last line efficiently
        with open(JSONL_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            # Read last 2KB (more than enough for one entry)
            f.seek(max(0, size - 2048))
            chunk = f.read().decode("utf-8", errors="replace")
            lines = chunk.strip().splitlines()
            if lines:
                return json.loads(lines[-1])
    except Exception as e:
        log.debug("get_last_entry: failed: %s", e)
    return None


def get_recent_entries(n: int) -> list[dict]:
    """Read the last N entries from JSONL."""
    if not JSONL_FILE.exists():
        return []
    try:
        with open(JSONL_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            # Read enough for N entries (~500 bytes each)
            read_size = min(size, n * 600)
            f.seek(max(0, size - read_size))
            chunk = f.read().decode("utf-8", errors="replace")
            lines = chunk.strip().splitlines()
            entries = []
            for line in lines[-n:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return entries
    except Exception as e:
        log.debug("get_recent_entries: failed: %s", e)
        return []


def append_entry(entry: dict):
    """Append a JSON entry to the JSONL log."""
    entry_json = json.dumps(entry)
    log.debug("pipeline: JSONL entry size=%d bytes", len(entry_json))
    with open(JSONL_FILE, "a") as f:
        f.write(entry_json + "\n")


def read_all_entries() -> list[dict]:
    """Read all entries from JSONL. Used by CLI commands."""
    if not JSONL_FILE.exists():
        return []
    lines = JSONL_FILE.read_text().strip().splitlines()
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries
