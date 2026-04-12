"""Data loading and parsing helpers."""

import csv
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ACTIVITY_CSV, MONITOR_DB, SLEEP_JSONL


def parse_time_range(args):
    """Parse --range or --from/--to into (start, end) naive *local* datetimes.

    The loaders convert these to UTC at the boundary — see _local_to_utc_iso
    and _local_to_utc_naive below. The naive-local form is preserved here so
    output.py's date labels stay in the user's timezone.
    """
    now = datetime.now()
    if args.range:
        m = re.match(r'^(\d+)(h|d|w)$', args.range)
        if not m:
            print(f"Error: invalid range '{args.range}'. Use e.g. 24h, 7d, 2w", file=sys.stderr)
            sys.exit(1)
        val, unit = int(m.group(1)), m.group(2)
        delta = {'h': timedelta(hours=val), 'd': timedelta(days=val), 'w': timedelta(weeks=val)}[unit]
        return now - delta, now
    elif args.start and args.end:
        try:
            start = datetime.strptime(args.start, '%Y-%m-%d')
            end = datetime.strptime(args.end, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            return start, end
        except ValueError:
            print("Error: dates must be YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: provide --range or both --from and --to", file=sys.stderr)
        sys.exit(1)


def _local_to_utc_iso(dt: datetime) -> str:
    """Naive local datetime → UTC Zulu ISO string for DB lex comparison.

    Without this, naive `datetime.now()` strftimed as `...Z` is wrong by the
    local UTC offset — every report silently drifted by ~4h on EDT.
    """
    return dt.astimezone().astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _local_to_utc_naive(dt: datetime) -> datetime:
    """Naive local datetime → naive UTC datetime for JSONL bound comparisons."""
    return dt.astimezone().astimezone(timezone.utc).replace(tzinfo=None)


def load_activity_csv(start, end, csv_path=None):
    """Load and filter activity CSV entries."""
    path = Path(csv_path) if csv_path else ACTIVITY_CSV
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            start_str = (r.get('Start') or '').strip()
            if not start_str:
                continue
            try:
                ts = datetime.strptime(start_str, '%Y-%m-%d %H:%M')
            except ValueError:
                continue
            if start <= ts <= end:
                r['_ts'] = ts
                rows.append(r)
    return rows


def load_sleep_log(start, end):
    """Load camera entries, preferring SQLite (canonical per SKILL.md)
    and falling back to the JSONL backup.

    Each entry is a dict matching the sleep-log JSONL schema, with:
      - '_ts' (naive UTC datetime) for range filtering and downstream display
      - '_reviewed' (bool) — true when the user confirmed the label
      - '_eye_state_gt' / '_eye_state_edited' — DB's authoritative eye_state
        column and whether a human touched it
      - 'shadow' sub-dict already contains BIRDEYE fields (birdeyeState,
        prodState, agreed, presenceConfidence, eyeConfidence, eyeState,
        birdeyeTimings, fallback) — written by monitor.py at capture time
    """
    if MONITOR_DB.exists():
        return _load_from_db(start, end)
    return _load_from_jsonl(start, end)


def _load_from_db(start, end):
    entries = []
    uri = f"file:{MONITOR_DB}?mode=ro"
    # DB stores Zulu ISO strings; convert local-naive bounds to UTC first.
    start_iso = _local_to_utc_iso(start)
    end_iso = _local_to_utc_iso(end)
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute(
            """
            SELECT timestamp, data, reviewed, shadow_model_version,
                   eye_state, eye_state_edited
            FROM entries
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
            """,
            (start_iso, end_iso),
        ):
            try:
                e = json.loads(row["data"]) if row["data"] else {}
            except (TypeError, json.JSONDecodeError):
                e = {}
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
            e["_ts"] = ts
            e["_reviewed"] = bool(row["reviewed"])
            # Eye-state ground truth lives on the row itself: `eye_state` is
            # always the current authoritative label (corrected or original),
            # and `eye_state_edited=1` means a human touched it.
            e["_eye_state_gt"] = row["eye_state"]
            e["_eye_state_edited"] = bool(row["eye_state_edited"])
            if row["shadow_model_version"]:
                e.setdefault("shadowModelVersion", row["shadow_model_version"])
            entries.append(e)
    return entries


def _load_from_jsonl(start, end):
    if not SLEEP_JSONL.exists():
        return []
    start_utc = _local_to_utc_naive(start)
    end_utc = _local_to_utc_naive(end)
    entries = []
    with open(SLEEP_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00')).replace(tzinfo=None)
                if start_utc <= ts <= end_utc:
                    e['_ts'] = ts
                    e.setdefault('_reviewed', False)
                    entries.append(e)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    entries.sort(key=lambda x: x['_ts'])
    return entries


def parse_duration_str(dur_str):
    """Parse HH:MM duration string to minutes."""
    if not dur_str or not dur_str.strip():
        return 0
    parts = dur_str.strip().split(':')
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


def parse_ml(val):
    """Extract ml from a string like '60ml', '0.5oz', '30ml'."""
    if not val:
        return 0
    val = val.strip().lower()
    if 'ml' in val:
        try:
            return int(re.search(r'(\d+)', val).group(1))
        except (AttributeError, ValueError):
            return 0
    if 'oz' in val:
        try:
            oz = float(re.search(r'([\d.]+)', val).group(1))
            return int(oz * 29.57)
        except (AttributeError, ValueError):
            return 0
    try:
        return int(val)
    except ValueError:
        return 0
