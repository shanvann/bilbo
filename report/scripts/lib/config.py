"""Paths and constants for baby report."""

import os
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
MONITOR_DIR = SKILL_DIR.parent / "baby-monitor" / "data"
ACTIVITY_CSV = MONITOR_DIR / "activity-log.csv"
SLEEP_JSONL = MONITOR_DIR / "sleep-log.jsonl"
MONITOR_DB = MONITOR_DIR / "monitor.db"

# Dashboard HTTP API. baby-report's monitor section is a thin client of this
# so the agent reading the report sees the exact same numbers as the live UI.
# Override via env var if the dashboard is bound elsewhere.
DASHBOARD_URL = os.environ.get("BABY_REPORT_DASHBOARD_URL", "http://localhost:5555")

# Camera monitor data starts on this date; use CSV for sleep before this
MONITOR_START = datetime(2026, 3, 28, 0, 0)
