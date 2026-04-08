#!/usr/bin/env python3
"""Birdeye performance report — analyze detection method usage and model accuracy.

Parses sleep-log.jsonl and system.log to report on how birdeye (local classifier)
and cloud API are performing over a given time range.

Usage:
    # Last 6 hours (default)
    python report.py

    # Last N hours
    python report.py --hours 24
    python report.py --hours 168   # last week

    # Date range
    python report.py --from 2026-04-07 --to 2026-04-08

    # Since a date
    python report.py --from 2026-04-01

    # Verbose (show per-entry details)
    python report.py --hours 12 --verbose

    # JSON output (for piping/dashboards)
    python report.py --hours 24 --json
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
JSONL_FILE = DATA_DIR / "sleep-log.jsonl"
SYSTEM_LOG = DATA_DIR / "system.log"


def parse_ts(ts_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def load_entries(start: datetime, end: datetime) -> list[dict]:
    if not JSONL_FILE.exists():
        return []
    entries = []
    for line in JSONL_FILE.read_text().strip().splitlines():
        if not line:
            continue
        e = json.loads(line)
        ts = parse_ts(e.get("timestamp", ""))
        if ts and start <= ts <= end:
            e["_ts"] = ts
            entries.append(e)
    return entries


def load_run_summaries(start: datetime, end: datetime) -> list[dict]:
    """Parse RUN_SUMMARY lines from system.log."""
    if not SYSTEM_LOG.exists():
        return []
    summaries = []
    for line in SYSTEM_LOG.read_text().strip().splitlines():
        if "RUN_SUMMARY" not in line:
            continue
        # Parse: [2026-04-08T16:07:03Z] monitor: RUN_SUMMARY method=... state=... ...
        try:
            ts_str = line.split("]")[0].lstrip("[")
            ts = parse_ts(ts_str)
            if not ts or not (start <= ts <= end):
                continue
            kv_part = line.split("RUN_SUMMARY ")[1]
            d = {"_ts": ts}
            for pair in kv_part.split():
                k, _, v = pair.partition("=")
                d[k] = v
            summaries.append(d)
        except (IndexError, ValueError):
            continue
    return summaries


def fmt_pct(n: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{n * 100 / total:.1f}%"


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def report(entries: list[dict], summaries: list[dict], start: datetime, end: datetime,
           verbose: bool = False, as_json: bool = False):
    """Generate and print the performance report."""

    if not entries:
        print("No entries found in the specified time range.")
        return

    # --- Time range ---
    span_hours = (end - start).total_seconds() / 3600
    actual_first = entries[0]["_ts"]
    actual_last = entries[-1]["_ts"]

    # --- Detection method breakdown ---
    methods = Counter(e.get("detectionMethod", "unknown") for e in entries)
    total = len(entries)

    birdeye_entries = [e for e in entries if e.get("detectionMethod") == "birdeye"]
    cloud_entries = [e for e in entries if e.get("detectionMethod") in ("vision-api", "openai-vision")]
    pixel_diff_entries = [e for e in entries if e.get("detectionMethod") == "pixel-diff"]
    other_entries = [e for e in entries if e.get("detectionMethod") not in
                     ("birdeye", "vision-api", "openai-vision", "pixel-diff")]

    # --- State distribution ---
    states = Counter()
    for e in entries:
        if not e.get("babyPresent", False):
            states["not_present"] += 1
        else:
            states[e.get("state", "Unknown")] += 1

    # --- Birdeye stats ---
    birdeye_states = Counter()
    birdeye_confidences = {"presence": [], "eye": []}
    birdeye_timings = []

    for e in birdeye_entries:
        if not e.get("babyPresent", False):
            birdeye_states["not_present"] += 1
        else:
            birdeye_states[e.get("state", "Unknown")] += 1
        if e.get("presenceConfidence") is not None:
            birdeye_confidences["presence"].append(e["presenceConfidence"])
        if e.get("eyeConfidence") is not None:
            birdeye_confidences["eye"].append(e["eyeConfidence"])
        t = e.get("birdeyeTimings", {})
        if "total" in t:
            birdeye_timings.append(t["total"])

    # --- Cloud API stats ---
    cloud_models = Counter(e.get("modelUsed", "unknown") for e in cloud_entries)
    cloud_states = Counter()
    for e in cloud_entries:
        if not e.get("babyPresent", False):
            cloud_states["not_present"] += 1
        else:
            cloud_states[e.get("state", "Unknown")] += 1

    # --- Gap detection ---
    gaps = []
    for i in range(1, len(entries)):
        gap = (entries[i]["_ts"] - entries[i - 1]["_ts"]).total_seconds()
        if gap > 600:  # > 10 min
            gaps.append({
                "start": entries[i - 1]["_ts"],
                "end": entries[i]["_ts"],
                "minutes": gap / 60,
            })

    # --- Alert stats ---
    alert_entries = [e for e in entries if e.get("alerts")]

    # --- Expected runs ---
    expected_runs = int(span_hours * 60 / 4)  # every 4 min

    # --- Birdeye vs Cloud agreement (entries where birdeye decided same state as cloud would) ---
    # We can't know this directly, but we can compare state distributions

    if as_json:
        report_data = {
            "range": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "hours": round(span_hours, 1),
            },
            "total_entries": total,
            "expected_runs": expected_runs,
            "coverage": round(total / max(expected_runs, 1), 3),
            "methods": dict(methods),
            "states": dict(states),
            "birdeye": {
                "count": len(birdeye_entries),
                "rate": round(len(birdeye_entries) / max(total, 1), 3),
                "states": dict(birdeye_states),
                "timing_avg": round(sum(birdeye_timings) / max(len(birdeye_timings), 1), 3) if birdeye_timings else None,
                "confidence_presence_avg": round(sum(birdeye_confidences["presence"]) / max(len(birdeye_confidences["presence"]), 1), 3) if birdeye_confidences["presence"] else None,
                "confidence_eye_avg": round(sum(birdeye_confidences["eye"]) / max(len(birdeye_confidences["eye"]), 1), 3) if birdeye_confidences["eye"] else None,
            },
            "cloud_api": {
                "count": len(cloud_entries),
                "models": dict(cloud_models),
                "states": dict(cloud_states),
            },
            "pixel_diff": {"count": len(pixel_diff_entries)},
            "gaps": [{"start": g["start"].isoformat(), "end": g["end"].isoformat(), "minutes": round(g["minutes"], 1)} for g in gaps],
            "alerts": len(alert_entries),
        }
        print(json.dumps(report_data, indent=2))
        return

    # --- Print report ---
    print(f"{'=' * 65}")
    print(f"BIRDEYE PERFORMANCE REPORT")
    print(f"{'=' * 65}")
    print(f"Range:    {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC ({span_hours:.0f}h)")
    print(f"Entries:  {total} (expected ~{expected_runs} at 4-min intervals)")
    print(f"Coverage: {fmt_pct(total, expected_runs)} of expected runs")
    print()

    # Detection methods
    print(f"--- Detection Method Breakdown ---")
    print(f"  {'birdeye (local)':>20s}: {len(birdeye_entries):>5d}  ({fmt_pct(len(birdeye_entries), total):>6s})")
    print(f"  {'cloud API':>20s}: {len(cloud_entries):>5d}  ({fmt_pct(len(cloud_entries), total):>6s})")
    print(f"  {'pixel-diff (skip)':>20s}: {len(pixel_diff_entries):>5d}  ({fmt_pct(len(pixel_diff_entries), total):>6s})")
    if other_entries:
        for method, count in methods.items():
            if method not in ("birdeye", "vision-api", "openai-vision", "pixel-diff"):
                print(f"  {method:>20s}: {count:>5d}  ({fmt_pct(count, total):>6s})")
    print()

    # API savings
    api_saved = len(birdeye_entries) + len(pixel_diff_entries)
    est_cost = len(cloud_entries) * 0.01
    est_saved = api_saved * 0.01
    print(f"  Cloud API calls:   {len(cloud_entries)} (est. ${est_cost:.2f})")
    print(f"  API calls avoided: {api_saved} (est. ${est_saved:.2f} saved)")
    print()

    # State distribution
    print(f"--- Baby State Distribution ---")
    for state in ["Asleep", "Awake", "not_present", "Unknown", "Drowsy"]:
        count = states.get(state, 0)
        if count > 0:
            print(f"  {state:>15s}: {count:>5d}  ({fmt_pct(count, total):>6s})")
    print()

    # Birdeye performance
    if birdeye_entries:
        print(f"--- Birdeye Performance ---")
        print(f"  Entries decided locally: {len(birdeye_entries)}")
        for state, count in sorted(birdeye_states.items(), key=lambda x: -x[1]):
            print(f"    {state:>15s}: {count:>5d}")

        if birdeye_confidences["presence"]:
            pc = birdeye_confidences["presence"]
            print(f"  Presence confidence:  avg={sum(pc)/len(pc):.3f}  "
                  f"min={min(pc):.3f}  max={max(pc):.3f}")

        if birdeye_confidences["eye"]:
            ec = birdeye_confidences["eye"]
            print(f"  Eye state confidence: avg={sum(ec)/len(ec):.3f}  "
                  f"min={min(ec):.3f}  max={max(ec):.3f}")

        if birdeye_timings:
            ts = sorted(birdeye_timings)
            avg = sum(ts) / len(ts)
            p50 = ts[len(ts) // 2]
            p95 = ts[int(len(ts) * 0.95)]
            print(f"  Inference timing:     avg={avg:.3f}s  p50={p50:.3f}s  p95={p95:.3f}s")
        print()

    # Cloud API breakdown
    if cloud_entries:
        print(f"--- Cloud API Usage ---")
        print(f"  Total calls: {len(cloud_entries)}")
        for model, count in cloud_models.most_common():
            print(f"    {model:>30s}: {count:>5d}")
        print(f"  States returned:")
        for state, count in sorted(cloud_states.items(), key=lambda x: -x[1]):
            print(f"    {state:>15s}: {count:>5d}")
        print()

    # Gaps
    if gaps:
        print(f"--- Gaps (>10 min) ---")
        for g in gaps[:10]:
            print(f"  {g['start'].strftime('%m/%d %H:%M')} → "
                  f"{g['end'].strftime('%H:%M')} UTC  ({g['minutes']:.0f} min)")
        if len(gaps) > 10:
            print(f"  ... and {len(gaps) - 10} more")
        print()

    # Alerts
    if alert_entries:
        print(f"--- Alerts ---")
        print(f"  Total entries with alerts: {len(alert_entries)}")
        all_alerts = []
        for e in alert_entries:
            all_alerts.extend(e.get("alerts", []))
        alert_types = Counter(all_alerts)
        for alert, count in alert_types.most_common():
            print(f"    {alert}: {count}")
        print()

    # Birdeye state transitions (awake<->asleep)
    birdeye_with_baby = [e for e in birdeye_entries if e.get("babyPresent")]
    if len(birdeye_with_baby) >= 2:
        transitions = Counter()
        for i in range(1, len(birdeye_with_baby)):
            prev_state = birdeye_with_baby[i - 1].get("state", "Unknown")
            curr_state = birdeye_with_baby[i].get("state", "Unknown")
            if prev_state != curr_state:
                transitions[(prev_state, curr_state)] += 1
        if transitions:
            print(f"--- Birdeye State Transitions ---")
            for (prev, curr), count in transitions.most_common():
                print(f"  {prev:>10s} → {curr:<10s}: {count}")
            print()

    # Verbose: per-entry details
    if verbose:
        print(f"--- Per-Entry Details (last 50) ---")
        for e in entries[-50:]:
            ts = e["_ts"].strftime("%m/%d %H:%M")
            method = e.get("detectionMethod", "?")[:8]
            state = e.get("state", "?")
            baby = "Y" if e.get("babyPresent") else "N"
            conf = ""
            if e.get("presenceConfidence") is not None:
                conf += f" p={e['presenceConfidence']:.2f}"
            if e.get("eyeConfidence") is not None:
                conf += f" e={e['eyeConfidence']:.2f}"
            timing = ""
            bt = e.get("birdeyeTimings", {})
            if "total" in bt:
                timing = f" {bt['total']:.2f}s"
            alerts = f" !!{len(e.get('alerts', []))}" if e.get("alerts") else ""
            print(f"  {ts}  {method:<10s} baby={baby} state={state:<12s}{conf}{timing}{alerts}")


def main():
    parser = argparse.ArgumentParser(description="Birdeye performance report")
    parser.add_argument("--hours", type=float, default=6, help="Report on last N hours (default: 6)")
    parser.add_argument("--from", dest="from_date", help="Start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM)")
    parser.add_argument("--to", dest="to_date", help="End date (YYYY-MM-DD or YYYY-MM-DDTHH:MM)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-entry details")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    if args.from_date:
        start = parse_ts(args.from_date) or parse_ts(args.from_date + "T00:00:00Z")
        if start is None:
            print(f"Cannot parse --from date: {args.from_date}", file=sys.stderr)
            return 1
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    else:
        start = now - timedelta(hours=args.hours)

    if args.to_date:
        end = parse_ts(args.to_date) or parse_ts(args.to_date + "T23:59:59Z")
        if end is None:
            print(f"Cannot parse --to date: {args.to_date}", file=sys.stderr)
            return 1
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
    else:
        end = now

    entries = load_entries(start, end)
    summaries = load_run_summaries(start, end)
    report(entries, summaries, start, end, verbose=args.verbose, as_json=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
