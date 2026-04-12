#!/usr/bin/env python3
"""
Baby monitor: capture RTSP frame, analyze via OpenAI vision, log results.

Combines capture + analysis into one script so the cron agent only needs
to run this and relay the output.

Usage:
  monitor.py                     Full pipeline (capture → analyze → log)
  monitor.py --capture-only      Capture a frame, print path, exit
  monitor.py --analyze FILE      Analyze an existing frame (skip capture)
  monitor.py --dry-run           Full pipeline but don't write to JSONL log
  monitor.py --verbose           Print detailed logs to stderr
  monitor.py --last N            Show last N log entries from JSONL
  monitor.py --status            Show current system status and recent gaps

Output (stdout): single JSON line
  {"status": "ok"|"alert"|"error", "frame": "...", "alerts": [...], "summary": "..."}

Backtesting:
  monitor.py --backtest                      Replay all historical frames against current logic
  monitor.py --backtest --last 100           Backtest last 100 entries only
  monitor.py --backtest --from 2026-03-30    Backtest from date
  monitor.py --backtest --quick              Skip API calls, only test pixel-diff gate
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure lib/ is importable when run as `python3 monitor.py` from scripts/
# or `python3 scripts/monitor.py` from baby-monitor/
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import ENV_FILE, MODEL_CHAIN, load_env, log, set_verbose
from lib.capture import capture_frame, enforce_disk_limit
from lib.db import get_db
from lib.detect import detect_empty_bassinet, make_empty_entry
from lib.vision import analyze_frame, flatten_analysis
from lib.local_pipeline import try_local_analysis
from lib.alerts import (
    check_alerts,
    check_edge_alert,
    check_wake_confirmation,
    get_alert_stats,
    log_alert_feedback,
    record_alert_feedback,
    reset_wake_cooldown,
    save_alert_state,
    send_telegram_alert,
    should_burst,
)
from lib.storage import append_entry, get_last_entry
from lib.cli import (
    cmd_audit, cmd_backtest, cmd_backtest_birdeye, cmd_eval_corrections,
    cmd_last, cmd_list_models, cmd_retrain, cmd_rollback, cmd_status, parse_args,
)


def _output(status: str, **kwargs):
    print(json.dumps({"status": status, **kwargs}))


def main():
    args = parse_args()

    if args.verbose:
        set_verbose()

    mode = "last" if args.last is not None else "status" if args.status else \
           "backtest" if args.backtest else \
           "capture-only" if args.capture_only else "analyze" if args.analyze else "pipeline"
    log.info("--- run start: mode=%s dry_run=%s verbose=%s models=%s ---", mode, args.dry_run, args.verbose,
             " → ".join(f"{m['provider']}/{m['model']}" for m in MODEL_CHAIN))
    log.debug("python=%s, pid=%d", sys.version.split()[0], __import__("os").getpid())
    t_start = time.monotonic()

    # --- Diagnostic modes (no env/API needed) ---

    if args.last is not None:
        return cmd_last(args.last)

    if args.status:
        return cmd_status()

    if args.feedback:
        alert_id, fb = args.feedback
        fb = fb.lower()
        if fb not in ("yes", "no"):
            print("Feedback must be 'yes' or 'no'", file=sys.stderr)
            return 1
        if record_alert_feedback(alert_id, fb):
            print(f"Recorded feedback '{fb}' for alert {alert_id}")
            return 0
        else:
            print(f"Alert {alert_id} not found or already has feedback", file=sys.stderr)
            return 1

    if args.alert_stats:
        stats = get_alert_stats()
        print(f"Alert Accuracy Stats:")
        print(f"  Total alerts: {stats['total']}")
        print(f"  Confirmed (yes): {stats['yes']}")
        print(f"  False alarm (no): {stats['no']}")
        print(f"  Pending feedback: {stats['pending']}")
        print(f"  Precision: {stats['precision']}")
        return 0

    if args.backtest:
        if args.birdeye:
            return cmd_backtest_birdeye(
                last_n=args.count,
                from_date=args.from_date,
            )
        return cmd_backtest(
            last_n=args.count,
            from_date=args.from_date,
            quick=args.quick,
            alerts=args.alerts,
        )

    if args.audit:
        return cmd_audit(sample_size=args.sample)

    if args.retrain:
        return cmd_retrain(force=args.force, skip_face_detect=args.skip_face_detect)

    if args.eval_corrections:
        return cmd_eval_corrections()

    if args.list_models:
        return cmd_list_models()

    if args.rollback:
        return cmd_rollback(args.rollback)

    # --- Load config ---

    if not ENV_FILE.exists():
        log.error("env file not found: %s", ENV_FILE)
        _output("error", error=f"env file not found: {ENV_FILE}")
        return 1

    env = load_env(ENV_FILE)
    rtsp_url = env.get("RTSP_STREAM_URL")
    api_key = env.get("OPENAI_API_KEY")
    anthropic_key = env.get("ANTHROPIC_API_KEY")

    # --- Capture-only mode ---

    if args.capture_only:
        if not rtsp_url:
            log.error("capture-only: missing RTSP_STREAM_URL in env file")
            print("ERROR: missing RTSP_STREAM_URL in env file", file=sys.stderr)
            return 1
        try:
            frame_path = capture_frame(rtsp_url)
            log.info("capture-only: done in %.1fs -> %s", time.monotonic() - t_start, frame_path)
            print(str(frame_path))
            return 0
        except RuntimeError as e:
            log.error("capture-only: failed - %s", e)
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    # --- Analyze-only mode ---

    if args.analyze:
        frame_path = Path(args.analyze)
        if not frame_path.exists():
            log.error("analyze: file not found: %s", frame_path)
            print(f"ERROR: file not found: {frame_path}", file=sys.stderr)
            return 1
        if not api_key:
            log.error("analyze: missing OPENAI_API_KEY in env file")
            print("ERROR: missing OPENAI_API_KEY in env file", file=sys.stderr)
            return 1
        log.info("analyze: analyzing existing frame %s (%dKB)",
                 frame_path, frame_path.stat().st_size // 1024)
        try:
            analysis = analyze_frame(frame_path, api_key, anthropic_key)
        except RuntimeError as e:
            _output("error", error=str(e), frame=str(frame_path))
            return 1
        flat = flatten_analysis(analysis, str(frame_path))
        alerts = check_alerts(flat)
        flat["alerts"] = alerts
        log.info("analyze: done in %.1fs, %d alerts", time.monotonic() - t_start, len(alerts))
        print(json.dumps(flat, indent=2))
        return 0

    # --- Full pipeline ---

    if not rtsp_url or not api_key:
        missing = []
        if not rtsp_url:
            missing.append("RTSP_STREAM_URL")
        if not api_key:
            missing.append("OPENAI_API_KEY")
        log.error("pipeline: missing env vars: %s", ", ".join(missing))
        _output("error", error=f"missing {', '.join(missing)}")
        return 1

    log.info("pipeline: starting full capture+analyze+log cycle")

    # Capture
    try:
        frame_path = capture_frame(rtsp_url)
    except RuntimeError as e:
        log.error("pipeline: capture failed, aborting - %s", e)
        _output("error", error=str(e))
        return 1

    enforce_disk_limit()

    # =======================================================================
    # Production pipeline: pixel-diff → cloud API (reliable, ground truth)
    # Shadow pipeline: birdeye runs in parallel (logged, not used for decisions)
    #
    # Birdeye results are compared against cloud API to track agreement.
    # Once agreement exceeds threshold, birdeye can be promoted to production.
    # =======================================================================

    # --- Shadow: run birdeye (results logged but not used for prod decisions) ---
    shadow_birdeye = try_local_analysis(frame_path)
    if shadow_birdeye is not None:
        fallback = shadow_birdeye.get("fallback")
        if fallback:
            log.info("shadow: birdeye -> partial (fallback=%s, presence_conf=%.3f eye=%s)",
                     fallback,
                     shadow_birdeye.get("presenceConfidence", 0),
                     shadow_birdeye.get("eyeConfidence", "n/a"))
        else:
            log.info("shadow: birdeye -> %s (conf: presence=%.3f eye=%s)",
                     shadow_birdeye.get("state"),
                     shadow_birdeye.get("presenceConfidence", 0),
                     shadow_birdeye.get("eyeConfidence", "n/a"))

    # --- Production: pixel-diff gate → cloud API ---
    is_empty, diff_score = detect_empty_bassinet(frame_path)

    if is_empty:
        flat = make_empty_entry(frame_path, diff_score)
        log.info("pipeline: pixel-diff -> empty (score=%.2f), cloud API skipped", diff_score)
    else:
        log.info("pipeline: pixel-diff -> changed (score=%.2f), calling cloud API", diff_score)
        try:
            analysis = analyze_frame(frame_path, api_key, anthropic_key)
        except RuntimeError as e:
            log.error("pipeline: analysis failed, aborting - %s", e)
            _output("error", error=str(e), frame=str(frame_path))
            return 1

        flat = flatten_analysis(analysis, str(frame_path))
        flat["diffScore"] = round(diff_score, 2) if diff_score >= 0 else None

        # Heuristic: if state is "Unknown", baby is present, and position matches
        # the previous frame, infer "Asleep" (baby hasn't moved, likely sleeping)
        if flat.get("state") == "Unknown" and flat.get("babyPresent"):
            prev = get_last_entry()
            if prev and prev.get("babyPresent") and prev.get("sleepPosition") == flat.get("sleepPosition"):
                if flat.get("sleepPosition") != "Unknown":
                    log.info("heuristic: state Unknown -> Asleep (position unchanged: %s)", flat.get("sleepPosition"))
                    flat["state"] = "Asleep"
                    flat["stateInferred"] = True

        # Update head position from cloud API (for birdeye shadow)
        head_pos = flat.get("headPosition")
        if isinstance(head_pos, dict) and head_pos.get("visible", False):
            from lib.classifiers import save_head_state
            save_head_state(head_pos["x"], head_pos["y"], source="cloud-api")

    # --- Compare shadow birdeye vs production result ---
    if shadow_birdeye is not None:
        prod_state = flat.get("state", "Unknown")
        if not flat.get("babyPresent", False):
            prod_state = "not_present"
        birdeye_state = shadow_birdeye.get("state", "Unknown")
        if not shadow_birdeye.get("babyPresent", False):
            birdeye_state = "not_present"

        agreed = birdeye_state.lower() == prod_state.lower()
        flat["shadow"] = {
            "birdeyeState": birdeye_state,
            "prodState": prod_state,
            "agreed": agreed,
            "presenceConfidence": shadow_birdeye.get("presenceConfidence"),
            "eyeConfidence": shadow_birdeye.get("eyeConfidence"),
            "eyeState": shadow_birdeye.get("eyeState"),
            "birdeyeTimings": shadow_birdeye.get("birdeyeTimings"),
            "fallback": shadow_birdeye.get("fallback"),
        }
        # Promote face detection data to top level for dashboard overlay + training
        if shadow_birdeye.get("faceBbox"):
            flat["faceBbox"] = shadow_birdeye["faceBbox"]
        if shadow_birdeye.get("faceConfidence") is not None:
            flat["faceConfidence"] = shadow_birdeye["faceConfidence"]
        # Track which model version produced this shadow result
        training_log = Path(__file__).resolve().parent.parent / "pipeline" / "models" / "training-log.jsonl"
        if training_log.exists():
            last_line = training_log.read_text().strip().splitlines()[-1:]
            if last_line:
                flat["shadowModelVersion"] = json.loads(last_line[0]).get("version")
        if agreed:
            log.info("shadow: AGREE birdeye=%s prod=%s", birdeye_state, prod_state)
        else:
            log.warning("shadow: DISAGREE birdeye=%s prod=%s", birdeye_state, prod_state)

    alerts = check_alerts(flat)

    # Build flat log entry
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {"timestamp": now, "frame": str(frame_path), **flat, "alerts": alerts}

    # Log to JSONL
    if args.dry_run:
        log.info("pipeline: dry-run, skipping write")
    else:
        append_entry(entry)  # JSONL backup
        db = get_db()
        db.insert_entry(entry)  # SQLite primary
        log.info("pipeline: logged entry at %s", now)
    elapsed = time.monotonic() - t_start

    # --- Safety alerts ---
    if not args.dry_run:
        check_edge_alert(entry, env)

    # --- Active wake detection (look-back confirmation, no extra captures) ---
    if not args.dry_run:
        if entry.get("babyPresent"):
            if should_burst(entry):
                wake_alert = check_wake_confirmation(entry)
                if wake_alert:
                    log.info("pipeline: ACTIVE WAKE confirmed (%d/%d Awake)",
                             wake_alert["awake_count"], wake_alert["total_frames"])
                    ts_str = wake_alert["timestamp"]
                    try:
                        ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        import zoneinfo
                        et_tz = zoneinfo.ZoneInfo("America/New_York")
                        local_time = ts_dt.astimezone(et_tz).strftime("%I:%M %p")
                    except Exception:
                        local_time = ts_str
                    alert_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                    wake_msg = (
                        f"🍼 Baby waking up!\n"
                        f"Confirmed: {wake_alert['awake_count']}/{wake_alert['total_frames']} "
                        f"recent frames show Awake\n"
                        f"First detected at {local_time}\n\n"
                        f"Was this correct?"
                    )
                    send_telegram_alert(wake_msg, env, alert_id=alert_id)
                    save_alert_state("active_wake")
                    log_alert_feedback(alert_id, wake_alert)
        else:
            # Baby removed — reset cooldown so next wake can trigger
            reset_wake_cooldown()

    # --- Structured run summary (system.log + stdout) ---
    detection_method = flat.get("detectionMethod", "pixel-diff")
    state = flat.get("state", "unknown")
    model_used = flat.get("modelUsed", "n/a")
    birdeye_timings = flat.get("birdeyeTimings", {})

    log.info("RUN_SUMMARY method=%s state=%s model=%s elapsed=%.1fs baby=%s alerts=%d",
             detection_method, state, model_used, elapsed,
             flat.get("babyPresent", "?"), len(alerts))

    # Enriched JSON output (goes to cron-stdout.log)
    output_extras = {
        "detectionMethod": detection_method,
        "state": state,
        "modelUsed": model_used,
        "elapsed": round(elapsed, 2),
    }
    if birdeye_timings:
        output_extras["birdeyeTimings"] = birdeye_timings
    if flat.get("presenceConfidence") is not None:
        output_extras["presenceConfidence"] = flat["presenceConfidence"]
    if flat.get("eyeConfidence") is not None:
        output_extras["eyeConfidence"] = flat["eyeConfidence"]

    if alerts:
        summary = "ALERT: " + "; ".join(alerts)
        log.warning("pipeline: %d alerts detected: %s", len(alerts), summary)
        _output("alert", frame=str(frame_path), alerts=alerts, summary=summary, **output_extras)
    else:
        _output("ok", frame=str(frame_path), alerts=[], summary="No safety alerts.", **output_extras)

    log.info("--- run end: %.1fs ---", elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
