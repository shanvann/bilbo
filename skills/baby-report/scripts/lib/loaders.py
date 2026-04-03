"""Data loading and parsing helpers."""

import csv
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import ACTIVITY_CSV, SLEEP_JSONL


def parse_time_range(args):
    """Parse --range or --from/--to into (start, end) datetimes."""
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
    """Load and filter camera sleep log entries."""
    if not SLEEP_JSONL.exists():
        return []
    entries = []
    with open(SLEEP_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00')).replace(tzinfo=None)
                if start <= ts <= end:
                    e['_ts'] = ts
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
