"""Paths and constants for baby report."""

from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
MONITOR_DIR = SKILL_DIR.parent / "baby-monitor" / "data"
ACTIVITY_CSV = MONITOR_DIR / "activity-log.csv"
SLEEP_JSONL = MONITOR_DIR / "sleep-log.jsonl"

# Camera monitor data starts on this date; use CSV for sleep before this
MONITOR_START = datetime(2026, 3, 28, 0, 0)
