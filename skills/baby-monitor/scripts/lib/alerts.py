"""Wake detection, edge alerts, burst confirmation, telegram, alert feedback."""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    ALERT_FEEDBACK_FILE,
    ALERT_LABELS,
    ALERT_RULES,
    ALERT_STATE_FILE,
    BURST_AWAKE_THRESHOLD,
    BURST_CONFIRM_COUNT,
    BURST_INTERVAL_SEC,
    EDGE_ALERT_COOLDOWN_MIN,
    JSONL_FILE,
    WAKE_COOLDOWN_MIN,
    WAKE_WINDOW,
)
from .capture import capture_frame
from .storage import get_recent_entries
from .vision import _build_ssl_context, analyze_frame, flatten_analysis

log = logging.getLogger("monitor")


def check_edge_alert(entry: dict, env: dict):
    """Send immediate alert if baby is pressed against bassinet side."""
    if not entry.get("babyPresent"):
        return
    location = entry.get("bassinetLocation", "Unknown")
    if location != "Pressed against side":
        return

    # Check cooldown
    if ALERT_STATE_FILE.exists():
        try:
            state = json.loads(ALERT_STATE_FILE.read_text())
            last_alert = state.get("lastEdgeAlert")
            if last_alert:
                last_ts = datetime.fromisoformat(last_alert.replace("Z", "+00:00")).replace(tzinfo=None)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                if (now - last_ts).total_seconds() / 60 < EDGE_ALERT_COOLDOWN_MIN:
                    log.info("edge-alert: pressed against side but in cooldown")
                    return
        except Exception:
            pass

    import zoneinfo
    ts_str = entry.get("timestamp", "")
    try:
        ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        et_tz = zoneinfo.ZoneInfo("America/New_York")
        local_time = ts_dt.astimezone(et_tz).strftime("%I:%M %p")
    except Exception:
        local_time = ts_str

    msg = (
        f"⚠️ Baby pressed against bassinet side!\n"
        f"Detected at {local_time}\n"
        f"Position: {entry.get('sleepPosition', 'Unknown')}"
    )
    log.warning("edge-alert: baby pressed against bassinet side at %s", local_time)
    send_telegram_alert(msg, env)

    # Save cooldown
    state = {}
    if ALERT_STATE_FILE.exists():
        try:
            state = json.loads(ALERT_STATE_FILE.read_text())
        except Exception:
            pass
    state["lastEdgeAlert"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))


def should_burst(current_entry: dict) -> bool:
    """Check if current entry should trigger burst confirmation.

    Triggers when:
      - Baby is present and state is "Awake"
      - Previous entries show baby was "Asleep" (waking from sleep, not just placed)
      - Not in cooldown from a recent alert
    """
    if not current_entry.get("babyPresent"):
        return False
    if current_entry.get("state") != "Awake":
        return False

    # Check that baby was sleeping recently (not just placed awake)
    recent = get_recent_entries(WAKE_WINDOW)
    recent_present = [e for e in recent if e.get("babyPresent")]
    if not recent_present:
        return False
    states = [e.get("state", "Unknown") for e in recent_present]
    if "Asleep" not in states:
        log.debug("burst: Awake detected but no prior Asleep in window, skipping")
        return False

    # Check cooldown
    if ALERT_STATE_FILE.exists():
        try:
            alert_state = json.loads(ALERT_STATE_FILE.read_text())
            last_alert = alert_state.get("lastActiveWakeAlert")
            if last_alert:
                last_ts = datetime.fromisoformat(last_alert.replace("Z", "+00:00")).replace(tzinfo=None)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                elapsed_min = (now - last_ts).total_seconds() / 60
                if elapsed_min < WAKE_COOLDOWN_MIN:
                    log.info("burst: Awake detected but in cooldown (%.0f min since last alert)",
                             elapsed_min)
                    return False
        except Exception as e:
            log.debug("burst: error reading alert state: %s", e)

    log.info("burst: Awake detected after sleep — triggering burst confirmation")
    return True


def run_burst_confirmation(rtsp_url: str, api_key: str, anthropic_key: str, trigger_entry: dict) -> dict | None:
    """Capture and analyze 2 additional frames at 60s intervals to confirm wake.

    Returns alert dict if confirmed (2+ of 3 frames show Awake), None otherwise.
    All burst frames are logged to JSONL.
    """
    all_states = [trigger_entry.get("state", "Unknown")]
    log.info("burst: starting confirmation (2 frames at %ds intervals)", BURST_INTERVAL_SEC)

    for burst_i in range(BURST_CONFIRM_COUNT):
        log.info("burst: waiting %ds before confirmation frame %d/%d",
                 BURST_INTERVAL_SEC, burst_i + 1, BURST_CONFIRM_COUNT)
        time.sleep(BURST_INTERVAL_SEC)

        # Capture
        try:
            frame_path = capture_frame(rtsp_url)
        except RuntimeError as e:
            log.error("burst: capture failed for frame %d: %s", burst_i + 1, e)
            all_states.append("Unknown")
            continue

        # Analyze
        try:
            analysis = analyze_frame(frame_path, api_key, anthropic_key)
        except RuntimeError as e:
            log.error("burst: analysis failed for frame %d: %s", burst_i + 1, e)
            all_states.append("Unknown")
            continue

        flat = flatten_analysis(analysis, str(frame_path))
        burst_state = flat.get("state", "Unknown")
        all_states.append(burst_state)

        # Log burst frame to JSONL
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        burst_entry = {"timestamp": now, "frame": str(frame_path), **flat,
                       "alerts": [], "burstFrame": True, "burstIndex": burst_i + 1}
        entry_json = json.dumps(burst_entry)
        with open(JSONL_FILE, "a") as f:
            f.write(entry_json + "\n")
        log.info("burst: frame %d/%d state=%s position=%s (logged)",
                 burst_i + 1, BURST_CONFIRM_COUNT, burst_state,
                 flat.get("sleepPosition", "?"))

    # Evaluate: how many of the 3 frames showed Awake?
    awake_count = all_states.count("Awake")
    log.info("burst: confirmation complete — states=%s awake=%d/%d threshold=%d",
             all_states, awake_count, len(all_states), BURST_AWAKE_THRESHOLD)

    if awake_count >= BURST_AWAKE_THRESHOLD:
        log.info("burst: CONFIRMED active wake (%d/%d Awake)", awake_count, len(all_states))
        return {
            "type": "active_wake",
            "burst_states": all_states,
            "awake_count": awake_count,
            "total_frames": len(all_states),
            "last_state": all_states[-1],
            "last_position": trigger_entry.get("sleepPosition", "Unknown"),
            "timestamp": trigger_entry.get("timestamp", ""),
        }
    else:
        log.info("burst: NOT confirmed (%d/%d Awake) — suppressing alert", awake_count, len(all_states))
        return None


