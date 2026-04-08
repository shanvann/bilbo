"""CLI commands: cmd_last, cmd_backtest, cmd_status, arg parsing."""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    AUDIT_LOG_FILE,
    AUDIT_SAMPLE_SIZE,
    BURST_AWAKE_THRESHOLD,
    CORRECTIONS_FILE,
    ENV_FILE,
    FRAMES_DIR,
    JSONL_FILE,
    LOG_FILE,
    MODEL_CHAIN,
    MODELS_DIR,
    PIXEL_DIFF_THRESHOLD,
    SKILL_DIR,
    WAKE_COOLDOWN_MIN,
    WAKE_WINDOW,
    load_env,
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
        print(f"ACTIVE WAKE ALERT BACKTEST (look-back confirmation, {BURST_AWAKE_THRESHOLD}/3 Awake required)")
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


def cmd_backtest_birdeye(last_n: int = None, from_date: str = None):
    """Run birdeye (local cascade) against historical frames.

    Uses the cloud API state labels in sleep-log.jsonl as ground truth.
    Reports accuracy, confusion matrix, fallback rate, and timing.
    """
    from .local_pipeline import try_local_analysis, BIRDEYE
    from .classifiers import save_head_state

    if not JSONL_FILE.exists():
        print("No JSONL log file found.", file=sys.stderr)
        return 1

    entries = read_all_entries()

    if from_date:
        entries = [e for e in entries if e.get("timestamp", "") >= from_date]
    if last_n:
        entries = entries[-last_n:]

    # Only test entries that were analyzed by the cloud API (not pixel-diff skips)
    # and have existing frame files
    testable = []
    skipped_no_frame = 0
    skipped_not_api = 0
    for e in entries:
        method = e.get("detectionMethod", "")
        if method != "vision-api":
            skipped_not_api += 1
            continue
        frame = e.get("frame", "")
        if not frame or not Path(frame).exists():
            skipped_no_frame += 1
            continue
        testable.append(e)

    if not testable:
        print(f"No testable entries (skipped {skipped_not_api} non-API, {skipped_no_frame} missing frames).",
              file=sys.stderr)
        return 1

    # Count how many entries have head position data from the cloud API
    has_head_pos = sum(1 for e in testable
                       if isinstance(e.get("headPosition"), dict) and "x" in e.get("headPosition", {}))
    print(f"Birdeye backtest: {len(testable)} frames with cloud API ground truth")
    print(f"  Skipped: {skipped_not_api} non-API entries, {skipped_no_frame} missing frames")
    print(f"  Head position data: {has_head_pos}/{len(testable)} entries")
    print(f"  (entries without head data use default position)")
    print()

    # State normalization: cloud API uses "Asleep"/"Awake"/"Unknown", birdeye uses same
    def normalize_state(s):
        if s is None:
            return "unknown"
        s = s.lower().strip()
        if s in ("asleep", "awake", "unknown", "not_present"):
            return s
        if s == "no":
            return "not_present"
        return "unknown"

    # Run birdeye on each frame
    results = {
        "total": len(testable),
        "birdeye_decided": 0,      # birdeye returned a confident state
        "birdeye_fallback": 0,     # birdeye returned None (would use cloud API)
        "birdeye_error": 0,        # shouldn't happen, but track it
        "correct": 0,
        "incorrect": 0,
        "confusion": {},           # {(true, pred): count}
        "timings": [],
        "fallback_reasons": [],    # what the ground truth was when birdeye fell back
    }

    for i, entry in enumerate(testable):
        frame_path = Path(entry["frame"])
        gt_present = entry.get("babyPresent", True)
        gt_state = normalize_state(entry.get("state"))

        # Ground truth: combine presence + state
        if not gt_present:
            gt_label = "not_present"
        else:
            gt_label = gt_state

        # Simulate head state: use head position from this entry's cloud API
        # data (if available), mimicking what would happen in production when
        # the cloud API updates head-state.json on each fallback
        head_pos = entry.get("headPosition")
        if isinstance(head_pos, dict) and "x" in head_pos:
            save_head_state(head_pos["x"], head_pos["y"], source="backtest")

        birdeye_result = try_local_analysis(frame_path)

        if birdeye_result is None:
            results["birdeye_fallback"] += 1
            results["fallback_reasons"].append(gt_label)
            status = "FALLBACK"
        else:
            results["birdeye_decided"] += 1
            pred_label = normalize_state(birdeye_result.get("state"))
            pair = (gt_label, pred_label)
            results["confusion"][pair] = results["confusion"].get(pair, 0) + 1

            if gt_label == pred_label:
                results["correct"] += 1
                status = "OK"
            else:
                results["incorrect"] += 1
                status = "MISS"

            timings = birdeye_result.get("birdeyeTimings", {})
            if "total" in timings:
                results["timings"].append(timings["total"])

        # Progress every 50 frames
        if (i + 1) % 50 == 0 or i == len(testable) - 1:
            pct = (i + 1) * 100 // len(testable)
            print(f"  [{pct:3d}%] {i+1}/{len(testable)}  decided={results['birdeye_decided']}  "
                  f"fallback={results['birdeye_fallback']}  correct={results['correct']}  "
                  f"incorrect={results['incorrect']}", end="\r")

    print()  # clear progress line
    print()

    # --- Report ---
    decided = results["birdeye_decided"]
    fallback = results["birdeye_fallback"]
    correct = results["correct"]
    incorrect = results["incorrect"]
    total = results["total"]

    print(f"{'='*65}")
    print(f"BIRDEYE BACKTEST RESULTS")
    print(f"{'='*65}")
    print(f"Total frames tested:   {total}")
    print(f"Birdeye decided:       {decided} ({decided*100//total}%)")
    print(f"Birdeye fallback:      {fallback} ({fallback*100//total}% → would use cloud API)")
    print()

    if decided > 0:
        accuracy = correct * 100 / decided
        print(f"Accuracy (when decided): {correct}/{decided} = {accuracy:.1f}%")
        print(f"  Correct:   {correct}")
        print(f"  Incorrect: {incorrect}")
        print()

        # Confusion matrix
        labels = sorted(set(k[0] for k in results["confusion"]) | set(k[1] for k in results["confusion"]))
        if labels:
            print(f"Confusion matrix (rows=ground truth, cols=birdeye):")
            header = f"  {'':>12s}" + "".join(f"{l:>12s}" for l in labels)
            print(header)
            for true_l in labels:
                row = f"  {true_l:>12s}"
                for pred_l in labels:
                    count = results["confusion"].get((true_l, pred_l), 0)
                    row += f"{count:>12d}"
                print(row)
            print()

        # Critical error: awake predicted as asleep
        awake_as_asleep = results["confusion"].get(("awake", "asleep"), 0)
        awake_total = sum(v for (t, p), v in results["confusion"].items() if t == "awake")
        if awake_total > 0:
            miss_rate = awake_as_asleep * 100 / awake_total
            print(f"Critical: awake→asleep misses: {awake_as_asleep}/{awake_total} ({miss_rate:.1f}%)")
        else:
            print(f"Critical: no awake frames to evaluate awake→asleep error rate")

    # Fallback analysis
    if fallback > 0:
        print()
        print(f"Fallback breakdown (what ground truth was when birdeye fell back):")
        from collections import Counter
        fb_counts = Counter(results["fallback_reasons"])
        for label, count in fb_counts.most_common():
            print(f"  {label:>12s}: {count} ({count*100//fallback}%)")

    # Timing stats
    if results["timings"]:
        ts = results["timings"]
        avg = sum(ts) / len(ts)
        p50 = sorted(ts)[len(ts) // 2]
        p95 = sorted(ts)[int(len(ts) * 0.95)]
        print()
        print(f"Timing (per frame, birdeye-decided only):")
        print(f"  avg={avg:.3f}s  p50={p50:.3f}s  p95={p95:.3f}s  min={min(ts):.3f}s  max={max(ts):.3f}s")

    print()
    return 0


def cmd_audit(sample_size: int = None):
    """Spot-check birdeye decisions by running a sample through the cloud API.

    Picks recent birdeye-decided frames with existing frame files, sends them
    to the cloud API, and compares. Logs disagreements to audit-log.jsonl
    for retraining.
    """
    import random
    from .vision import analyze_frame, flatten_analysis

    sample_size = sample_size or AUDIT_SAMPLE_SIZE

    if not ENV_FILE.exists():
        print("Missing .env.baby-monitor", file=sys.stderr)
        return 1

    env = load_env(ENV_FILE)
    api_key = env.get("OPENAI_API_KEY")
    anthropic_key = env.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Missing OPENAI_API_KEY in env", file=sys.stderr)
        return 1

    entries = read_all_entries()
    # Filter to birdeye-decided entries with existing frames
    candidates = []
    for e in entries:
        if e.get("detectionMethod") != "birdeye":
            continue
        frame = e.get("frame", "")
        if frame and Path(frame).exists():
            candidates.append(e)

    if not candidates:
        print("No birdeye entries with existing frames to audit.", file=sys.stderr)
        return 1

    sample = random.sample(candidates, min(sample_size, len(candidates)))
    print(f"Auditing {len(sample)} birdeye frames against cloud API (gpt-4o)")
    print()

    agrees = 0
    disagrees = 0
    errors = 0
    disagreement_details = []

    for i, entry in enumerate(sample):
        frame_path = Path(entry["frame"])
        birdeye_state = entry.get("state", "Unknown")
        birdeye_present = entry.get("babyPresent", False)

        try:
            analysis = analyze_frame(frame_path, api_key, anthropic_key)
            flat = flatten_analysis(analysis, str(frame_path))
        except Exception as e:
            log.warning("audit: cloud API failed for %s: %s", frame_path.name, e)
            errors += 1
            continue

        cloud_present = flat.get("babyPresent", False)
        cloud_state = flat.get("state", "Unknown")

        # Normalize for comparison
        b_label = birdeye_state if birdeye_present else "not_present"
        c_label = cloud_state if cloud_present else "not_present"

        match = b_label.lower() == c_label.lower()
        if match:
            agrees += 1
        else:
            disagrees += 1
            detail = {
                "timestamp": entry.get("timestamp"),
                "frame": str(frame_path),
                "birdeye": b_label,
                "cloud": c_label,
                "birdeyeConfidence": entry.get("presenceConfidence"),
                "eyeConfidence": entry.get("eyeConfidence"),
            }
            disagreement_details.append(detail)

            # Log to audit-log.jsonl for retraining
            audit_entry = {
                "auditTimestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "originalTimestamp": entry.get("timestamp"),
                "frame": str(frame_path),
                "birdeyeState": b_label,
                "cloudState": c_label,
                "cloudFull": flat,
                "source": "audit",
            }
            with open(AUDIT_LOG_FILE, "a") as f:
                f.write(json.dumps(audit_entry) + "\n")

        status = "OK" if match else "DISAGREE"
        if (i + 1) % 10 == 0 or i == len(sample) - 1:
            print(f"  [{i+1}/{len(sample)}] agree={agrees} disagree={disagrees} errors={errors}",
                  end="\r")

    print()
    print()
    total = agrees + disagrees
    print(f"{'='*55}")
    print(f"AUDIT RESULTS ({len(sample)} frames)")
    print(f"{'='*55}")
    print(f"Agreement:    {agrees}/{total} ({agrees*100//max(total,1)}%)")
    print(f"Disagreement: {disagrees}/{total} ({disagrees*100//max(total,1)}%)")
    if errors:
        print(f"API errors:   {errors}")

    if disagreement_details:
        print()
        print(f"Disagreements (logged to {AUDIT_LOG_FILE.name} for retraining):")
        for d in disagreement_details:
            print(f"  {d['timestamp']}  birdeye={d['birdeye']:<12s} cloud={d['cloud']:<12s}  "
                  f"conf={d.get('eyeConfidence', '?')}")

    return 0


def cmd_retrain():
    """Retrain classifiers using corrections + audit disagreements as supplemental data.

    Only retrains if corrections.jsonl or audit-log.jsonl has new entries since the
    last model file was modified.
    """
    import subprocess

    # Check if retraining data exists
    has_corrections = CORRECTIONS_FILE.exists() and CORRECTIONS_FILE.stat().st_size > 0
    has_audit = AUDIT_LOG_FILE.exists() and AUDIT_LOG_FILE.stat().st_size > 0

    if not has_corrections and not has_audit:
        print("No corrections or audit data to retrain on. Skipping.")
        return 0

    # Check if models are newer than the retraining data
    model_files = list(MODELS_DIR.glob("*.pt"))
    if model_files:
        newest_model = max(f.stat().st_mtime for f in model_files)
        data_mtime = 0
        if has_corrections:
            data_mtime = max(data_mtime, CORRECTIONS_FILE.stat().st_mtime)
        if has_audit:
            data_mtime = max(data_mtime, AUDIT_LOG_FILE.stat().st_mtime)
        if newest_model > data_mtime:
            print("Models are newer than retraining data. No retrain needed.")
            return 0

    # Count retraining signals
    n_corrections = 0
    n_audit = 0
    if has_corrections:
        n_corrections = sum(1 for _ in CORRECTIONS_FILE.read_text().strip().splitlines() if _)
    if has_audit:
        n_audit = sum(1 for _ in AUDIT_LOG_FILE.read_text().strip().splitlines() if _)

    print(f"Retraining with {n_corrections} corrections + {n_audit} audit disagreements")

    # Build the training command
    train_script = SKILL_DIR / "scripts" / "train_classifiers.py"
    sleep_log = JSONL_FILE
    frames_dir = FRAMES_DIR
    face_crops = SKILL_DIR / "pipeline" / "output" / "bootstrap" / "face_crops"

    cmd = [
        sys.executable, str(train_script),
        "--sleep-log", str(sleep_log),
        "--frames", str(frames_dir),
        "--output", str(MODELS_DIR),
    ]
    if face_crops.exists():
        cmd.extend(["--face-crops", str(face_crops)])
    if CORRECTIONS_FILE.exists() and CORRECTIONS_FILE.stat().st_size > 0:
        cmd.extend(["--corrections", str(CORRECTIONS_FILE)])
    if AUDIT_LOG_FILE.exists() and AUDIT_LOG_FILE.stat().st_size > 0:
        cmd.extend(["--audit", str(AUDIT_LOG_FILE)])

    print(f"Running: {' '.join(cmd[-6:])}")
    print()

    result = subprocess.run(cmd, cwd=str(SKILL_DIR))

    if result.returncode == 0:
        print()
        print("Retrain complete. Reload launchd to use new models:")
        print("  launchctl unload ~/Library/LaunchAgents/com.openclaw.baby-monitor.plist")
        print("  launchctl load ~/Library/LaunchAgents/com.openclaw.baby-monitor.plist")
    else:
        print(f"Retrain failed (exit code {result.returncode})", file=sys.stderr)

    return result.returncode


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
    mode.add_argument("--audit", action="store_true",
                      help="spot-check birdeye decisions against cloud API")
    mode.add_argument("--retrain", action="store_true",
                      help="retrain classifiers with corrections + audit data")
    p.add_argument("--dry-run", action="store_true",
                   help="run full pipeline but do not write to the JSONL log")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print all log messages to stderr (not just warnings)")
    p.add_argument("--quick", action="store_true",
                   help="(backtest) skip API calls, only test pixel-diff gate")
    p.add_argument("--alerts", action="store_true",
                   help="(backtest) test active wake alert detection")
    p.add_argument("--birdeye", action="store_true",
                   help="(backtest) test birdeye local cascade against cloud API ground truth")
    p.add_argument("--from-date", metavar="DATE",
                   help="(backtest) only test entries from this date (YYYY-MM-DD)")
    p.add_argument("--count", metavar="N", type=int,
                   help="(backtest) only test last N entries")
    p.add_argument("--sample", metavar="N", type=int,
                   help="(audit) number of frames to spot-check")
    return p.parse_args()
