---
name: baby-report
description: Generate baby activity reports from tracked data (feeds, sleep, pumping, diapers, weight, monitor performance). Use when asked for a baby report, baby summary, activity summary, sleep report, feeding report, monitor performance, birdeye stats, model accuracy, API costs, or any time-ranged query about baby activities. Triggers on "baby report", "activity report", "weekly summary", "daily summary", "how did the baby sleep", "feeding summary", "diaper report", "monitor performance", "birdeye", "model stats", "API usage".
---

# Baby Report

Generate consistent, accurate reports from three data sources:
1. **Activity CSV** (`baby-monitor/data/activity-log.csv`) — manual tracking: feeds, pumps, diapers, weight
2. **Monitor SQLite DB** (`baby-monitor/data/monitor.db`) — canonical source for camera entries, shadow-mode BIRDEYE metrics, corrections, and review state
3. **Sleep monitor JSONL** (`baby-monitor/data/sleep-log.jsonl`) — append-only backup; used only if the DB is missing

**Data source rules:**
- **Sleep / monitor** → SQLite when available (canonical); JSONL fallback
- **Everything else** (feeding, pumping, diapers, weight) → activity CSV

## Usage

```bash
python3 scripts/report.py --range 24h                # full report (all sections)
python3 scripts/report.py --range 7d
python3 scripts/report.py --from 2026-03-25 --to 2026-03-31
python3 scripts/report.py --range 24h --section sleep     # single section
python3 scripts/report.py --range 24h --section monitor   # model performance only
python3 scripts/report.py --range 7d --format json        # structured JSON output
```

Options:
- `--range`: Relative range — `24h`, `7d`, `2w`
- `--from`/`--to`: Absolute date range (YYYY-MM-DD)
- `--section`: Only one section — `sleep`, `feeding`, `pumping`, `diapers`, `weight`, `monitor`
- `--format`: `text` (default, markdown) or `json`
- `--csv`: Override path to activity CSV

## Report Sections

### Sleep (from camera)
In-bassinet time, daily breakdown, longest uninterrupted stretch, out-of-bassinet gaps.

### Feeding (from CSV)
Breast vs bottle counts, volumes, formula vs breast milk breakdown.

### Pumping (from CSV)
Sessions, total volume, averages.

### Diapers (from CSV)
Counts, poo/pee breakdown, stool colors.

### Weight (from CSV)
Weight measurements found in feed notes.

### Monitor (from SQLite)
Two distinct things are reported back-to-back:

**Monitor Performance** — production decision source:
- Coverage (expected vs actual entries; 1 entry per 4 min).
- Production decision source breakdown: cloud API, pixel-diff, birdeye. BIRDEYE is normally 0% here because it runs in shadow mode — this line measures who *decided* each frame, not who classified it. Once BIRDEYE is promoted out of shadow mode this will be non-zero.
- API cost estimate (calls made vs avoided).
- Cloud API model breakdown, coverage gaps >10 min, and alert count.

**BIRDEYE Shadow Performance** — shown whenever entries carry shadow fields:
- Shadow model version(s) in use.
- Per-stage inference latency (`shadow.birdeyeTimings.total`): avg, p50, p95, max.
- Presence and eye-classifier confidence stats (from `shadow.presenceConfidence` / `shadow.eyeConfidence`).
- Fallback usage (when trainable face detector bails and YuNet or head-position crop is used).

**Eye-state (eyes_open vs eyes_closed)** — the only label dimension reported. Body-state (Asleep/Awake) analysis is intentionally omitted because the Asleep/Awake taxonomy is being redefined upstream.
- Alignment vs prod on paired frames (frames where BOTH prod and BIRDEYE produced one of `eyes_open` / `eyes_closed`).
- 2×2 confusion matrix.
- Per-class P/R/F1 for `eyes_open` and `eyes_closed` (small-sample warning when n<20).
- **Ground truth** (when corrected or reviewed frames exist in window):
    - Accuracy plus per-class P/R/F1 restricted to the ground-truth slice.
    - GT sourced from the DB's authoritative `eye_state` column, which is overwritten on dashboard correction (`eye_state_edited=1`) and confirmed on review (`reviewed=1`).
- **Diagnostics** — rows that fell outside the paired set:
    - `unclassified`: prod had a real eye label, BIRDEYE returned `None` (face detection / classifier failure).
    - `hallucinated`: prod said `face_not_visible` / `not_in_bassinet`, but BIRDEYE returned an eye label anyway.
    - `declined_correctly`: prod and BIRDEYE both declined to classify.

**JSON output** (`--format json`) returns the full structured metrics under `monitor`, with the shadow-mode block under `monitor.shadow`. Top-level shadow keys: `count`, `confidence`, `timing_total`, `fallbacks`, `model_versions`, `eye_state`. The `eye_state` block has sub-keys `paired_count`, `alignment`, `confusion`, `per_class`, `ground_truth`, `diagnostics`.

The `analyze_monitor_entries()` and `analyze_shadow_performance()` functions in `lib/monitor.py` are pure — they take a list of entries and return the structured dicts above, so the dashboard or other tools can import them directly.

## CSV Column Mapping (important quirks)

For **Diaper** rows, columns are shifted:
- `Duration` → stool color (yellow/green/brown)
- `Start Condition` → consistency (Loose/Runny/Solid)
- `End Condition` → contents (Poo:small, Pee:medium, Both, etc.)

## Updating Data

When the user sends a new activity CSV, save it to `baby-monitor/data/activity-log.csv` (overwrite). New CSVs may contain overlapping old data — the script deduplicates by timestamp so this is safe. The report script reads it fresh each run.