def save_alert_state(alert_type: str):
    """Update alert state file with timestamp."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {}
    if ALERT_STATE_FILE.exists():
        try:
            state = json.loads(ALERT_STATE_FILE.read_text())
        except Exception:
            pass
    if alert_type == "active_wake":
        state["lastActiveWakeAlert"] = now
    ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))
    log.debug("alert-state: saved %s at %s", alert_type, now)


def log_alert_feedback(alert_id: str, wake_alert: dict):
    """Log an alert for later feedback tracking."""
    entry = {
        "alertId": alert_id,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "burstStates": wake_alert.get("burst_states", []),
        "awakeCount": wake_alert.get("awake_count", 0),
        "totalFrames": wake_alert.get("total_frames", 0),
        "feedback": None,  # filled in when user responds
    }
    with open(ALERT_FEEDBACK_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    log.info("alert-feedback: logged alert %s for feedback", alert_id)


def record_alert_feedback(alert_id: str, feedback: str) -> bool:
    """Record user feedback (yes/no) for an alert. Called externally."""
    if not ALERT_FEEDBACK_FILE.exists():
        return False
    lines = ALERT_FEEDBACK_FILE.read_text().strip().splitlines()
    updated = False
    new_lines = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("alertId") == alert_id and entry.get("feedback") is None:
            entry["feedback"] = feedback
            entry["feedbackAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            updated = True
        new_lines.append(json.dumps(entry))
    if updated:
        ALERT_FEEDBACK_FILE.write_text("\n".join(new_lines) + "\n")
    return updated


def get_alert_stats() -> dict:
    """Get alert accuracy stats from feedback data."""
    if not ALERT_FEEDBACK_FILE.exists():
        return {"total": 0, "yes": 0, "no": 0, "pending": 0}
    lines = ALERT_FEEDBACK_FILE.read_text().strip().splitlines()
    total = len(lines)
    yes = sum(1 for l in lines if json.loads(l).get("feedback") == "yes")
    no = sum(1 for l in lines if json.loads(l).get("feedback") == "no")
    pending = sum(1 for l in lines if json.loads(l).get("feedback") is None)
    return {"total": total, "yes": yes, "no": no, "pending": pending,
            "precision": f"{yes*100//(yes+no)}%" if (yes+no) > 0 else "N/A"}


def reset_wake_cooldown():
    """Reset wake alert cooldown (called when baby is taken out)."""
    if ALERT_STATE_FILE.exists():
        try:
            state = json.loads(ALERT_STATE_FILE.read_text())
            if "lastActiveWakeAlert" in state:
                del state["lastActiveWakeAlert"]
                ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))
                log.debug("alert-state: reset active wake cooldown (baby removed)")
        except Exception:
            pass


def send_telegram_alert(message: str, env: dict, alert_id: str = None):
    """Send alert via Telegram Bot API, optionally with feedback buttons."""
    bot_token = env.get("TELEGRAM_BOT_TOKEN") or env.get("BILBO_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        log.warning("telegram-alert: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in env")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload_dict = {"chat_id": chat_id, "text": message}

    if alert_id:
        payload_dict["reply_markup"] = {
            "inline_keyboard": [
                [
                    {"text": "✅ Yes, awake", "callback_data": f"wake_yes:{alert_id}"},
                    {"text": "❌ No, false alarm", "callback_data": f"wake_no:{alert_id}"},
                ]
            ]
        }

    payload = json.dumps(payload_dict).encode()
    headers = {"Content-Type": "application/json"}

    try:
        ctx = _build_ssl_context()
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            log.info("telegram-alert: sent successfully (HTTP %d)", resp.status)
            return True
    except Exception as e:
        log.error("telegram-alert: failed to send: %s", e)
        return False


def check_alerts(flat: dict) -> list[str]:
    """Check flat entry against legacy alert rules."""
    alerts = []
    checked = 0
    for field, trigger_values in ALERT_RULES.items():
        value = flat.get(field, "Unknown")
        checked += 1
        if value in trigger_values:
            label = ALERT_LABELS.get(field, field)
            log.debug("alert: %s=%s (rule matched)", field, value)
            alerts.append(f"{label}: {value}")
    log.debug("alert check: %d rules evaluated, %d alerts triggered", checked, len(alerts))
    return alerts
