"""CLI commands: cmd_last, cmd_backtest, cmd_status, arg parsing."""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bilbo.config import (
    AUDIT_LOG_FILE,
    AUDIT_SAMPLE_SIZE,
    BURST_AWAKE_THRESHOLD,
    CORRECTIONS_FILE,
    ENV_FILE,
    FRAMES_DIR,
    JSONL_FILE,
    LOG_FILE,
    MODEL_CHAIN,
    BILBO_ROOT,
    MODELS_DIR,
    PIXEL_DIFF_THRESHOLD,
    WAKE_COOLDOWN_MIN,
    WAKE_WINDOW,
    load_env,
)
from bilbo.pipeline.detect import compute_diff_score
from bilbo.storage.files import read_all_entries

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
    from bilbo.pipeline.local_pipeline import try_local_analysis, BIRDEYE
    from bilbo.pipeline.classifiers import save_head_state

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
    from bilbo.pipeline.vision import analyze_frame, flatten_analysis

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


def cmd_list_models():
    """List available model versions."""
    version_dirs = sorted(
        [d for d in MODELS_DIR.iterdir() if d.is_dir() and d.name.startswith("v_")],
        key=lambda d: d.name,
    )
    if not version_dirs:
        print("No versioned models found.")
        return 0

    # Check which is current
    latest_link = MODELS_DIR / "latest"
    current = None
    if latest_link.is_symlink():
        current = latest_link.resolve().name

    # Load training log for metadata
    training_log = MODELS_DIR / "training-log.jsonl"
    log_entries = {}
    if training_log.exists():
        for line in training_log.read_text().strip().splitlines():
            if line:
                entry = json.loads(line)
                log_entries[entry.get("version")] = entry

    print(f"Model versions ({len(version_dirs)} total, latest 20 kept):")
    print()
    for d in version_dirs:
        marker = " ← active" if d.name == current else ""
        meta = log_entries.get(d.name, {})
        entries = meta.get("entries_total", "?")
        sources = meta.get("label_sources", {})
        corrections = sources.get("correction", 0)
        audit = sources.get("audit", 0)
        ts = meta.get("timestamp", "")[:19]
        print(f"  {d.name}  trained={ts}  entries={entries}  "
              f"corrections={corrections}  audit={audit}{marker}")

    return 0


def cmd_rollback(version: str):
    """Rollback to a specific model version."""
    target = MODELS_DIR / version
    if not target.is_dir():
        # Try partial match
        matches = [d for d in MODELS_DIR.iterdir()
                    if d.is_dir() and d.name.startswith("v_") and version in d.name]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            print(f"Ambiguous version '{version}'. Matches:", file=sys.stderr)
            for m in matches:
                print(f"  {m.name}", file=sys.stderr)
            return 1
        else:
            print(f"Version '{version}' not found.", file=sys.stderr)
            return 1

    # Check it has model files
    if not (target / "presence_classifier.pt").exists() and not (target / "eye_state_classifier.pt").exists():
        print(f"Version {target.name} has no model files.", file=sys.stderr)
        return 1

    # Update latest symlink
    latest_link = MODELS_DIR / "latest"
    old_version = None
    if latest_link.is_symlink():
        old_version = latest_link.resolve().name
        latest_link.unlink()
    latest_link.symlink_to(target.name)

    # Also copy into top-level models/ for backward compat
    import shutil
    for pt_file in target.glob("*.pt"):
        shutil.copy2(pt_file, MODELS_DIR / pt_file.name)

    print(f"Rolled back: {old_version or 'none'} → {target.name}")
    print("Reload launchd to use the rolled-back models:")
    print("  launchctl unload ~/Library/LaunchAgents/com.baby-monitor.plist")
    print("  launchctl load ~/Library/LaunchAgents/com.baby-monitor.plist")

    return 0


