---
name: baby-report
description: Generate baby activity reports from tracked data (feeds, sleep, pumping, diapers, weight, monitor performance). Use when asked for a baby report, baby summary, activity summary, sleep report, feeding report, monitor performance, birdeye stats, model accuracy, API costs, or any time-ranged query about baby activities. Triggers on "baby report", "activity report", "weekly summary", "daily summary", "how did the baby sleep", "feeding summary", "diaper report", "monitor performance", "birdeye", "model stats", "API usage". Also use for ANY baby monitor or BIRDEYE-related questions — training results, face detection analysis, model performance, shadow mode metrics. This skill is the single entry point for all baby monitoring data.
---

# Baby Report

Generate consistent, accurate reports from these data sources:
1. **Activity CSV** (`baby-monitor/data/activity-log.csv`) — manual tracking: feeds, pumps, diapers, weight
2. **Baby-monitor dashboard HTTP API** (`http://localhost:5555/api/*`) — canonical source for the **Monitor** section (BIRDEYE classifier metrics, model deploy state, production decision counts, shadow stats, costs). baby-report is a thin client of these endpoints — there is no local SQL or F1 computation in the monitor section, so the agent reading the report sees the exact same numbers as the live dashboard UI.
3. **Monitor SQLite DB** (`baby-monitor/data/monitor.db`) — used only by the **Sleep** section, which iterates raw entries to compute in-bassinet stretches.
4. **Sleep monitor JSONL** (`baby-monitor/data/sleep-log.jsonl`) — append-only backup; used by the Sleep section only when the DB is missing.

**Data source rules:**
- **Monitor** → dashboard HTTP API (`/api/monitor-stats`, `/api/safety-stats`). Requires the dashboard launchd service to be running; the section returns a clear error otherwise.
- **Sleep** → SQLite when available; JSONL fallback.
- **Everything else** (feeding, pumping, diapers, weight) → activity CSV.

Override the dashboard URL with `BABY_REPORT_DASHBOARD_URL=http://host:port` if it's bound somewhere other than `http://localhost:5555`.

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

### Monitor (from dashboard HTTP API)
The monitor section is rendered from two dashboard endpoints — `lib/monitor.py` is a pure HTTP client of these. **No local F1 or confusion math.** Whatever the dashboard's BIRDEYE Classifiers card shows, the report shows.

**Monitor Performance** — windowed by `--range`, sourced from `GET /api/monitor-stats?hours=N`:
- Coverage (expected vs actual entries; 1 entry per 4 min).
- Production decision source breakdown: cloud API, pixel-diff, birdeye. BIRDEYE is normally 0% here because it runs in shadow mode — this line measures who *decided* each frame, not who classified it.
- API cost estimate (calls made vs avoided).
- BIRDEYE inference latency stats (avg, p50, p99, max).
- Presence and eye-classifier confidence stats.
- Cloud API model breakdown and capture-gap count.
- Shadow agreement rate (BIRDEYE vs prod state, when both produced one).

**BIRDEYE Classifiers** — sourced from `GET /api/safety-stats?hours=N`:
- Deployed model version (with rollback warning).
- Shadow frame count in window.
- Ground truth labels (reviewed + corrected counts).
- Face detection rate / fallback rate.
- **Presence panel** (`not_present` vs `present`): BIRDEYE and Cloud API macro-F1, accuracy, per-class P/R/F1 vs ground truth.
- **Eye-state panel** (`eyes_open` vs `eyes_closed`): same shape.

> ⚠ The dashboard's GT pairs are computed across **all** reviewed/corrected frames, not just the requested window. The classifier F1/accuracy numbers in the report are therefore lifetime metrics, not windowed. Only the production decision counts, latency, costs, and shadow frame count are scoped by `--range`.

**JSON output** (`--format json`) passes the dashboard responses through verbatim under the top-level `monitor` key, with two sub-objects: `monitor.monitor_stats` and `monitor.safety_stats`. This is intentionally a 1:1 mirror of the dashboard's API contract.

**Failure mode**: if the dashboard isn't running, the Monitor section (and the JSON `monitor` key) returns a clear error pointing at the launchctl command. There is no silent fallback to local SQL — that's how the two skills drift apart.

## CSV Column Mapping (important quirks)

For **Diaper** rows, columns are shifted:
- `Duration` → stool color (yellow/green/brown)
- `Start Condition` → consistency (Loose/Runny/Solid)
- `End Condition` → contents (Poo:small, Pee:medium, Both, etc.)

## Answering Baby Monitor Questions

For any question about training results, BIRDEYE performance, face detection, model accuracy, or monitoring data — **always use this skill exclusively**. Do not manually explore the baby-monitor directory, read sleep-log.jsonl directly, or run ad-hoc Python scripts outside of `report.py`.

The canonical workflow:
1. Determine the relevant time range and section
2. Run `report.py` with appropriate `--range` and `--section` flags
3. For training-specific questions, check `pipeline/models/training-log.jsonl` via `report.py --section monitor`
4. Present results from the report output only

**Working directory:** Always run from `skills/baby-report/` using the system Python (`python3`), not the baby-monitor venv.

## Updating Data

When the user sends a new activity CSV, save it to `baby-monitor/data/activity-log.csv` (overwrite). New CSVs may contain overlapping old data — the script deduplicates by timestamp so this is safe. The report script reads it fresh each run.
