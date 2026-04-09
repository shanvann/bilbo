"""SQLite database for baby monitor — replaces JSONL/JSON file I/O.

Single file: data/monitor.db
All read/write operations go through this module.
JSONL remains as append-only backup (dual-write).

Usage:
    from lib.db import get_db

    db = get_db()
    db.insert_entry({...})
    entries = db.get_entries(hours=24)
    db.insert_correction({...})
    stats = db.get_pending_corrections_count()
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR

DB_PATH = DATA_DIR / "monitor.db"

_local = threading.local()


def get_connection() -> sqlite3.Connection:
    """Get a thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            frame TEXT,
            baby_present INTEGER,
            state TEXT,
            eye_state TEXT,
            eye_state_edited INTEGER DEFAULT 0,
            eye_state_corrected_at TEXT,
            detection_method TEXT,
            model_used TEXT,
            shadow_model_version TEXT,
            presence_confidence REAL,
            eye_confidence REAL,
            diff_score REAL,
            shadow_birdeye_state TEXT,
            shadow_prod_state TEXT,
            shadow_agreed INTEGER,
            shadow_timings_total REAL,
            data JSON,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_entries_timestamp ON entries(timestamp);
        CREATE INDEX IF NOT EXISTS idx_entries_detection ON entries(detection_method);
        CREATE INDEX IF NOT EXISTS idx_entries_edited ON entries(eye_state_edited);

        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corrected_at TEXT NOT NULL,
            original_timestamp TEXT NOT NULL,
            frame TEXT,
            original_state TEXT,
            corrected_state TEXT,
            original_eye_state TEXT,
            corrected_eye_state TEXT,
            detection_method TEXT,
            source TEXT DEFAULT 'dashboard',
            used_in_training TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_corrections_corrected_at ON corrections(corrected_at);
        CREATE INDEX IF NOT EXISTS idx_corrections_trained ON corrections(used_in_training);

        CREATE TABLE IF NOT EXISTS training_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            entries_total INTEGER,
            label_sources JSON,
            split JSON,
            config JSON,
            metrics JSON,
            models_trained TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_training_version ON training_runs(version);

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value JSON,
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Entries (sleep log)
# ---------------------------------------------------------------------------

