"""JSONL read/write helpers and alert state file management."""

import json
import logging
from pathlib import Path

from bilbo.config import JSONL_FILE

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
    """Read the last N entries from JSONL.

    Uses an adaptive read-size: start with 2 KB per entry, and if we
    didn't end up with at least N complete entries, double and retry
    (up to the file size). This avoids a prior bug where the fixed
    600-bytes-per-entry budget silently under-returned once JSONL
    entries grew past ~600 bytes (post shadow-dict / experiments dict,
    real entries are ~1.4 KB).

    A partial first line (from seeking mid-line) is dropped by the
    json parser and *not* counted as a complete entry, so the loop
    keeps expanding until we actually have N parseable rows.
    """
    if not JSONL_FILE.exists():
        return []
    try:
        with open(JSONL_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []

            per_entry = 2048  # pessimistic — real entries are ~1.4 KB now
            while True:
                read_size = min(size, n * per_entry)
                f.seek(max(0, size - read_size))
                chunk = f.read().decode("utf-8", errors="replace")
                raw_lines = chunk.strip().splitlines()

                entries: list[dict] = []
                # Parse back-to-front so we can bail as soon as we have
                # N entries. Any line that fails to parse is a partial
                # (usually only the very first one after a mid-line seek).
                for line in raw_lines[-(n + 2):]:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

                if len(entries) >= n or read_size >= size:
                    return entries[-n:]

                # Didn't get enough parseable entries — expand the
                # window and try again. Capped at the file size above.
                per_entry *= 2
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
