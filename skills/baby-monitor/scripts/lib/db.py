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

    # Idempotent migrations for existing DBs (added 2026-04-09: training timing)
    for col, decl in (
        ("duration_seconds", "REAL"),
        ("started_at", "TEXT"),
        ("finished_at", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE training_runs ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists

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
                   e.shadow_birdeye_state, e.shadow_prod_state,
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
                   e.shadow_birdeye_state, e.shadow_prod_state,
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
            "shadowBirdeyeState": row["shadow_birdeye_state"],
            "shadowProdState": row["shadow_prod_state"],
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
    """Compute BIRDEYE-vs-ground-truth safety/quality breakdown.

    Returns separate panels for the presence classifier and the eye-state
    classifier, each with two ground-truth sources:

      - vsCloud: windowed shadow data (entries.shadow_birdeye_state vs
        entries.shadow_prod_state). High-volume, noisy proxy.
      - vsCorrections: comes from the latest training_runs row's
        metrics.correction_agreement (populated by cmd_retrain). Low-volume,
        high-quality. Updates only at retrain time.

    This is the data backing the dashboard's "Safety" panel — the metrics
    that actually answer "is birdeye safe enough to promote?".
    """
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ----- Pull shadow rows in the window -----
    rows = conn.execute("""
        SELECT shadow_birdeye_state, shadow_prod_state
        FROM entries
        WHERE timestamp >= ?
          AND shadow_birdeye_state IS NOT NULL
          AND shadow_prod_state IS NOT NULL
    """, (cutoff,)).fetchall()

    # shadow_birdeye_state has 3 values: not_present | Asleep | Awake.
    # shadow_prod_state values are case-inconsistent: lowercase from
    # cmd_retrain re-inference, capitalized from monitor.py historical
    # writes. Lowercase everything before comparing.
    #
    # The eye-state panel is reported in raw eye-state domain (eyes_open /
    # eyes_closed), NOT in derived Awake/Asleep. Map historical state-domain
    # values via STATE_TO_EYE below. Unknown/Drowsy are excluded — they're
    # ambiguous and the 2-class classifier doesn't emit them.
    STATE_TO_EYE = {
        "asleep": "eyes_closed",
        "awake": "eyes_open",
    }

    presence_pairs: list[tuple[str, str]] = []
    eye_pairs: list[tuple[str, str]] = []

    for row in rows:
        bird = (row["shadow_birdeye_state"] or "").strip()
        prod_raw = (row["shadow_prod_state"] or "").strip()
        bird_lower = bird.lower()
        prod = prod_raw.lower()

        # Both sides: Unknown means present (baby visible, state uncertain).
        # BIRDEYE returns Unknown when face detection or eye-state fails but
        # presence classifier confirmed the baby is there.
        bird_present = bird_lower in ("asleep", "awake", "unknown")
        prod_present = prod in ("asleep", "awake", "unknown", "drowsy")

        presence_pairs.append((
            "present" if prod_present else "not_present",
            "present" if bird_present else "not_present",
        ))

        # Eye-state confusion: only over frames where both sides have a
        # definitive eye state (Awake or Asleep). Unknown/Drowsy are skipped.
        if bird_present and prod_present:
            prod_eye = STATE_TO_EYE.get(prod)
            bird_eye = STATE_TO_EYE.get(bird_lower)
            if prod_eye and bird_eye:
                eye_pairs.append((prod_eye, bird_eye))

    def build_panel(pairs: list[tuple[str, str]], classes: list[str]) -> dict:
        """Build a confusion + per-class P/R/F1 + macro-F1 panel."""
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

    presence_vs_cloud = build_panel(presence_pairs, ["not_present", "present"])
    eye_vs_cloud = build_panel(
        eye_pairs,
        ["eyes_open", "eyes_closed"],
    )

    # ----- Corrections-side: read from the *deployed* training run -----
    # The deployed model is whatever pipeline/models/latest points at, which
    # can differ from the most-recent training_runs row after a --rollback.
    # We surface both so the dashboard can flag a rolled-back state.
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

    # Find the training_runs row matching the deployed version. Falls back to
    # the latest trained row if the symlink is missing/broken.
    deployed_run = None
    if deployed_version:
        for row in conn.execute(
            "SELECT metrics FROM training_runs WHERE version = ? LIMIT 1",
            (deployed_version,),
        ):
            try:
                deployed_run = {"metrics": json.loads(row["metrics"]) if row["metrics"] else None}
            except (TypeError, json.JSONDecodeError):
                deployed_run = None
            break
    if deployed_run is None and latest_trained:
        deployed_run = latest_trained[0]
        deployed_version = deployed_version or latest_trained_version

    corr_data = None
    if deployed_run:
        metrics = deployed_run.get("metrics") or {}
        corr_data = metrics.get("correction_agreement")

    presence_vs_corrections = None
    eye_vs_corrections = None
    if corr_data:
        by_class = corr_data.get("by_class", {})

        # Presence-vs-corrections comes from the separate presence counters
        # that cmd_retrain/cmd_eval_corrections tally independently of eye
        # state. Reason: a frame where birdeye said eyes_closed but truth
        # was eyes_open is WRONG on eye state but CORRECT on presence
        # (both sides agree the baby is present). The old implementation
        # conflated these and under-reported presence accuracy by ~25%.
        pres_data = corr_data.get("presence")
        if pres_data:
            pres_cm = pres_data.get("confusion")
            if pres_cm:
                # Full confusion matrix — use build_panel for consistent format
                pres_classes = ["not_present", "present"]
                pres_cm_pairs = []
                for t in pres_classes:
                    row = pres_cm.get(t, {})
                    for p in pres_classes:
                        count = row.get(p, 0)
                        pres_cm_pairs.extend([(t, p)] * count)
                presence_vs_corrections = build_panel(pres_cm_pairs, pres_classes)
            else:
                # Legacy: only by_class correct/total
                pres_by_class = pres_data.get("by_class", {})
                pres_total_info = pres_data.get("total", {"correct": 0, "total": 0})
                pres_total = pres_total_info.get("total", 0) or 0
                pres_correct = pres_total_info.get("correct", 0) or 0
                pres_byclass = {
                    "not_present": dict(pres_by_class.get("not_present", {"correct": 0, "total": 0})),
                    "present":     dict(pres_by_class.get("present",     {"correct": 0, "total": 0})),
                }
                pres_recalls = []
                for c in ("not_present", "present"):
                    v = pres_byclass.get(c, {})
                    if v.get("total", 0) > 0:
                        pres_recalls.append(v["correct"] / v["total"])
                presence_vs_corrections = {
                    "byClass": pres_byclass,
                    "accuracy": round(pres_correct / pres_total, 3) if pres_total > 0 else 0.0,
                    "macroRecall": round(sum(pres_recalls) / len(pres_recalls), 3) if pres_recalls else 0.0,
                    "total": pres_total,
                }
        # If the training run predates the presence sub-key (correction_agreement
        # from before 2026-04-09), leave presence_vs_corrections as None —
        # re-run `monitor.py --eval-corrections` to populate it.

        # Eye state: 2-class (drops not_in_bassinet).
        # Use the confusion matrix if available (populated by --eval-corrections
        # after 2026-04-10), otherwise fall back to by_class correct/total.
        eye_classes_list = ["eyes_open", "eyes_closed"]
        eye_cm = corr_data.get("eye_confusion")
        if eye_cm:
            # Full confusion matrix available — use build_panel for consistent format
            eye_cm_pairs = []
            for t in eye_classes_list:
                row = eye_cm.get(t, {})
                for p in eye_classes_list:
                    count = row.get(p, 0)
                    eye_cm_pairs.extend([(t, p)] * count)
            eye_vs_corrections = build_panel(eye_cm_pairs, eye_classes_list)
        else:
            # Legacy: only by_class correct/total, no confusion matrix
            eye_byclass = {c: dict(by_class.get(c, {"correct": 0, "total": 0})) for c in eye_classes_list}
            eye_correct = sum(c["correct"] for c in eye_byclass.values())
            eye_total = sum(c["total"] for c in eye_byclass.values())
            eye_recalls = []
            for c in eye_classes_list:
                v = eye_byclass.get(c, {})
                if v.get("total", 0) > 0:
                    eye_recalls.append(v["correct"] / v["total"])
            eye_vs_corrections = {
                "byClass": eye_byclass,
                "accuracy": round(eye_correct / eye_total, 3) if eye_total > 0 else 0.0,
                "macroRecall": round(sum(eye_recalls) / len(eye_recalls), 3) if eye_recalls else 0.0,
                "total": eye_total,
            }

    # ----- Face detection stats -----
    # Query baby-present entries with shadow data in the window.
    # Use cloud API state as proxy ground truth for face visibility:
    #   Awake/Asleep → face expected (visible)
    #   Unknown/Drowsy → face not expected (not clearly visible)
    face_rows = conn.execute("""
        SELECT data, shadow_prod_state FROM entries
        WHERE timestamp >= ?
          AND baby_present = 1
          AND shadow_birdeye_state IS NOT NULL
    """, (cutoff,)).fetchall()

    face_total = 0
    face_detected = 0
    face_confidences = []
    # Confusion: (expected, detected) where both are "visible" or "not_visible"
    face_pairs = []

    for row in face_rows:
        entry = json.loads(row["data"])
        face_total += 1
        fb = entry.get("faceBbox")
        has_face = bool(fb and isinstance(fb, dict) and "x1" in fb)
        if has_face:
            face_detected += 1
            fc = entry.get("faceConfidence")
            if fc is not None:
                face_confidences.append(fc)

        # Ground truth: cloud API state determines if face was visible
        prod = (row["shadow_prod_state"] or "").strip().lower()
        face_expected = prod in ("awake", "asleep")
        face_pairs.append((
            "visible" if face_expected else "not_visible",
            "visible" if has_face else "not_visible",
        ))

    face_vs_cloud = build_panel(face_pairs, ["visible", "not_visible"])

    face_stats = {
        "total": face_total,
        "detected": face_detected,
        "detectionRate": round(face_detected / face_total, 3) if face_total > 0 else 0.0,
        "fallbackRate": round((face_total - face_detected) / face_total, 3) if face_total > 0 else 0.0,
        "vsCloud": face_vs_cloud,
    }
    if face_confidences:
        sc = sorted(face_confidences)
        face_stats["confidence"] = {
            "avg": round(sum(sc) / len(sc), 3),
            "min": round(sc[0], 3),
            "max": round(sc[-1], 3),
            "p50": round(sc[len(sc) // 2], 3),
        }

    return {
        "hours": hours,
        "shadowTotal": len(presence_pairs),
        "deployedVersion": deployed_version,
        "latestTrainedVersion": latest_trained_version,
        "rolledBack": (
            deployed_version is not None
            and latest_trained_version is not None
            and deployed_version != latest_trained_version
        ),
        "presence": {
            "vsCloud": presence_vs_cloud,
            "vsCorrections": presence_vs_corrections,
        },
        "eyeState": {
            "vsCloud": eye_vs_cloud,
            "vsCorrections": eye_vs_corrections,
        },
        "faceDetection": face_stats,
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

    # Corrections
    insert_correction = staticmethod(insert_correction)
    get_pending_corrections_count = staticmethod(get_pending_corrections_count)
    get_pending_corrections = staticmethod(get_pending_corrections)

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
