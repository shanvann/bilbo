"""CLI commands: cmd_last, cmd_backtest, cmd_status, arg parsing."""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    BURST_AWAKE_THRESHOLD,
    BURST_CONFIRM_COUNT,
    FRAMES_DIR,
    JSONL_FILE,
    LOG_FILE,
    MODEL_CHAIN,
    PIXEL_DIFF_THRESHOLD,
    WAKE_COOLDOWN_MIN,
    WAKE_WINDOW,
)
from .detect import compute_diff_score
from .storage import read_all_entries

log = logging.getLogger("monitor")


def cmd_last(n: int):
    if not JSONL_FILE.exists():
        print("No log file found.", file=sys.stderr)
        return 1
    lines = JSONL_FILE.read_text().strip().splitlines()
    entries = [json.loads(l) for l in lines[-n:]]
    for e in entries:
        ts = e["timestamp"]
        present = e.get("babyPresent", "?")
        position = e.get("sleepPosition", "?")
        state = e.get("state", "?")
        alerts = e.get("alerts", [])
        alert_str = f"  !! {', '.join(alerts)}" if alerts else ""
        print(f"  {ts}  present={present}  position={position}  state={state}{alert_str}")
    return 0


def cmd_backtest(last_n: int = None, from_date: str = None, quick: bool = False,
                  alerts: bool = False):
    """Replay historical JSONL entries to test current detection logic.

    --quick: Only tests the pixel-diff gate (no API calls).
    --alerts: Test active wake detection and show when alerts would fire.
    """
    if not JSONL_FILE.exists():
        print("No JSONL log file found.", file=sys.stderr)
        return 1

    entries = read_all_entries()

    # Filter
    if from_date:
        entries = [e for e in entries if e.get("timestamp", "") >= from_date]
    if last_n:
        entries = entries[-last_n:]

    if not entries:
        print("No entries to backtest.", file=sys.stderr)
        return 1

    # Filter to entries with existing frame files
    valid = []
    skipped_missing = 0
    for e in entries:
        frame = e.get("frame", "")
        if frame and Path(frame).exists():
            valid.append(e)
        else:
            skipped_missing += 1

    if not valid:
        print(f"No entries with existing frame files (skipped {skipped_missing} missing).", file=sys.stderr)
        return 1

    print(f"Backtesting {len(valid)} entries ({skipped_missing} skipped — frame missing)")
    print(f"Mode: {'quick (pixel-diff only)' if quick else 'full pipeline'}")
    print()

    # Simulate pipeline
    stats = {
        "total": len(valid),
        "api_called": 0,
        "api_skipped": 0,
        "correct_skip": 0,       # skipped API, original also said empty
        "correct_call": 0,       # called API, original said baby present
        "false_skip": 0,         # skipped API but original said baby present (DANGEROUS)
        "unnecessary_call": 0,   # called API but original said empty (wasteful but safe)
        "false_skip_details": [],
    }

    prev_entry = None

    for i, entry in enumerate(valid):
        frame_path = Path(entry["frame"])
        original_present = entry.get("babyPresent", True)

        # Simulate pixel-diff gate
        would_skip = False
        diff_score = -1

        if prev_entry is not None and not prev_entry.get("_simulated_present", prev_entry.get("babyPresent", True)):
            prev_frame = prev_entry.get("frame", "")
            if prev_frame and Path(prev_frame).exists():
                diff_score = compute_diff_score(frame_path, Path(prev_frame))
                if 0 <= diff_score < PIXEL_DIFF_THRESHOLD:
                    would_skip = True

        if would_skip:
            stats["api_skipped"] += 1
            if original_present:
                stats["false_skip"] += 1
                stats["false_skip_details"].append({
                    "frame": str(frame_path),
                    "timestamp": entry.get("timestamp"),
                    "diff_score": round(diff_score, 2),
                    "original_state": entry.get("state"),
                    "original_position": entry.get("sleepPosition"),
                })
            else:
                stats["correct_skip"] += 1
            # For simulation: this entry would be logged as empty
            entry["_simulated_present"] = False
        else:
            stats["api_called"] += 1
            if original_present:
                stats["correct_call"] += 1
            else:
                stats["unnecessary_call"] += 1
            entry["_simulated_present"] = original_present

        prev_entry = entry

    # Report
    pct_saved = stats["api_skipped"] * 100 / stats["total"] if stats["total"] else 0
    print(f"{'='*60}")
    print(f"BACKTEST RESULTS (threshold={PIXEL_DIFF_THRESHOLD})")
    print(f"{'='*60}")
    print(f"Total frames:        {stats['total']}")
    print(f"API calls:           {stats['api_called']} ({stats['api_called']*100/stats['total']:.0f}%)")
    print(f"API skipped:         {stats['api_skipped']} ({pct_saved:.0f}% savings)")
    print()
    print(f"Correct skips:       {stats['correct_skip']}  (empty→empty, saved API ✅)")
    print(f"Correct calls:       {stats['correct_call']}  (baby present, API called ✅)")
    print(f"Unnecessary calls:   {stats['unnecessary_call']}  (empty but API called — safe, just wasteful)")
    print(f"FALSE SKIPS:         {stats['false_skip']}  (baby present but API skipped ⚠️)")

    if stats["false_skip_details"]:
        print()
        print("⚠️  FALSE SKIP DETAILS (review these frames!):")
        for d in stats["false_skip_details"]:
            print(f"  {d['timestamp']}  score={d['diff_score']}  "
                  f"state={d['original_state']}  pos={d['original_position']}")
            print(f"    frame: {d['frame']}")

    est_cost_saved = stats["api_skipped"] * 0.01
    print()
    print(f"Estimated savings:   ~${est_cost_saved:.2f} ({stats['api_skipped']} calls × $0.01)")

    # --- Alert backtest (burst simulation) ---
    if alerts:
        print()
        print(f"{'='*60}")
        print(f"ACTIVE WAKE ALERT BACKTEST (burst confirmation, {BURST_AWAKE_THRESHOLD}/{BURST_CONFIRM_COUNT+1} Awake required)")
        print(f"{'='*60}")

        alert_events = []
        burst_triggers = 0
        last_alert_ts = None

        for i in range(WAKE_WINDOW, len(valid)):
            entry = valid[i]
            if not entry.get("babyPresent"):
                last_alert_ts = None
                continue

            # Stage 1: would burst trigger?
            if entry.get("state") != "Awake":
                continue

            # Must have prior Asleep in window
            window = valid[max(0, i - WAKE_WINDOW + 1):i]
            window_present = [e for e in window if e.get("babyPresent")]
            if not any(e.get("state") == "Asleep" for e in window_present):
                continue

            # Cooldown check
            entry_ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
            if last_alert_ts:
                elapsed = (entry_ts - last_alert_ts).total_seconds() / 60
                if elapsed < WAKE_COOLDOWN_MIN:
                    continue

            burst_triggers += 1

            # Stage 2: simulate burst by looking at next 2 entries
            # (approximation — real burst would capture at 60s intervals)
            burst_states = [entry.get("state", "Unknown")]
            for j in range(i + 1, min(i + 3, len(valid))):
                if valid[j].get("babyPresent"):
                    burst_states.append(valid[j].get("state", "Unknown"))
                else:
                    burst_states.append("Unknown")

            awake_in_burst = burst_states.count("Awake")
            confirmed = awake_in_burst >= BURST_AWAKE_THRESHOLD

            if not confirmed:
                continue

            # Find when baby was actually removed
            removed_delta = None
            for j in range(i + 1, len(valid)):
                if not valid[j].get("babyPresent"):
                    rt = datetime.fromisoformat(valid[j]["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
                    removed_delta = (rt - entry_ts).total_seconds() / 60
                    break

            tp = removed_delta is not None and removed_delta <= 30

            alert_events.append({
                "timestamp": entry["timestamp"],
                "burst_states": burst_states,
                "awake_count": awake_in_burst,
                "removed_after": f"{removed_delta:.0f}min" if removed_delta else "N/A",
                "true_positive": tp,
            })
            last_alert_ts = entry_ts

        print(f"\nBurst triggers: {burst_triggers}")
        print(f"Alerts confirmed: {len(alert_events)}")
        print(f"Bursts suppressed: {burst_triggers - len(alert_events)}")

        if alert_events:
            tp = sum(1 for a in alert_events if a["true_positive"])
            fp = sum(1 for a in alert_events if not a["true_positive"])
            prec = tp * 100 // len(alert_events) if alert_events else 0
            print(f"True positive: {tp}  False positive: {fp}  Precision: {prec}%")
            print()
            for a in alert_events:
                ts = a["timestamp"]
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    import zoneinfo as _zi
                    local = dt.astimezone(_zi.ZoneInfo("America/New_York")).strftime("%m/%d %I:%M %p")
                except Exception:
                    local = ts
                tp_str = "✅ TP" if a["true_positive"] else "❌ FP"
                print(f"  {local}  burst={a['burst_states']}  "
                      f"awake={a['awake_count']}/3  removed={a['removed_after']}  {tp_str}")
        else:
            print("\nNo alerts would have fired (all bursts suppressed).")

    return 0 if stats["false_skip"] == 0 else 1


def cmd_status():
    # JSONL stats
    if JSONL_FILE.exists():
        lines = JSONL_FILE.read_text().strip().splitlines()
        entries = [json.loads(l) for l in lines]
        timestamps = [datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) for e in entries]
        now = datetime.now(timezone.utc)
        last_ts = timestamps[-1]
        age = now - last_ts
        age_min = age.total_seconds() / 60

        print(f"Log entries:    {len(entries)}")
        print(f"First entry:    {timestamps[0].isoformat()}")
        print(f"Last entry:     {last_ts.isoformat()}")
        print(f"Last entry age: {age_min:.0f} min ago")

        # Gaps > 10 min in last 24 hours
        cutoff = now - __import__("datetime").timedelta(hours=24)
        recent = [t for t in timestamps if t >= cutoff]
        gaps = []
        for i in range(1, len(recent)):
            gap_min = (recent[i] - recent[i - 1]).total_seconds() / 60
            if gap_min > 10:
                gaps.append((recent[i - 1], recent[i], gap_min))
        if gaps:
            print(f"\nGaps > 10 min (last 24h): {len(gaps)}")
            for start, end, minutes in gaps[:10]:
                print(f"  {start.strftime('%H:%M')} -> {end.strftime('%H:%M')} UTC  ({minutes:.0f} min)")
            if len(gaps) > 10:
                print(f"  ... and {len(gaps) - 10} more")
        else:
            print("\nNo gaps > 10 min in last 24h")
    else:
        print("No JSONL log file found.")

    # Frames dir
    if FRAMES_DIR.exists():
        frames = list(FRAMES_DIR.glob("frame_*.jpg"))
        total_mb = sum(f.stat().st_size for f in frames) / (1024 * 1024)
        print(f"\nFrames:         {len(frames)} files, {total_mb:.0f} MB")
    else:
        print("\nFrames dir not found.")

    # System log tail
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text().strip().splitlines()
        print(f"\nSystem log (last 5):")
        for line in lines[-5:]:
            print(f"  {line}")

    return 0


def parse_args():
    p = argparse.ArgumentParser(
        description="Baby monitor: capture, analyze, and log bassinet frames.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s                        full pipeline (cron mode)
  %(prog)s --capture-only         grab a frame and print its path
  %(prog)s --analyze frame.jpg    analyze an existing frame
  %(prog)s --dry-run              full pipeline, skip JSONL write
  %(prog)s --last 5               show last 5 log entries
  %(prog)s --status               system health overview
""",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--capture-only", action="store_true",
                      help="capture a frame and print the path, then exit")
    mode.add_argument("--analyze", metavar="FILE",
                      help="skip capture, analyze an existing frame image")
    mode.add_argument("--last", metavar="N", type=int,
                      help="show last N entries from the JSONL log")
    mode.add_argument("--status", action="store_true",
                      help="print system health: log stats, gaps, disk usage")
    mode.add_argument("--backtest", action="store_true",
                      help="replay historical frames to test detection logic")
    mode.add_argument("--feedback", nargs=2, metavar=("ALERT_ID", "YES_OR_NO"),
                      help="record feedback for an alert: --feedback <id> yes|no")
    mode.add_argument("--alert-stats", action="store_true",
                      help="show alert accuracy stats from user feedback")
    p.add_argument("--dry-run", action="store_true",
                   help="run full pipeline but do not write to the JSONL log")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print all log messages to stderr (not just warnings)")
    p.add_argument("--quick", action="store_true",
                   help="(backtest) skip API calls, only test pixel-diff gate")
    p.add_argument("--alerts", action="store_true",
                   help="(backtest) test active wake alert detection")
    p.add_argument("--from-date", metavar="DATE",
                   help="(backtest) only test entries from this date (YYYY-MM-DD)")
    p.add_argument("--count", metavar="N", type=int,
                   help="(backtest) only test last N entries")
    return p.parse_args()