def cmd_backfill_shadow(hours: float | None = None, only_stale: bool = False,
                         dry_run: bool = False, limit: int | None = None) -> int:
    """Re-run BIRDEYE shadow inference over historical entries.

    Use after deploying a new model: replays the local pipeline against
    every frame in the window and writes the new predictions into both the
    JSON `data` blob and the indexed shadow_birdeye_* columns.

    --only-stale skips entries already tagged with the deployed model
    version, so repeated runs are cheap and incremental.
    """
    import time as _time
    from bilbo.storage.db import get_db, _derive_shadow_columns
    from bilbo.pipeline.local_pipeline import try_local_analysis
    import bilbo.pipeline.local_pipeline as _lp

    latest_link = MODELS_DIR / "latest"
    if not latest_link.is_symlink():
        print(f"No deployed model — {latest_link} is not a symlink", file=sys.stderr)
        return 1
    deployed_version = Path(os.readlink(latest_link)).name
    print(f"deployed model: {deployed_version}")

    # Force fresh classifier load so we pick up the latest weights on disk.
    _lp._presence_clf = None
    _lp._eye_state_clf = None
    _lp._face_detector = None
    _lp._face_detector_fallback = None
    _lp._available = None

    db = get_db()
    entries = db.get_entries(hours=hours) if hours is not None else db.get_entries()
    print(f"window: {f'last {hours}h' if hours is not None else 'all entries'} — {len(entries)} fetched")

    if only_stale:
        entries = [e for e in entries if e.get("shadowModelVersion") != deployed_version]
        print(f"--only-stale: {len(entries)} entries with stale or missing model version")

    if limit is not None:
        entries = entries[:limit]
        print(f"--limit: capped to {len(entries)} entries")

    n_total = len(entries)
    n_processed = 0
    n_missing_frame = 0
    n_hard_error = 0
    n_updated = 0
    n_changed = 0
    t0 = _time.monotonic()

    for i, entry in enumerate(entries, 1):
        ts = entry.get("timestamp")
        frame = entry.get("frame")
        if not frame or not Path(frame).exists():
            n_missing_frame += 1
            continue

        result = try_local_analysis(Path(frame))
        n_processed += 1

        if result is None:
            n_hard_error += 1
            continue

        # Build the shadow dict in the same shape live capture writes
        # (monitor.py uses state-domain birdeyeState/prodState fields for
        # back-compat with historical readers).
        birdeye_state = result.get("state", "Unknown")
        if not result.get("babyPresent", False):
            birdeye_state = "not_present"
        prod_state = entry.get("state", "Unknown")
        if not entry.get("babyPresent", False):
            prod_state = "not_present"

        shadow = {
            "birdeyeState": birdeye_state,
            "prodState": prod_state,
            "agreed": birdeye_state.lower() == prod_state.lower(),
            "presenceConfidence": result.get("presenceConfidence"),
            "eyeConfidence": result.get("eyeConfidence"),
            "eyeState": result.get("eyeState"),
            "birdeyeTimings": result.get("birdeyeTimings"),
            "fallback": result.get("fallback"),
        }

        # Detect actual prediction change before mutating
        prev_present = entry.get("shadow", {}).get("birdeyeState") if isinstance(entry.get("shadow"), dict) else None
        prev_eye = entry.get("shadow", {}).get("eyeState") if isinstance(entry.get("shadow"), dict) else None
        if prev_present != birdeye_state or prev_eye != shadow["eyeState"]:
            n_changed += 1

        updates = {"shadow": shadow, "shadowModelVersion": deployed_version}
        if result.get("faceBbox"):
            updates["faceBbox"] = result["faceBbox"]
        if result.get("faceConfidence") is not None:
            updates["faceConfidence"] = result["faceConfidence"]

        if not dry_run:
            db.update_entry(ts, updates)
        n_updated += 1

        if i % 100 == 0 or i == n_total:
            elapsed = _time.monotonic() - t0
            rate = n_processed / elapsed if elapsed else 0
            eta = (n_total - i) / rate if rate else 0
            print(
                f"  [{i}/{n_total}] processed={n_processed} updated={n_updated} "
                f"changed={n_changed} missing={n_missing_frame} errors={n_hard_error} "
                f"rate={rate:.1f}/s eta={eta:.0f}s",
                flush=True,
            )

    elapsed = _time.monotonic() - t0
    print(
        f"done in {elapsed:.1f}s — total={n_total} processed={n_processed} "
        f"updated={n_updated} changed={n_changed} missing_frame={n_missing_frame} "
        f"hard_error={n_hard_error}"
        + (" (dry-run)" if dry_run else "")
    )
    return 0