def insert_entry(entry: dict):
    """Insert a sleep log entry. Also stores full JSON in data column."""
    conn = get_connection()
    shadow = entry.get("shadow", {}) if isinstance(entry.get("shadow"), dict) else {}
    conn.execute("""
        INSERT INTO entries (
            timestamp, frame, baby_present, state, eye_state,
            eye_state_edited, eye_state_corrected_at,
            detection_method, model_used, shadow_model_version,
            presence_confidence, eye_confidence, diff_score,
            shadow_birdeye_state, shadow_prod_state, shadow_agreed,
            shadow_timings_total, data
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        entry.get("timestamp"),
        entry.get("frame"),
        1 if entry.get("babyPresent") else 0,
        entry.get("state"),
        entry.get("eyeState"),
        1 if entry.get("eyeStateEdited") else 0,
        entry.get("eyeStateCorrectedAt"),
        entry.get("detectionMethod"),
        entry.get("modelUsed"),
        entry.get("shadowModelVersion"),
        entry.get("presenceConfidence"),
        entry.get("eyeConfidence"),
        entry.get("diffScore"),
        shadow.get("birdeyeState"),
        shadow.get("prodState"),
        1 if shadow.get("agreed") else (0 if "agreed" in shadow else None),
        shadow.get("birdeyeTimings", {}).get("total") if isinstance(shadow.get("birdeyeTimings"), dict) else None,
        json.dumps(entry),
    ))
    conn.commit()


def get_entries(hours: float = None, start: str = None, end: str = None,
                limit: int = None) -> list[dict]:
    """Query entries by time range. Returns list of full entry dicts."""
    conn = get_connection()
    conditions = []
    params = []

    if hours is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conditions.append("timestamp >= ?")
        params.append(cutoff)
    if start:
        conditions.append("timestamp >= ?")
        params.append(start)
    if end:
        conditions.append("timestamp <= ?")
        params.append(end)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    order = " ORDER BY timestamp ASC"
    limit_clause = f" LIMIT {limit}" if limit else ""

    rows = conn.execute(f"SELECT data FROM entries{where}{order}{limit_clause}", params).fetchall()
    return [json.loads(row["data"]) for row in rows]


def get_last_entry() -> dict | None:
    """Get the most recent entry."""
    conn = get_connection()
    row = conn.execute("SELECT data FROM entries ORDER BY timestamp DESC LIMIT 1").fetchone()
    return json.loads(row["data"]) if row else None


def get_recent_entries(n: int) -> list[dict]:
    """Get the last N entries."""
    conn = get_connection()
    rows = conn.execute("SELECT data FROM entries ORDER BY timestamp DESC LIMIT ?", (n,)).fetchall()
    return [json.loads(row["data"]) for row in reversed(rows)]


def update_entry(timestamp: str, updates: dict) -> bool:
    """Update fields on an entry by timestamp. Returns True if found."""
    conn = get_connection()
    row = conn.execute("SELECT data FROM entries WHERE timestamp = ?", (timestamp,)).fetchone()
    if not row:
        return False

    entry = json.loads(row["data"])
    entry.update(updates)

    # Update indexed columns too
    conn.execute("""
        UPDATE entries SET
            state = ?, eye_state = ?, eye_state_edited = ?,
            eye_state_corrected_at = ?, shadow_model_version = ?,
            data = ?
        WHERE timestamp = ?
    """, (
        entry.get("state"),
        entry.get("eyeState"),
        1 if entry.get("eyeStateEdited") else 0,
        entry.get("eyeStateCorrectedAt"),
        entry.get("shadowModelVersion"),
        json.dumps(entry),
        timestamp,
    ))
    conn.commit()
    return True


def get_entry_count(hours: float = None) -> int:
    """Count entries in time range."""
    conn = get_connection()
    if hours is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = conn.execute("SELECT COUNT(*) as cnt FROM entries WHERE timestamp >= ?", (cutoff,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as cnt FROM entries").fetchone()
    return row["cnt"]


# ---------------------------------------------------------------------------
# Timeline (optimized for dashboard)
# ---------------------------------------------------------------------------

def get_timeline(hours: float = None, date: str = None) -> list[dict]:
    """Get timeline entries for dashboard. Uses indexed timestamp query."""
    conn = get_connection()
    if date:
        # Full day in ET (approximate: -4h offset)
        start = date + "T04:00:00Z"  # midnight ET = 4am UTC
        end_date = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
        end = end_date.strftime("%Y-%m-%d") + "T04:00:00Z"
    else:
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(hours=hours or 24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute("""
        SELECT data FROM entries
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp ASC
    """, (start, end)).fetchall()
    return [json.loads(row["data"]) for row in rows]


# ---------------------------------------------------------------------------
# Monitor stats (optimized aggregation)
# ---------------------------------------------------------------------------

def get_monitor_stats(hours: float = 24) -> dict:
    """Compute monitor performance stats using SQL aggregation."""
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Method counts
    methods = {}
    for row in conn.execute("""
        SELECT detection_method, COUNT(*) as cnt
        FROM entries WHERE timestamp >= ?
        GROUP BY detection_method
    """, (cutoff,)):
        methods[row["detection_method"] or "unknown"] = row["cnt"]

    total = sum(methods.values())

    # Shadow stats
    shadow_row = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN shadow_agreed = 1 THEN 1 ELSE 0 END) as agreed,
            SUM(CASE WHEN shadow_agreed = 0 THEN 1 ELSE 0 END) as disagreed
        FROM entries
        WHERE timestamp >= ? AND shadow_agreed IS NOT NULL
    """, (cutoff,)).fetchone()

    # Confidence and timing stats from shadow data
    conf_rows = conn.execute("""
        SELECT presence_confidence, eye_confidence, shadow_timings_total
        FROM entries
        WHERE timestamp >= ?
          AND (presence_confidence IS NOT NULL OR shadow_timings_total IS NOT NULL)
    """, (cutoff,)).fetchall()

    presence_confs = [r["presence_confidence"] for r in conf_rows if r["presence_confidence"] is not None]
    eye_confs = [r["eye_confidence"] for r in conf_rows if r["eye_confidence"] is not None]
    timings = [r["shadow_timings_total"] for r in conf_rows if r["shadow_timings_total"] is not None]

    def stats(vals):
        if not vals:
            return None
        s = sorted(vals)
        return {
            "avg": round(sum(s) / len(s), 3),
            "min": round(s[0], 3),
            "max": round(s[-1], 3),
            "p50": round(s[len(s) // 2], 3),
        }

    # Gaps > 10 min
    gap_count = 0
    ts_rows = conn.execute("""
        SELECT timestamp FROM entries WHERE timestamp >= ? ORDER BY timestamp
    """, (cutoff,)).fetchall()
    for i in range(1, len(ts_rows)):
        try:
            t1 = datetime.fromisoformat(ts_rows[i-1]["timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(ts_rows[i]["timestamp"].replace("Z", "+00:00"))
            if (t2 - t1).total_seconds() > 600:
                gap_count += 1
        except (ValueError, AttributeError):
            pass

    # Cloud models
    cloud_models = {}
    for row in conn.execute("""
        SELECT model_used, COUNT(*) as cnt FROM entries
        WHERE timestamp >= ? AND detection_method IN ('vision-api', 'openai-vision')
        GROUP BY model_used
    """, (cutoff,)):
        cloud_models[row["model_used"] or "unknown"] = row["cnt"]

    cloud_count = methods.get("vision-api", 0) + methods.get("openai-vision", 0)
    birdeye_count = methods.get("birdeye", 0)
    pixel_diff_count = methods.get("pixel-diff", 0)

    return {
        "hours": hours,
        "total": total,
        "methods": {
            "birdeye": birdeye_count,
            "cloud_api": cloud_count,
            "pixel_diff": pixel_diff_count,
        },
        "birdeyeRate": round(birdeye_count / total, 3) if total else 0,
        "confidence": {
            "presence": stats(presence_confs),
            "eye": stats(eye_confs),
        },
        "timing": stats(timings),
        "cloudModels": cloud_models,
        "cost": {
            "apiCalls": cloud_count,
            "apiAvoided": birdeye_count + pixel_diff_count,
            "estCost": round(cloud_count * 0.01, 2),
            "estSaved": round((birdeye_count + pixel_diff_count) * 0.01, 2),
        },
        "gaps": gap_count,
        "shadow": {
            "total": shadow_row["total"],
            "agreed": shadow_row["agreed"],
            "disagreed": shadow_row["disagreed"],
            "agreementRate": round(shadow_row["agreed"] / shadow_row["total"], 3) if shadow_row["total"] else None,
        },
    }


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

def insert_correction(correction: dict):
    """Insert a correction record."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO corrections (
            corrected_at, original_timestamp, frame,
            original_state, corrected_state,
            original_eye_state, corrected_eye_state,
            detection_method, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        correction.get("correctedAt"),
        correction.get("originalTimestamp"),
        correction.get("frame"),
        correction.get("originalState"),
        correction.get("correctedState"),
        correction.get("originalEyeState"),
        correction.get("correctedEyeState"),
        correction.get("detectionMethod"),
        correction.get("source", "dashboard"),
    ))
    conn.commit()


def get_pending_corrections_count(last_trained: str = None) -> tuple[int, int]:
    """Return (total_corrections, pending_corrections)."""
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) as cnt FROM corrections").fetchone()["cnt"]
    if not last_trained:
        return total, total
    pending = conn.execute(
        "SELECT COUNT(*) as cnt FROM corrections WHERE corrected_at > ?",
        (last_trained,)
    ).fetchone()["cnt"]
    return total, pending


# ---------------------------------------------------------------------------
# Training runs
# ---------------------------------------------------------------------------

def insert_training_run(run: dict):
    """Insert a training run record."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO training_runs (
            version, timestamp, entries_total, label_sources,
            split, config, metrics, models_trained
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run.get("version"),
        run.get("timestamp"),
        run.get("entries_total"),
        json.dumps(run.get("label_sources")),
        json.dumps(run.get("split")),
        json.dumps(run.get("config")),
        json.dumps(run.get("metrics")),
        run.get("models_trained"),
    ))
    conn.commit()


def get_last_training_runs(n: int = 2) -> list[dict]:
    """Get last N training runs, newest first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT version, timestamp, entries_total, label_sources,
               split, config, metrics, models_trained
        FROM training_runs ORDER BY timestamp DESC LIMIT ?
    """, (n,)).fetchall()
    result = []
    for row in rows:
        result.append({
            "version": row["version"],
            "timestamp": row["timestamp"],
            "entries_total": row["entries_total"],
            "label_sources": json.loads(row["label_sources"]) if row["label_sources"] else None,
            "split": json.loads(row["split"]) if row["split"] else None,
            "config": json.loads(row["config"]) if row["config"] else None,
            "metrics": json.loads(row["metrics"]) if row["metrics"] else None,
            "models_trained": row["models_trained"],
        })
    return result


# ---------------------------------------------------------------------------
# Key-value state (head position, alert state, training state)
# ---------------------------------------------------------------------------

def get_state(key: str) -> dict | None:
    """Get a state value by key."""
    conn = get_connection()
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def set_state(key: str, value: dict):
    """Set a state value (upsert)."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO state (key, value, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
    """, (key, json.dumps(value)))
    conn.commit()


