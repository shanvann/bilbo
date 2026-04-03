---
name: baby-report
description: Generate baby activity reports from tracked data (feeds, sleep, pumping, diapers, weight). Use when asked for a baby report, baby summary, activity summary, sleep report, feeding report, or any time-ranged query about baby activities. Triggers on "baby report", "activity report", "weekly summary", "daily summary", "how did the baby sleep", "feeding summary", "diaper report".
---

# Baby Report

Generate consistent, accurate reports from two data sources:
1. **Activity CSV** (`baby-monitor/data/activity-log.csv`) — manual tracking: feeds, pumps, diapers, weight
2. **Sleep monitor JSONL** (`baby-monitor/data/sleep-log.jsonl`) — camera-based bassinet monitoring

**Data source rules:**
- **Sleep** → camera monitor JSONL when available (ground truth); falls back to CSV for days with no camera data
- **Everything else** (feeding, pumping, diapers, weight) → activity CSV

The CSV may log longer sleep blocks that include feeding gaps. The camera data catches those interruptions. The script auto-detects which days have camera data and uses CSV as fallback per-day.

## Usage

```bash
python3 scripts/report.py --range 24h
python3 scripts/report.py --range 7d
python3 scripts/report.py --from 2026-03-25 --to 2026-03-31
python3 scripts/report.py --range 24h --section sleep
python3 scripts/report.py --range 7d --format json
```

Options:
- `--range`: Relative range — `24h`, `7d`, `2w`
- `--from`/`--to`: Absolute date range (YYYY-MM-DD)
- `--section`: Only one section — `sleep`, `feeding`, `pumping`, `diapers`, `weight`
- `--format`: `text` (default, markdown) or `json`
- `--csv`: Override path to activity CSV

## Report Sections

- **Sleep** (from camera): In-bassinet time, daily breakdown, longest uninterrupted stretch, out-of-bassinet gaps
- **Feeding** (from CSV): Breast vs bottle counts, volumes, formula vs breast milk breakdown
- **Pumping** (from CSV): Sessions, total volume, averages
- **Diapers** (from CSV): Counts, poo/pee breakdown, stool colors
- **Weight** (from CSV): Weight measurements found in feed notes

## CSV Column Mapping (important quirks)

For **Diaper** rows, columns are shifted:
- `Duration` → stool color (yellow/green/brown)
- `Start Condition` → consistency (Loose/Runny/Solid)
- `End Condition` → contents (Poo:small, Pee:medium, Both, etc.)

## Updating Data

When the user sends a new activity CSV, save it to `baby-monitor/data/activity-log.csv` (overwrite). New CSVs may contain overlapping old data — the script deduplicates by timestamp so this is safe. The report script reads it fresh each run.
