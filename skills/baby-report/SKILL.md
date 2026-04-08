---
name: baby-report
description: Generate baby activity reports from tracked data (feeds, sleep, pumping, diapers, weight, monitor performance). Use when asked for a baby report, baby summary, activity summary, sleep report, feeding report, monitor performance, birdeye stats, model accuracy, API costs, or any time-ranged query about baby activities. Triggers on "baby report", "activity report", "weekly summary", "daily summary", "how did the baby sleep", "feeding summary", "diaper report", "monitor performance", "birdeye", "model stats", "API usage".
---

# Baby Report

Generate consistent, accurate reports from three data sources:
1. **Activity CSV** (`baby-monitor/data/activity-log.csv`) — manual tracking: feeds, pumps, diapers, weight
2. **Sleep monitor JSONL** (`baby-monitor/data/sleep-log.jsonl`) — camera-based bassinet monitoring
3. **Monitor performance** (same JSONL) — detection method usage, BIRDEYE classifier metrics, cloud API costs

**Data source rules:**
- **Sleep** → camera monitor JSONL when available (ground truth); falls back to CSV for days with no camera data
- **Everything else** (feeding, pumping, diapers, weight) → activity CSV
- **Monitor** → sleep-log.jsonl `detectionMethod` field distinguishes birdeye (local), cloud API, and pixel-diff entries

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

### Monitor (from JSONL)
BIRDEYE local classifier performance and cloud API usage. Reports:

- **Detection method breakdown** — percentage of frames handled by birdeye (local), cloud API, and pixel-diff
- **API cost estimate** — cloud API calls made vs avoided, estimated dollar savings
- **BIRDEYE confidence stats** — presence classifier and eye state classifier confidence distributions (avg, min, max, p50, p95)
- **BIRDEYE inference timing** — per-frame latency (avg, p50, p95)
- **BIRDEYE state distribution** — how many frames classified as Asleep, Awake, not_present
- **Cloud API model usage** — which models were called (gpt-4o-mini, gpt-4o, etc.)
- **Coverage gaps** — periods >10 min with no entries (cron missed or camera down)
- **State transitions** — birdeye Awake↔Asleep transitions (helps spot flip-flopping)
- **Alerts** — count and types of safety alerts triggered

The `analyze_monitor_entries()` function in `lib/monitor.py` is a pure function (no I/O) that accepts a list of entries and returns a structured dict. It is designed to be importable by the dashboard for real-time metrics:

```python
from lib.monitor import analyze_monitor_entries
metrics = analyze_monitor_entries(entries)
# metrics["birdeye"]["rate"]           → 0.98
# metrics["birdeye"]["timing"]["avg"]  → 0.041
# metrics["cost"]["est_saved"]         → 1.25
```

**JSON output** includes the full monitor metrics under the `monitor` key, with nested `birdeye`, `cloud_api`, `pixel_diff`, `gaps`, `alerts`, `transitions`, and `cost` objects.

## CSV Column Mapping (important quirks)

For **Diaper** rows, columns are shifted:
- `Duration` → stool color (yellow/green/brown)
- `Start Condition` → consistency (Loose/Runny/Solid)
- `End Condition` → contents (Poo:small, Pee:medium, Both, etc.)

## Updating Data

When the user sends a new activity CSV, save it to `baby-monitor/data/activity-log.csv` (overwrite). New CSVs may contain overlapping old data — the script deduplicates by timestamp so this is safe. The report script reads it fresh each run.
