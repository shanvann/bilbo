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
import os
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
            shadow_birdeye_present INTEGER,
            shadow_birdeye_eye TEXT,
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
            models_trained TEXT,
            duration_seconds REAL,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_training_version ON training_runs(version);

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value JSON,
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
    """)

    # Idempotent migrations for existing DBs
    for table, col, decl in (
        ("training_runs", "duration_seconds", "REAL"),
        ("training_runs", "started_at", "TEXT"),
        ("training_runs", "finished_at", "TEXT"),
        ("entries", "reviewed", "INTEGER DEFAULT 0"),
        ("entries", "reviewed_at", "TEXT"),
        # Eye-state shadow columns (replaced the old state-domain ones).
        # shadow_birdeye_present: 1 if BIRDEYE saw a baby, 0 if not, NULL if BIRDEYE didn't run.
        # shadow_birdeye_eye: eyes_open / eyes_closed / NULL.
        ("entries", "shadow_birdeye_present", "INTEGER"),
        ("entries", "shadow_birdeye_eye", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()


# ---------------------------------------------------------------------------
# Entries (sleep log)
# ---------------------------------------------------------------------------

_EYE_LABELS = ("eyes_open", "eyes_closed")


def _bbox_iou(a: dict, b: dict) -> float:
    """Intersection-over-union for two normalized bboxes.

    Both dicts are {"x1","y1","x2","y2"} in [0, 1] coordinates relative to
    the same reference frame (the bassinet crop).  Returns 0.0 when either
    box is degenerate or the boxes don't overlap.
    """
    ix1 = max(a["x1"], b["x1"])
    iy1 = max(a["y1"], b["y1"])
    ix2 = min(a["x2"], b["x2"])
    iy2 = min(a["y2"], b["y2"])
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    a_area = max(0.0, a["x2"] - a["x1"]) * max(0.0, a["y2"] - a["y1"])
    b_area = max(0.0, b["x2"] - b["x1"]) * max(0.0, b["y2"] - b["y1"])
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


def _summarize_iou(values: list[float]) -> dict:
    """Reduce a list of IoU scores to the summary the dashboard renders.

    n/mean/p50/p10 give shape; over50 and over75 are the conventional
    "detection is usable" / "detection is tight" thresholds from the
    object-detection literature.  Empty input returns a zeroed shape so
    the frontend can render the card without branching on null.
    """
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "p50": None, "p10": None,
                "over50": 0, "over75": 0}
    sorted_vals = sorted(values)
    mean = sum(sorted_vals) / n
    p50 = sorted_vals[n // 2]
    p10 = sorted_vals[max(0, int(n * 0.1) - 1)]
    return {
        "n": n,
        "mean": round(mean, 3),
        "p50": round(p50, 3),
        "p10": round(p10, 3),
        "over50": sum(1 for v in sorted_vals if v >= 0.5),
        "over75": sum(1 for v in sorted_vals if v >= 0.75),
    }


def _derive_shadow_columns(entry: dict, shadow: dict) -> tuple[int | None, str | None, int | None]:
    """Derive the three indexed shadow columns from the in-memory entry + shadow dict.

    Returns (shadow_birdeye_present, shadow_birdeye_eye, shadow_agreed). All
    three can be None when the shadow comparison didn't run or can't be compared.
    """
    if not shadow:
        return None, None, None

    # Presence: BIRDEYE saw a baby iff its state label isn't the sentinel
    # 'not_present'. The JSON shadow dict still carries the state-domain
    # birdeyeState field for back-compat with historical readers.
    birdeye_state_legacy = shadow.get("birdeyeState")
    if birdeye_state_legacy is None:
        present = None
    elif birdeye_state_legacy == "not_present":
        present = 0
    else:
        present = 1

    birdeye_eye = shadow.get("eyeState")
    if birdeye_eye not in _EYE_LABELS:
        birdeye_eye = None

    prod_eye = entry.get("eyeState")
    if prod_eye not in _EYE_LABELS or birdeye_eye is None:
        agreed = None
    else:
        agreed = 1 if birdeye_eye == prod_eye else 0

    return present, birdeye_eye, agreed


def insert_entry(entry: dict):
    """Insert a sleep log entry. Also stores full JSON in data column."""
    conn = get_connection()
    shadow = entry.get("shadow", {}) if isinstance(entry.get("shadow"), dict) else {}
    birdeye_present, birdeye_eye, agreed = _derive_shadow_columns(entry, shadow)
    conn.execute("""
        INSERT INTO entries (
            timestamp, frame, baby_present, state, eye_state,
            eye_state_edited, eye_state_corrected_at,
            detection_method, model_used, shadow_model_version,
            presence_confidence, eye_confidence, diff_score,
            shadow_birdeye_present, shadow_birdeye_eye, shadow_agreed,
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
        birdeye_present,
        birdeye_eye,
        agreed,
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

    shadow = entry.get("shadow") if isinstance(entry.get("shadow"), dict) else {}
    bird_present, bird_eye, agreed = _derive_shadow_columns(entry, shadow)
    bird_timings_total = (
        shadow.get("birdeyeTimings", {}).get("total")
        if isinstance(shadow.get("birdeyeTimings"), dict) else None
    )

    # Update indexed columns too
    conn.execute("""
        UPDATE entries SET
            state = ?, eye_state = ?, eye_state_edited = ?,
            eye_state_corrected_at = ?, shadow_model_version = ?,
            shadow_birdeye_present = ?, shadow_birdeye_eye = ?,
            shadow_agreed = ?, shadow_timings_total = ?,
            data = ?
        WHERE timestamp = ?
    """, (
        entry.get("state"),
        entry.get("eyeState"),
        1 if entry.get("eyeStateEdited") else 0,
        entry.get("eyeStateCorrectedAt"),
        entry.get("shadowModelVersion"),
        bird_present,
        bird_eye,
        agreed,
        bird_timings_total,
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
        n = len(s)
        return {
            "avg": round(sum(s) / n, 3),
            "min": round(s[0], 3),
            "max": round(s[-1], 3),
            "p50": round(s[n // 2], 3),
            "p99": round(s[min(n - 1, int(n * 0.99))], 3),
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

    # BIRDEYE model version distribution — which versioned checkpoint
    # produced the BIRDEYE-decided frames in the window. Post-flip this
    # is ~99% of non-empty frames. Pre-flip (shadow mode) it's empty
    # because detection_method was 'vision-api' for every non-empty
    # frame regardless of shadow-BIRDEYE's output.
    birdeye_versions: dict[str, int] = {}
    for row in conn.execute("""
        SELECT shadow_model_version, COUNT(*) as cnt FROM entries
        WHERE timestamp >= ? AND detection_method = 'birdeye'
        GROUP BY shadow_model_version
        ORDER BY cnt DESC
    """, (cutoff,)):
        key = row["shadow_model_version"] or "(unknown)"
        birdeye_versions[key] = row["cnt"]

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
        "birdeyeVersions": birdeye_versions,
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


def mark_reviewed(timestamps: list[str]) -> int:
    """Mark a list of entries as reviewed (human-confirmed ground truth).

    Returns the number of entries updated.
    """
    if not timestamps:
        return 0
    conn = get_connection()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    for ts in timestamps:
        row = conn.execute("SELECT data FROM entries WHERE timestamp = ?", (ts,)).fetchone()
        if not row:
            continue
        entry = json.loads(row["data"])
        entry["reviewed"] = True
        entry["reviewedAt"] = now
        conn.execute("""
            UPDATE entries SET reviewed = 1, reviewed_at = ?, data = ?
            WHERE timestamp = ?
        """, (now, json.dumps(entry), ts))
        updated += 1
    conn.commit()
    return updated


def get_reviewed_ground_truth() -> list[dict]:
    """Get all reviewed frames as ground truth for F1 calculation.

    Returns entries that are either:
    - reviewed=1 (user confirmed the label, corrected or not)
    - eye_state_edited=1 (user corrected the label)

    For reviewed-but-uncorrected frames, the existing eye_state/state
    from the cloud API is the confirmed ground truth.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT data FROM entries
        WHERE reviewed = 1 OR eye_state_edited = 1
        ORDER BY timestamp ASC
    """).fetchall()
    return [json.loads(row["data"]) for row in rows]


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


def get_pending_corrections(last_trained: str = None) -> list[dict]:
    """Return pending correction records (not yet used in training).

    Each record includes the correction details plus the entry's current
    eye state and shadow predictions for context.
    """
    conn = get_connection()
    if last_trained:
        rows = conn.execute("""
            SELECT c.corrected_at, c.original_timestamp, c.frame,
                   c.original_state, c.corrected_state,
                   c.original_eye_state, c.corrected_eye_state,
                   c.detection_method, c.source,
                   e.shadow_birdeye_present, e.shadow_birdeye_eye,
                   e.presence_confidence, e.eye_confidence
            FROM corrections c
            LEFT JOIN entries e ON e.timestamp = c.original_timestamp
            WHERE c.corrected_at > ?
            ORDER BY c.corrected_at DESC
        """, (last_trained,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT c.corrected_at, c.original_timestamp, c.frame,
                   c.original_state, c.corrected_state,
                   c.original_eye_state, c.corrected_eye_state,
                   c.detection_method, c.source,
                   e.shadow_birdeye_present, e.shadow_birdeye_eye,
                   e.presence_confidence, e.eye_confidence
            FROM corrections c
            LEFT JOIN entries e ON e.timestamp = c.original_timestamp
            ORDER BY c.corrected_at DESC
        """).fetchall()

    result = []
    for row in rows:
        result.append({
            "correctedAt": row["corrected_at"],
            "originalTimestamp": row["original_timestamp"],
            "frame": row["frame"],
            "originalState": row["original_state"],
            "correctedState": row["corrected_state"],
            "originalEyeState": row["original_eye_state"],
            "correctedEyeState": row["corrected_eye_state"],
            "detectionMethod": row["detection_method"],
            "source": row["source"],
            "shadowBirdeyePresent": row["shadow_birdeye_present"],
            "shadowBirdeyeEye": row["shadow_birdeye_eye"],
            "presenceConfidence": row["presence_confidence"],
            "eyeConfidence": row["eye_confidence"],
        })
    return result


# ---------------------------------------------------------------------------
# Training runs
# ---------------------------------------------------------------------------

def insert_training_run(run: dict):
    """Insert a training run record."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO training_runs (
            version, timestamp, entries_total, label_sources,
            split, config, metrics, models_trained,
            duration_seconds, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run.get("version"),
        run.get("timestamp"),
        run.get("entries_total"),
        json.dumps(run.get("label_sources")),
        json.dumps(run.get("split")),
        json.dumps(run.get("config")),
        json.dumps(run.get("metrics")),
        run.get("models_trained"),
        run.get("duration_seconds"),
        run.get("started_at"),
        run.get("finished_at"),
    ))
    conn.commit()


def get_last_training_runs(n: int = 2) -> list[dict]:
    """Get last N training runs, newest first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT version, timestamp, entries_total, label_sources,
               split, config, metrics, models_trained,
               duration_seconds, started_at, finished_at
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
            "duration_seconds": row["duration_seconds"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        })
    return result


def update_training_run_metrics(version: str, updates: dict) -> bool:
    """Merge `updates` into the `metrics` JSON column of one training_runs row.

    Read-modify-write — safe because we only expect one writer (cmd_retrain
    and cmd_eval_corrections never overlap). Returns True if a matching row
    was found and updated, False if no row exists for that version.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT metrics FROM training_runs WHERE version = ? LIMIT 1",
        (version,),
    ).fetchone()
    if row is None:
        return False
    try:
        existing = json.loads(row["metrics"]) if row["metrics"] else {}
    except (TypeError, json.JSONDecodeError):
        existing = {}
    existing.update(updates)
    conn.execute(
        "UPDATE training_runs SET metrics = ? WHERE version = ?",
        (json.dumps(existing), version),
    )
    conn.commit()
    return True


def get_safety_stats(hours: float = 168) -> dict:
    """Compute classifier performance vs ground truth (corrections).

    Returns per-classifier panels comparing both BIRDEYE and Cloud API
    against human corrections (the only real ground truth).  The vsCloud
    shadow comparison is no longer surfaced — corrections are authoritative.

    Each classifier panel contains:
      - birdeyeVsCorrections: BIRDEYE predictions vs correction labels
      - cloudVsCorrections:   Cloud API predictions vs correction labels
      - production:           windowed shadow stats (detection rate, timing)
    """
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    STATE_TO_EYE = {
        "asleep": "eyes_closed",
        "awake": "eyes_open",
    }

    def build_panel(pairs: list[tuple[str, str]], classes: list[str]) -> dict:
        """Build a confusion + per-class P/R/F1 + macro-F1 panel.
        pairs: list of (truth, predicted)."""
        cm = {t: {p: 0 for p in classes} for t in classes}
        for t, p in pairs:
            if t in cm and p in cm[t]:
                cm[t][p] += 1

        per_class = {}
        for cls in classes:
            tp = cm[cls][cls]
            fn = sum(cm[cls][p] for p in classes if p != cls)
            fp = sum(cm[t][cls] for t in classes if t != cls)
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
            per_class[cls] = {
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "f1": round(f1, 3),
                "support": tp + fn,
            }

        total = sum(sum(row.values()) for row in cm.values())
        correct = sum(cm[c][c] for c in classes)
        macro_f1 = sum(c["f1"] for c in per_class.values()) / len(classes) if classes else 0.0
        return {
            "confusion": cm,
            "perClass": per_class,
            "macroF1": round(macro_f1, 3),
            "accuracy": round(correct / total, 3) if total > 0 else 0.0,
            "total": total,
        }

    # ----- Compute vs ground truth (reviewed + corrected frames) -----
    # Ground truth = reviewed frames (user confirmed label) + corrected
    # frames (user changed label).  For reviewed-but-uncorrected frames
    # the existing eye_state is confirmed correct.  For corrected frames
    # the corrected eye_state overrides.
    gt_rows = conn.execute("""
        SELECT e.timestamp, e.state AS cloud_state, e.eye_state AS cloud_eye_state,
               e.shadow_birdeye_present, e.shadow_birdeye_eye,
               e.baby_present, e.eye_state_edited,
               e.reviewed, e.data,
               c.corrected_eye_state, c.corrected_state
        FROM entries e
        LEFT JOIN corrections c ON c.original_timestamp = e.timestamp
        WHERE e.reviewed = 1 OR e.eye_state_edited = 1
        ORDER BY e.timestamp ASC
    """).fetchall()

    # Deduplicate: if an entry has multiple corrections, take the latest
    seen = set()
    unique_rows = []
    for row in reversed(gt_rows):
        ts = row["timestamp"]
        if ts in seen:
            continue
        seen.add(ts)
        unique_rows.append(row)
    unique_rows.reverse()

    # Build pairs: (truth, predicted) for each source
    bird_pres_pairs = []
    cloud_pres_pairs = []
    bird_eye_pairs = []
    cloud_eye_pairs = []
    reviewed_count = 0
    corrected_count = 0

    for row in unique_rows:
        # Derive (truth_present, truth_eye) separately. Presence is known
        # whenever we have any human signal; eye-state is only defined for
        # frames where the baby is present AND the eyes are scoreable. A
        # frame can contribute to the presence matrix without contributing
        # to the eye-state matrix (e.g. reviewed "Unknown" cloud labels, or
        # corrections to "face_not_visible").
        # Priority: correction > reviewed entry's existing label.
        truth_present = None
        truth_eye = None
        if row["corrected_eye_state"]:
            ces = row["corrected_eye_state"]
            if ces in ("eyes_open", "eyes_closed"):
                truth_present, truth_eye = True, ces
            elif ces == "not_in_bassinet":
                truth_present, truth_eye = False, "not_in_bassinet"
            elif ces == "face_not_visible":
                # Baby present, face occluded — counts for presence only.
                truth_present, truth_eye = True, None
            corrected_count += 1
        elif row["corrected_state"]:
            cs = (row["corrected_state"] or "").lower()
            mapped = STATE_TO_EYE.get(cs)
            if mapped:
                truth_present, truth_eye = True, mapped
            corrected_count += 1
        elif row["reviewed"]:
            # Reviewed but not corrected — the existing label is ground truth.
            if not row["baby_present"]:
                truth_present, truth_eye = False, "not_in_bassinet"
            else:
                truth_present = True
                eye = row["cloud_eye_state"]
                if eye in ("eyes_open", "eyes_closed"):
                    truth_eye = eye
                else:
                    truth_eye = STATE_TO_EYE.get((row["cloud_state"] or "").lower())
                # truth_eye may stay None for ambiguous cloud labels (e.g.
                # state="Unknown") — that's fine, presence still counts.
            reviewed_count += 1

        if truth_present is None:
            continue

        # --- Cloud API predictions ---
        cloud_eye = row["cloud_eye_state"]
        if not cloud_eye:
            cs = (row["cloud_state"] or "").lower()
            cloud_eye = STATE_TO_EYE.get(cs)
        cloud_present = row["baby_present"] == 1

        cloud_pres_pairs.append((
            "present" if truth_present else "not_present",
            "present" if cloud_present else "not_present",
        ))
        if truth_present and cloud_present and truth_eye in ("eyes_open", "eyes_closed"):
            if cloud_eye in ("eyes_open", "eyes_closed"):
                cloud_eye_pairs.append((truth_eye, cloud_eye))

        # --- BIRDEYE predictions ---
        # shadow_birdeye_present: 1 if BIRDEYE saw a baby, 0 if not, NULL if BIRDEYE didn't run.
        bird_present_col = row["shadow_birdeye_present"]
        if bird_present_col is None:
            continue  # no shadow data for this frame
        bird_present = bool(bird_present_col)

        bird_pres_pairs.append((
            "present" if truth_present else "not_present",
            "present" if bird_present else "not_present",
        ))
        if truth_present and bird_present and truth_eye in ("eyes_open", "eyes_closed"):
            pred_eye = row["shadow_birdeye_eye"]
            if pred_eye in ("eyes_open", "eyes_closed"):
                bird_eye_pairs.append((truth_eye, pred_eye))

    pres_classes = ["not_present", "present"]
    eye_classes = ["eyes_open", "eyes_closed"]

    # ----- Windowed shadow production stats (detection rate, timing) -----
    # "has shadow data" == BIRDEYE ran on the frame, which is the `shadow_birdeye_present`
    # column being non-NULL (it's 0/1 when set, NULL when BIRDEYE didn't run).
    shadow_row = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN shadow_birdeye_present IS NOT NULL THEN 1 ELSE 0 END) as with_shadow
        FROM entries
        WHERE timestamp >= ? AND baby_present = 1
    """, (cutoff,)).fetchone()

    # ----- Deployed model version -----
    from .config import MODELS_DIR
    latest_link = MODELS_DIR / "latest"
    deployed_version = None
    try:
        if latest_link.is_symlink():
            deployed_version = Path(os.readlink(latest_link)).name
    except OSError:
        pass
    latest_trained = get_last_training_runs(1)
    latest_trained_version = latest_trained[0].get("version") if latest_trained else None

    # ----- Face detection stats (windowed) -----
    face_rows = conn.execute("""
        SELECT data FROM entries
        WHERE timestamp >= ?
          AND baby_present = 1
          AND shadow_birdeye_present IS NOT NULL
    """, (cutoff,)).fetchall()

    face_total = 0
    face_detected = 0
    face_confidences = []
    ious_windowed = []
    for row in face_rows:
        entry = json.loads(row["data"])
        face_total += 1
        fb = entry.get("faceBbox")
        if fb and isinstance(fb, dict) and "x1" in fb:
            face_detected += 1
            fc = entry.get("faceConfidence")
            if fc is not None:
                face_confidences.append(fc)
            # IoU vs user-drawn correction, when available.
            fb_corr = entry.get("faceBboxCorrected")
            if fb_corr and isinstance(fb_corr, dict) and "x1" in fb_corr:
                ious_windowed.append(_bbox_iou(fb, fb_corr))

    face_stats = {
        "total": face_total,
        "detected": face_detected,
        "detectionRate": round(face_detected / face_total, 3) if face_total > 0 else 0.0,
        "fallbackRate": round((face_total - face_detected) / face_total, 3) if face_total > 0 else 0.0,
    }
    if face_confidences:
        sc = sorted(face_confidences)
        face_stats["confidence"] = {
            "avg": round(sum(sc) / len(sc), 3),
            "min": round(sc[0], 3),
            "max": round(sc[-1], 3),
            "p50": round(sc[len(sc) // 2], 3),
        }

    # IoU vs dashboard-drawn corrections.  Two scopes:
    #   windowed — only frames inside the current range selector; small N but
    #              tracks the deployed model's current behavior
    #   allTime  — every frame ever corrected; larger N but mixes model versions
    # The dashboard shows both so a short window isn't silently blank when
    # the user just hasn't reviewed much lately.
    all_iou_rows = conn.execute("""
        SELECT data FROM entries
        WHERE data LIKE '%faceBboxCorrected%'
          AND data LIKE '%faceBbox%'
          AND baby_present = 1
    """).fetchall()
    ious_all = []
    for row in all_iou_rows:
        try:
            e = json.loads(row["data"])
        except (TypeError, json.JSONDecodeError):
            continue
        fb = e.get("faceBbox")
        fb_corr = e.get("faceBboxCorrected")
        if (fb and isinstance(fb, dict) and "x1" in fb
                and fb_corr and isinstance(fb_corr, dict) and "x1" in fb_corr):
            ious_all.append(_bbox_iou(fb, fb_corr))

    face_stats["iou"] = {
        "windowed": _summarize_iou(ious_windowed),
        "allTime": _summarize_iou(ious_all),
    }

    # Bbox-impact analysis: does running eye-state on the corrected bbox
    # produce a better answer than running it on BIRDEYE's predicted bbox?
    # Computed offline by scripts/bbox_impact.py and cached in the state
    # table so we don't block API calls on torch inference. May be absent
    # if the script has never been run.
    bbox_impact = get_state("bbox_impact")
    if bbox_impact:
        face_stats["bboxImpact"] = bbox_impact

    # Shadow experiments: iterate the registry and aggregate per-experiment
    # stats. Wrapped in try/except so a broken experiment module can never
    # kill the entire safety-stats endpoint — the dashboard's other panels
    # must keep rendering even if experiments fail to load.
    experiment_stats_by_name: dict[str, dict] = {}
    try:
        from .experiments import get_registry as _get_experiment_registry
        for exp in _get_experiment_registry():
            stats = get_experiment_stats(exp.name, hours)
            # Include descriptive metadata from the Experiment instance so
            # the dashboard card doesn't have to fetch it separately.
            experiment_stats_by_name[exp.name] = {
                **stats,
                "description": exp.description,
            }
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("monitor").warning(
            "get_safety_stats: experiments framework failed: %s", e
        )

    return {
        "hours": hours,
        "shadowTotal": shadow_row["with_shadow"] if shadow_row else 0,
        "deployedVersion": deployed_version,
        "latestTrainedVersion": latest_trained_version,
        "rolledBack": (
            deployed_version is not None
            and latest_trained_version is not None
            and deployed_version != latest_trained_version
        ),
        "groundTruth": {
            "total": reviewed_count + corrected_count,
            "reviewed": reviewed_count,
            "corrected": corrected_count,
        },
        "presence": {
            "birdeyeVsGT": build_panel(bird_pres_pairs, pres_classes) if bird_pres_pairs else None,
            "cloudVsGT": build_panel(cloud_pres_pairs, pres_classes) if cloud_pres_pairs else None,
        },
        "eyeState": {
            "birdeyeVsGT": build_panel(bird_eye_pairs, eye_classes) if bird_eye_pairs else None,
            "cloudVsGT": build_panel(cloud_eye_pairs, eye_classes) if cloud_eye_pairs else None,
        },
        "faceDetection": face_stats,
        "experiments": experiment_stats_by_name,
    }


def get_experiment_stats(name: str, hours: float = 168) -> dict:
    """Aggregate metrics for a single shadow experiment over a time window.

    Produces the same shape the dashboard Experiments card expects:

    - ``count``: frames where the experiment produced a result
    - ``agreementWithProd``: fraction where experiment's eyeState matches
      prod's eyeState (both must have a binary eye label to count)
    - ``accuracyVsGT`` / ``accuracyProdVsGT``: for frames where the user has
      reviewed or corrected the label, how often each pipeline matches it
    - ``deltaVsProd``: (experiment accuracy - prod accuracy) on the GT set;
      >0 means the experiment is better, <0 means worse
    - ``perClass``: same split, keyed by ground-truth class
    - ``avgLatencyMs``: mean experiment latency
    - ``modelVersion``: most recent model version the experiment reported

    Returns a dict with ``count=0`` and null metrics when the experiment has
    no data in the window — safe for the dashboard to render as empty state.
    """
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    rows = conn.execute(
        """
        SELECT timestamp, eye_state, eye_state_edited, reviewed, baby_present,
               shadow_birdeye_eye, data
        FROM entries
        WHERE timestamp >= ?
          AND baby_present = 1
          AND data LIKE '%experiments%'
        """,
        (cutoff,),
    ).fetchall()

    count = 0
    count_no_prod = 0  # experiment ran but prod BIRDEYE didn't (pre-flip)
    agree_prod = 0
    agree_prod_denom = 0
    gt_count = 0
    correct_exp = 0
    correct_prod = 0
    latencies: list[float] = []
    last_version: str | None = None
    per_class: dict[str, dict] = {}

    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, json.JSONDecodeError):
            continue
        experiments = data.get("experiments") or {}
        exp_result = experiments.get(name)
        if not isinstance(exp_result, dict):
            continue

        exp_eye = exp_result.get("eyeState")
        # Prod prediction: must come from the BIRDEYE audit column
        # (shadow_birdeye_eye). Do NOT fall back to data.eyeState — on
        # reviewed/corrected frames that field holds the user-confirmed
        # ground-truth label, not a prediction, and using it as a proxy
        # for prod would artificially inflate prod's accuracy to 100%.
        prod_eye = row["shadow_birdeye_eye"]
        if not prod_eye:
            # Pre-BIRDEYE-flip frames: no prod prediction to compare
            # against. Still count the experiment as having run but
            # exclude from agreement/GT comparisons.
            count_no_prod += 1
            lat = exp_result.get("latencyMs")
            if isinstance(lat, (int, float)):
                latencies.append(lat)
            if exp_result.get("modelVersion"):
                last_version = exp_result["modelVersion"]
            continue

        count += 1

        if exp_eye in ("eyes_open", "eyes_closed") and prod_eye in ("eyes_open", "eyes_closed"):
            agree_prod_denom += 1
            if exp_eye == prod_eye:
                agree_prod += 1

        lat = exp_result.get("latencyMs")
        if isinstance(lat, (int, float)):
            latencies.append(lat)
        if exp_result.get("modelVersion"):
            last_version = exp_result["modelVersion"]

        # Ground-truth comparison — only frames where the user EXPLICITLY
        # CORRECTED the eye-state label (eye_state_edited=1). On review-only
        # frames (reviewed=1 without a correction), the stored eye_state is
        # whatever prod said at capture time, so comparing prod to it would
        # tautologically score 100% — the frame was only in the GT set
        # because prod wasn't wrong enough for the user to change it.
        # Restricting to corrections gives us a fair adversarial test set.
        if not row["eye_state_edited"]:
            continue
        gt = row["eye_state"] if row["eye_state"] in ("eyes_open", "eyes_closed") else None
        if not gt:
            continue
        gt_count += 1
        if exp_eye == gt:
            correct_exp += 1
        if prod_eye == gt:
            correct_prod += 1

        bucket = per_class.setdefault(
            gt, {"n": 0, "correctExp": 0, "correctProd": 0}
        )
        bucket["n"] += 1
        if exp_eye == gt:
            bucket["correctExp"] += 1
        if prod_eye == gt:
            bucket["correctProd"] += 1

    for cls_bucket in per_class.values():
        n = cls_bucket["n"]
        cls_bucket["accuracyExp"] = round(cls_bucket["correctExp"] / n, 3) if n else 0.0
        cls_bucket["accuracyProd"] = round(cls_bucket["correctProd"] / n, 3) if n else 0.0
        cls_bucket["delta"] = round(cls_bucket["accuracyExp"] - cls_bucket["accuracyProd"], 3)

    return {
        "name": name,
        "count": count,
        "countNoProd": count_no_prod,  # frames where exp ran but BIRDEYE didn't
        "windowHours": hours,
        "agreementWithProd": (
            round(agree_prod / agree_prod_denom, 3) if agree_prod_denom else None
        ),
        "agreementDenom": agree_prod_denom,
        "groundTruthCount": gt_count,
        "accuracyVsGT": round(correct_exp / gt_count, 3) if gt_count else None,
        "accuracyProdVsGT": round(correct_prod / gt_count, 3) if gt_count else None,
        "deltaVsProd": (
            round((correct_exp - correct_prod) / gt_count, 3) if gt_count else None
        ),
        "avgLatencyMs": (
            round(sum(latencies) / len(latencies), 2) if latencies else None
        ),
        "modelVersion": last_version,
        "perClass": per_class,
    }


def get_training_duration_stats() -> dict:
    """Aggregate training-run duration across all recorded runs.

    Returns {"count": N, "avg_seconds": float|None, "p99_seconds": float|None}.
    Only rows with a non-null duration_seconds are considered.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT duration_seconds FROM training_runs "
        "WHERE duration_seconds IS NOT NULL"
    ).fetchall()
    values = [row["duration_seconds"] for row in rows]
    if not values:
        return {"count": 0, "avg_seconds": None, "p99_seconds": None}

    avg = sum(values) / len(values)

    # Linear-interpolation percentile (numpy default).
    s = sorted(values)
    if len(s) == 1:
        p99 = s[0]
    else:
        k = (len(s) - 1) * 0.99
        lo = int(k)
        hi = min(lo + 1, len(s) - 1)
        p99 = s[lo] + (s[hi] - s[lo]) * (k - lo) if hi > lo else s[lo]

    return {"count": len(values), "avg_seconds": avg, "p99_seconds": p99}


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

    # Corrections & review
    insert_correction = staticmethod(insert_correction)
    get_pending_corrections_count = staticmethod(get_pending_corrections_count)
    get_pending_corrections = staticmethod(get_pending_corrections)
    mark_reviewed = staticmethod(mark_reviewed)
    get_reviewed_ground_truth = staticmethod(get_reviewed_ground_truth)

    # Training
    insert_training_run = staticmethod(insert_training_run)
    get_last_training_runs = staticmethod(get_last_training_runs)
    update_training_run_metrics = staticmethod(update_training_run_metrics)
    get_training_duration_stats = staticmethod(get_training_duration_stats)
    get_safety_stats = staticmethod(get_safety_stats)

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