def _reinfer_corrections_against_current_model(
    model_version: str | None,
) -> tuple[int, int, dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Run BIRDEYE inference on every corrected frame and compare the raw
    eye-state classifier output (or presence, for not_in_bassinet corrections)
    to the human-corrected ground truth.

    Used by cmd_retrain (after a fresh training run) and cmd_eval_corrections
    (to re-evaluate the currently deployed model without retraining).

    Comparison semantics (updated 2026-04-09): we compare at the eye-state
    level, NOT at the derived Awake/Asleep level.

    - For corrections labeled `eyes_open`, `eyes_closed`, `face_not_visible`:
      compared to birdeye's raw eyeState output. A `None` result from
      try_local_analysis means birdeye's eye-state classifier output
      `face_not_visible` and the pipeline would fall back to cloud — we
      treat that as a `face_not_visible` prediction for accounting.
    - For corrections labeled `not_in_bassinet`: compared to birdeye's
      presence classifier output (baby_present == False).

    Presence-level agreement is tracked in a separate counter so that
    "birdeye said present but wrong eye state" counts as correct for the
    presence panel and wrong for the eye-state panel — the two classifiers
    are evaluated independently.

    Side effects:
      - Force-reloads the local_pipeline classifier singletons so the latest
        model weights on disk are picked up, not whatever was cached.
      - Mutates entries in sleep-log.jsonl with new shadow data + the
        provided model_version label.

    Args:
        model_version: written into entry["shadowModelVersion"] if non-None.

    Returns:
        (total_reinfer, agreed_after, eye_class_counts, presence_class_counts).
        eye_class_counts is keyed by the eye-state correction label and
        covers every reinferred frame (no dropped fallbacks).
        presence_class_counts is keyed by the presence truth label
        ("not_present" or "present") and tracks how often birdeye's presence
        classifier was correct, independently of its eye-state output.
    """
    # Force reload of classifiers (clear cached singleton)
    from bilbo.pipeline.local_pipeline import try_local_analysis
    import bilbo.pipeline.local_pipeline as _lp
    _lp._presence_clf = None
    _lp._eye_state_clf = None
    _lp._face_detector = None
    _lp._face_detector_fallback = None
    _lp._available = None

    # State-domain labels kept for backwards compat with the existing
    # shadow-dict schema (db.insert_entry, api_timeline, etc.). The agreement
    # computation itself is in eye-state domain — see below.
    EYE_TO_STATE = {"eyes_open": "Awake", "eyes_closed": "Asleep",
                    "low_confidence": "Unknown", "not_in_bassinet": "not_present"}

    all_lines = JSONL_FILE.read_text().strip().splitlines()
    entries = [json.loads(l) for l in all_lines]
    updated = 0
    agreed_after = 0
    total_reinfer = 0
    correction_class_counts: dict[str, dict[str, int]] = {}
    # Confusion pairs: (ground_truth, predicted)
    eye_confusion_pairs: list[tuple[str, str]] = []
    pres_confusion_pairs: list[tuple[str, str]] = []
    presence_class_counts: dict[str, dict[str, int]] = {
        "not_present": {"correct": 0, "total": 0},
        "present":     {"correct": 0, "total": 0},
    }

    for entry in entries:
        if not entry.get("eyeStateEdited"):
            continue
        frame = entry.get("frame", "")
        if not frame or not Path(frame).exists():
            continue

        total_reinfer += 1
        birdeye_result = try_local_analysis(Path(frame))

        # Extract birdeye's raw eye-state prediction. None result means the
        # pipeline would have fallen back to cloud (low confidence or
        # not present). Record as "low_confidence" for accounting.
        if birdeye_result is None:
            birdeye_eye = "low_confidence"
            birdeye_present = True  # must have been present, else
                                    # _build_entry("not_present", ...) would
                                    # have been returned (not None)
            shadow_pc = None
            shadow_ec = None
            shadow_timings = None
        else:
            birdeye_present = bool(birdeye_result.get("babyPresent", False))
            birdeye_eye = birdeye_result.get("eyeState") or "low_confidence"
            shadow_pc = birdeye_result.get("presenceConfidence")
            shadow_ec = birdeye_result.get("eyeConfidence")
            shadow_timings = birdeye_result.get("birdeyeTimings")

        gt_eye = entry.get("eyeState", "")

        # Agreement is computed at the eye-state level (not the derived
        # Awake/Asleep level). Awake/Asleep is a higher-level concept that
        # requires more logic than a single frame's eye-state prediction
        # and is being decoupled from the raw classifier metrics.
        #   - not_in_bassinet correction:  presence classifier check
        #   - eye-state correction:        raw eye-state classifier check
        if gt_eye == "not_in_bassinet":
            agreed = (birdeye_present is False)
        elif not birdeye_present:
            # Correction says it's an eye state but birdeye said not_present —
            # that's a presence-side disagreement.
            agreed = False
        else:
            agreed = (birdeye_eye == gt_eye)

        if agreed:
            agreed_after += 1

        # Per-class tally for the dashboard's eye-state "vs corrections" panel.
        cls = gt_eye or "unknown"
        bucket = correction_class_counts.setdefault(cls, {"correct": 0, "total": 0})
        bucket["total"] += 1
        if agreed:
            bucket["correct"] += 1

        # Confusion pair for eye-state corrections (skip not_in_bassinet — that's presence)
        if gt_eye in ("eyes_open", "eyes_closed") and birdeye_eye in ("eyes_open", "eyes_closed"):
            eye_confusion_pairs.append((gt_eye, birdeye_eye))

        # Presence-level tally — independent of eye-state correctness. A frame
        # where birdeye got the presence right but the eye state wrong still
        # counts as correct here.
        pres_truth = "not_present" if gt_eye == "not_in_bassinet" else "present"
        pres_pred = "present" if birdeye_present else "not_present"
        pres_confusion_pairs.append((pres_truth, pres_pred))
        presence_class_counts[pres_truth]["total"] += 1
        if pres_truth == pres_pred:
            presence_class_counts[pres_truth]["correct"] += 1

        # Update the entry's shadow sub-dict. Schema matches the pre-existing
        # shape exactly (birdeyeState, prodState, agreed, eyeState, etc.) so
        # that db.insert_entry, api_timeline, and the frame-viewer all keep
        # working. birdeyeState and prodState are derived from eye-state via
        # EYE_TO_STATE for back-compat with state-domain readers — the
        # `agreed` value above was computed in eye-state domain, which is
        # what actually matters for training/metric accounting.
        birdeye_state_compat = (
            "not_present" if not birdeye_present
            else EYE_TO_STATE.get(birdeye_eye, "Unknown")
        )
        entry["shadow"] = {
            "birdeyeState": birdeye_state_compat,
            "prodState": EYE_TO_STATE.get(gt_eye, "Unknown"),
            "agreed": agreed,
            "presenceConfidence": shadow_pc,
            "eyeConfidence": shadow_ec,
            "eyeState": birdeye_eye,
            "birdeyeTimings": shadow_timings,
        }
        if model_version is not None:
            entry["shadowModelVersion"] = model_version

        # Tag whether post-retrain inference agrees with correction
        entry["retrainAgreed"] = agreed

        # Promote face detection data
        if birdeye_result and birdeye_result.get("faceBbox"):
            entry["faceBbox"] = birdeye_result["faceBbox"]
        if birdeye_result and birdeye_result.get("faceConfidence") is not None:
            entry["faceConfidence"] = birdeye_result["faceConfidence"]

        updated += 1

    # Write back to JSONL
    if updated > 0:
        JSONL_FILE.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    # Write back to SQLite — update shadow columns + retrainAgreed for each
    # corrected entry so the dashboard sees fresh results immediately.
    if updated > 0:
        from bilbo.storage.db import get_connection
        conn = get_connection()
        # Derive new-schema shadow columns from the in-memory shadow dict.
        from bilbo.storage.db import _derive_shadow_columns
        for entry in entries:
            if not entry.get("eyeStateEdited"):
                continue
            ts = entry.get("timestamp")
            if not ts:
                continue
            shadow = entry.get("shadow", {})
            bird_present, bird_eye, agreed = _derive_shadow_columns(entry, shadow)

            # IMPORTANT: merge with the existing SQLite row's `data` blob
            # before writing, so SQLite-only fields survive.  JSONL is
            # the source of truth for primary + shadow fields, but a few
            # fields live in SQLite only and would be silently stripped
            # by a naive `data = json.dumps(entry)` overwrite:
            #
            #   - ``experiments`` — written by scripts/lib/experiments.py
            #     and scripts/experiments_backfill.py
            #   - ``bboxImpact`` — written by scripts/bbox_impact.py
            #   - any future field added by an analysis script that
            #     treats SQLite as primary and skips the JSONL round-trip
            #
            # Merge order: existing wins for SQLite-only keys, JSONL
            # entry wins for anything in JSONL (so the retrain's shadow
            # / eyeState updates still take effect).
            existing_row = conn.execute(
                "SELECT data FROM entries WHERE timestamp = ?", (ts,)
            ).fetchone()
            merged = dict(entry)
            if existing_row and existing_row["data"]:
                try:
                    existing_data = json.loads(existing_row["data"])
                    for k, v in existing_data.items():
                        if k not in merged:
                            merged[k] = v
                except (TypeError, json.JSONDecodeError):
                    pass

            conn.execute("""
                UPDATE entries SET
                    data = ?,
                    shadow_birdeye_present = ?,
                    shadow_birdeye_eye = ?,
                    shadow_agreed = ?,
                    presence_confidence = ?,
                    eye_confidence = ?,
                    shadow_timings_total = ?,
                    shadow_model_version = ?
                WHERE timestamp = ?
            """, (
                json.dumps(merged),
                bird_present,
                bird_eye,
                agreed,
                shadow.get("presenceConfidence"),
                shadow.get("eyeConfidence"),
                (shadow.get("birdeyeTimings") or {}).get("total"),
                entry.get("shadowModelVersion"),
                ts,
            ))
        conn.commit()
        log.info("reinfer: updated %d entries in SQLite", updated)

    return total_reinfer, agreed_after, correction_class_counts, presence_class_counts, eye_confusion_pairs, pres_confusion_pairs


def cmd_eval_corrections() -> int:
    """Re-run BIRDEYE on the human-corrected frames using the currently
    deployed model, and write the per-class agreement into that model's
    training_runs row in SQLite.

    Use cases:
      - The deployed model was trained before correction_agreement existed
        and the dashboard "vs Corrections" panel is empty.
      - You rolled back to an older model and want fresh vs-corrections data
        for it without triggering a full retrain.
      - You added new corrections via the dashboard and want to see how the
        deployed model handles them, before deciding whether to retrain.
    """
    print("Evaluating deployed model against the corrections set...")

    latest_link = MODELS_DIR / "latest"
    if not latest_link.is_symlink():
        print(f"No deployed model — {latest_link} is not a symlink", file=sys.stderr)
        return 1
    deployed_version = Path(os.readlink(latest_link)).name
    print(f"Deployed model: {deployed_version}")
    print()

    total_reinfer, agreed_after, correction_class_counts, presence_class_counts, eye_confusion_pairs, pres_confusion_pairs = (
        _reinfer_corrections_against_current_model(deployed_version)
    )

    if total_reinfer == 0:
        print("No corrected frames found in sleep-log.jsonl.")
        return 0

    pct = agreed_after * 100 // total_reinfer
    print(f"Re-inferred {total_reinfer} corrected frames")
    print(f"Overall eye-state agreement: {agreed_after}/{total_reinfer} ({pct}%)")
    print()
    print("Eye-state per-class:")
    for cls in ("eyes_open", "eyes_closed", "low_confidence", "not_in_bassinet"):
        counts = correction_class_counts.get(cls)
        if not counts or counts["total"] == 0:
            continue
        cpct = counts["correct"] * 100 // counts["total"]
        print(f"  {cls:20s}  {counts['correct']:3d}/{counts['total']:3d}  ({cpct}%)")
    print()
    print("Presence per-class (birdeye present/not_present correctness):")
    for cls in ("not_present", "present"):
        counts = presence_class_counts.get(cls, {})
        if not counts or counts["total"] == 0:
            continue
        cpct = counts["correct"] * 100 // counts["total"]
        print(f"  {cls:20s}  {counts['correct']:3d}/{counts['total']:3d}  ({cpct}%)")
    print()

    # Update SQLite training_runs row for the deployed version.
    from bilbo.storage.db import get_db
    db = get_db()
    pres_correct = sum(c["correct"] for c in presence_class_counts.values())
    pres_total = sum(c["total"] for c in presence_class_counts.values())

    # Build confusion matrices from pairs
    def _build_cm(pairs, classes):
        cm = {t: {p: 0 for p in classes} for t in classes}
        for t, p in pairs:
            if t in cm and p in cm[t]:
                cm[t][p] += 1
        return cm

    eye_corr_cm = _build_cm(eye_confusion_pairs, ["eyes_open", "eyes_closed"])
    pres_corr_cm = _build_cm(pres_confusion_pairs, ["not_present", "present"])

    correction_data = {
        "by_class": correction_class_counts,
        "total": {"correct": agreed_after, "total": total_reinfer},
        "eye_confusion": eye_corr_cm,
        "presence": {
            "by_class": presence_class_counts,
            "total": {"correct": pres_correct, "total": pres_total},
            "confusion": pres_corr_cm,
        },
    }
    updated = db.update_training_run_metrics(
        deployed_version,
        {"correction_agreement": correction_data},
    )

    if updated:
        print(f"✓ training_runs[{deployed_version}].metrics.correction_agreement updated")
        print("  (refresh the dashboard to see the new vs-Corrections panel data)")
        return 0
    else:
        print(
            f"⚠ no training_runs row exists for {deployed_version}; "
            f"SQLite was not updated. (Check `monitor.py --list-models`.)",
            file=sys.stderr,
        )
        return 1


def cmd_retrain(
    trigger: str = "cli",
    force: bool = False,
    skip_face_detect: bool = False,
    skip_post_retrain: bool = False,
    post_retrain_backfill_days: int = 7,
):
    """Retrain classifiers using corrections + audit disagreements as supplemental data.

    Only retrains if corrections.jsonl or audit-log.jsonl has new entries since the
    last model file was modified.

    After a successful retrain, this runs a post-retrain chain so the dashboard
    reflects the new model without manual follow-up:
      1. backfill_birdeye_primary.py over the last `post_retrain_backfill_days`
         to refresh primary eyeState/face fields on recent frames (skips user-
         edited rows).
      2. backfill_state.py to re-smooth the derived `state` column.
      3. bbox_impact.py --force to regenerate Per-class / Bbox-impact numbers
         against the newly-deployed model.

    Pass `skip_post_retrain=True` to opt out (e.g. for a quick metrics-only
    training run that doesn't need data refresh).
    """
    import subprocess
    from . import training_state

    # Check if another training process is running (not us)
    state = training_state.get_status()
    if state.get("status") == "running":
        running_pid = state.get("pid")
        my_pid = __import__("os").getpid()
        if running_pid and running_pid != my_pid:
            print(f"Training already in progress (pid={running_pid}). Use --list-models to check status.")
            return 1

    # Check for pending corrections/audit data since last training.
    # Use content timestamps, not file mtime (unreliable across timezones).
    from datetime import datetime as _dt

    last_trained_dt = None
    training_log = MODELS_DIR / "training-log.jsonl"
    if training_log.exists():
        lines = training_log.read_text().strip().splitlines()
        if lines:
            try:
                ts = json.loads(lines[-1]).get("timestamp", "")
                last_trained_dt = _dt.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, json.JSONDecodeError):
                pass

    def count_pending(path):
        if not path.exists() or path.stat().st_size == 0:
            return 0, 0
        total = 0
        pending = 0
        for line in path.read_text().strip().splitlines():
            if not line:
                continue
            total += 1
            if not last_trained_dt:
                pending += 1
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("correctedAt") or entry.get("auditTimestamp") or ""
                if ts:
                    entry_dt = _dt.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
                    if entry_dt > last_trained_dt:
                        pending += 1
            except (ValueError, json.JSONDecodeError):
                pending += 1
        return total, pending

    n_corrections, pending_corrections = count_pending(CORRECTIONS_FILE)
    n_audit, pending_audit = count_pending(AUDIT_LOG_FILE)
    total_pending = pending_corrections + pending_audit

    if total_pending == 0 and not force:
        print(f"No new data since last training. Skipping.")
        print(f"  Corrections: {n_corrections} total, 0 pending")
        print(f"  Audit: {n_audit} total, 0 pending")
        print(f"  Last trained: {last_trained_dt}")
        print(f"  (use --force to retrain anyway)")
        return 0

    if force and total_pending == 0:
        print(f"Forced retrain (no new data — running on existing dataset).")
        print(f"  (total: {n_corrections} corrections, {n_audit} audit)")
    else:
        print(f"Retraining: {pending_corrections} new corrections + {pending_audit} new audit entries")
        print(f"  (total: {n_corrections} corrections, {n_audit} audit)")

    # Build the training command
    sleep_log = JSONL_FILE
    frames_dir = FRAMES_DIR
    face_crops = BILBO_ROOT / "pipeline" / "output" / "bootstrap" / "face_crops"

    cmd = [
        sys.executable, "-m", "bilbo.train_classifiers",
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
    if skip_face_detect:
        cmd.extend(["--model", "all-no-face"])

    print(f"Running: {' '.join(cmd[-6:])}")
    print()

    training_state.mark_started(trigger)
    result = subprocess.run(cmd)
    training_state.mark_completed(result.returncode)

    if result.returncode != 0:
        print(f"Retrain failed (exit code {result.returncode})", file=sys.stderr)
        return result.returncode

    # --- Read the new training run from training-log.jsonl ---
    # We hold off on the SQLite sync until after the re-inference loop below
    # so we can enrich `metrics.correction_agreement` in a single insert.
    training_log_file = MODELS_DIR / "training-log.jsonl"
    run_data = None
    training_log_lines = []
    model_version = None
    if training_log_file.exists():
        training_log_lines = training_log_file.read_text().strip().splitlines()
        if training_log_lines:
            run_data = json.loads(training_log_lines[-1])
            model_version = run_data.get("version")

    # --- Post-retrain: re-infer corrected frames with new model ---
    print()
    print("Re-inferring corrected frames with new model...")

    total_reinfer, agreed_after, correction_class_counts, presence_class_counts, eye_confusion_pairs, pres_confusion_pairs = (
        _reinfer_corrections_against_current_model(model_version)
    )

    agreement_pct = agreed_after * 100 // total_reinfer if total_reinfer > 0 else 0
    print(f"Re-inferred {total_reinfer} corrected frames")
    print(f"Eye-state agreement: {agreed_after}/{total_reinfer} ({agreement_pct}%)")

    # --- Enrich training-log.jsonl + SQLite training_runs with the
    # post-retrain correction agreement, then sync. Done in a single pass so
    # the JSONL backup and SQLite stay consistent.
    if run_data is not None:
        if total_reinfer > 0:
            pres_correct = sum(c["correct"] for c in presence_class_counts.values())
            pres_total = sum(c["total"] for c in presence_class_counts.values())
            metrics = run_data.setdefault("metrics", {}) or {}

            def _build_cm(pairs, classes):
                cm = {t: {p: 0 for p in classes} for t in classes}
                for t, p in pairs:
                    if t in cm and p in cm[t]:
                        cm[t][p] += 1
                return cm

            metrics["correction_agreement"] = {
                "by_class": correction_class_counts,
                "total": {"correct": agreed_after, "total": total_reinfer},
                "eye_confusion": _build_cm(eye_confusion_pairs, ["eyes_open", "eyes_closed"]),
                "presence": {
                    "by_class": presence_class_counts,
                    "total": {"correct": pres_correct, "total": pres_total},
                    "confusion": _build_cm(pres_confusion_pairs, ["not_present", "present"]),
                },
            }
            run_data["metrics"] = metrics
            # Rewrite the last JSONL line in place so the SQLite sync (and any
            # disaster-recovery from JSONL) sees the enriched record.
            training_log_lines[-1] = json.dumps(run_data)
            training_log_file.write_text("\n".join(training_log_lines) + "\n")

        from bilbo.storage.db import get_db
        db = get_db()
        existing = db.get_last_training_runs(1)
        if not existing or existing[0].get("version") != run_data.get("version"):
            db.insert_training_run(run_data)
            log.info("retrain: synced training run %s to SQLite", run_data.get("version"))

    # --- Post-retrain refresh chain ---
    # Runs automatically so dashboard Per-class / Bbox-impact numbers track
    # the deployed model without manual follow-up. Failures here are logged
    # but non-fatal — the retrain itself already succeeded and been persisted.
    if not skip_post_retrain:
        from datetime import timedelta as _td
        backfill_start = (
            _dt.utcnow() - _td(days=post_retrain_backfill_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        steps = [
            (
                "backfill_birdeye_primary",
                [
                    sys.executable, "-m", "bilbo.scripts.backfill_birdeye_primary",
                    "--start", backfill_start,
                ],
            ),
            (
                "backfill_state",
                [sys.executable, "-m", "bilbo.scripts.backfill_state"],
            ),
            (
                "bbox_impact",
                [sys.executable, "-m", "bilbo.scripts.bbox_impact", "--force"],
            ),
        ]

        print()
        print(f"Post-retrain chain (--skip-post-retrain to disable):")
        for name, cmd in steps:
            print(f"  → {name}")
            try:
                step_result = subprocess.run(cmd)
                if step_result.returncode != 0:
                    log.warning(
                        "post-retrain step %s exited %d — continuing "
                        "(retrain itself succeeded and is already persisted)",
                        name, step_result.returncode,
                    )
            except Exception as e:
                log.warning("post-retrain step %s raised %s — continuing", name, e)
    else:
        print()
        print("Skipping post-retrain chain (--skip-post-retrain).")

    print()
    print(f"Retrain complete (model {model_version}).")

    return 0


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
    mode.add_argument("--eval-corrections", action="store_true",
                      help="re-run BIRDEYE on corrected frames using the deployed "
                           "model and write per-class agreement to its training_runs row "
                           "(no retraining; useful after a rollback)")
    p.add_argument("--force", action="store_true",
                   help="(retrain) bypass the 'no new data since last training' guard "
                        "— useful when training code or hyperparameters changed")
    p.add_argument("--skip-face-detect", action="store_true",
                   help="(retrain) skip face detector training (~60 min savings)")
    p.add_argument("--skip-post-retrain", action="store_true",
                   help="(retrain) skip the post-retrain chain "
                        "(backfill-primary → backfill-state → bbox-impact)")
    p.add_argument("--post-retrain-backfill-days", metavar="N", type=int, default=7,
                   help="(retrain) days back for post-retrain primary backfill window (default: 7)")
    mode.add_argument("--list-models", action="store_true",
                      help="list available model versions")
    mode.add_argument("--rollback", metavar="VERSION",
                      help="rollback to a specific model version")
    mode.add_argument("--backfill-shadow", action="store_true",
                      help="re-run BIRDEYE shadow inference on historical entries "
                           "(use after deploying a new model)")
    p.add_argument("--hours", metavar="N", type=float,
                   help="(backfill-shadow) only process entries from the last N hours "
                        "(default: all entries)")
    p.add_argument("--only-stale", action="store_true",
                   help="(backfill-shadow) skip entries already tagged with the "
                        "currently deployed model version")
    p.add_argument("--limit", metavar="N", type=int,
                   help="(backfill-shadow) cap the number of entries processed")
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
    p.add_argument("--loop", action="store_true",
                   help="persistent capture every 60s (Docker mode; "
                        "default is one tick and exit)")
    p.add_argument("--sample", metavar="N", type=int,
                   help="(audit) number of frames to spot-check")
    return p.parse_args()