# ---------------------------------------------------------------------------
# Migration: import existing JSONL/JSON files into SQLite
# ---------------------------------------------------------------------------

def migrate_from_files():
    """One-time migration: import existing data files into SQLite."""
    from .config import (
        JSONL_FILE, ALERT_STATE_FILE, ALERT_FEEDBACK_FILE,
        HEAD_STATE_FILE, CORRECTIONS_FILE, AUDIT_LOG_FILE, MODELS_DIR,
    )

    conn = get_connection()
    init_db()

    # Check if already migrated
    count = conn.execute("SELECT COUNT(*) as cnt FROM entries").fetchone()["cnt"]
    if count > 0:
        print(f"Database already has {count} entries. Skipping migration.")
        return

    # Migrate sleep-log.jsonl
    if JSONL_FILE.exists():
        entries = []
        for line in JSONL_FILE.read_text().strip().splitlines():
            if line.strip():
                entries.append(json.loads(line))
        print(f"Migrating {len(entries)} sleep log entries...")
        for entry in entries:
            insert_entry(entry)
        print(f"  Done: {len(entries)} entries")

    # Migrate corrections.jsonl
    if CORRECTIONS_FILE.exists():
        corrections = []
        for line in CORRECTIONS_FILE.read_text().strip().splitlines():
            if line.strip():
                corrections.append(json.loads(line))
        print(f"Migrating {len(corrections)} corrections...")
        for c in corrections:
            insert_correction(c)
        print(f"  Done: {len(corrections)} corrections")

    # Migrate training-log.jsonl
    training_log = MODELS_DIR / "training-log.jsonl"
    if training_log.exists():
        runs = []
        for line in training_log.read_text().strip().splitlines():
            if line.strip():
                runs.append(json.loads(line))
        print(f"Migrating {len(runs)} training runs...")
        for run in runs:
            insert_training_run(run)
        print(f"  Done: {len(runs)} training runs")

    # Migrate state files
    for key, path in [
        ("head", HEAD_STATE_FILE),
        ("alert", ALERT_STATE_FILE),
    ]:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                set_state(key, data)
                print(f"  Migrated state: {key}")
            except (json.JSONDecodeError, OSError):
                pass

    # Migrate training state
    training_state = DATA_DIR / "training-state.json"
    if training_state.exists():
        try:
            data = json.loads(training_state.read_text())
            set_state("training", data)
            print(f"  Migrated state: training")
        except (json.JSONDecodeError, OSError):
            pass

    print("Migration complete.")


# ---------------------------------------------------------------------------
# DB class for convenient access
# ---------------------------------------------------------------------------

class MonitorDB:
    """Convenience wrapper. Use get_db() to get an instance."""

    def __init__(self):
        init_db()

    # Entries
    insert_entry = staticmethod(insert_entry)
    get_entries = staticmethod(get_entries)
    get_last_entry = staticmethod(get_last_entry)
    get_recent_entries = staticmethod(get_recent_entries)
    update_entry = staticmethod(update_entry)
    get_entry_count = staticmethod(get_entry_count)
    get_timeline = staticmethod(get_timeline)
    get_monitor_stats = staticmethod(get_monitor_stats)

    # Corrections
    insert_correction = staticmethod(insert_correction)
    get_pending_corrections_count = staticmethod(get_pending_corrections_count)

    # Training
    insert_training_run = staticmethod(insert_training_run)
    get_last_training_runs = staticmethod(get_last_training_runs)

    # State
    get_state = staticmethod(get_state)
    set_state = staticmethod(set_state)

    # Migration
    migrate_from_files = staticmethod(migrate_from_files)


_db_instance = None

def get_db() -> MonitorDB:
    """Get the singleton MonitorDB instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = MonitorDB()
    return _db_instance
