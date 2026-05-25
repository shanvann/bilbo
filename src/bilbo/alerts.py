"""Wake detection, edge alerts, burst confirmation, telegram, alert feedback."""

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bilbo.config import (
    ALERT_FEEDBACK_FILE,
    ALERT_LABELS,
    ALERT_RULES,
    ALERT_STATE_FILE,
    ASLEEP_COOLDOWN_MIN,
    BURST_AWAKE_THRESHOLD,
    EDGE_ALERT_COOLDOWN_MIN,
    JSONL_FILE,
    TELEGRAM_ALERTS_ENABLED,
    WAKE_COOLDOWN_MIN,
    WAKE_WINDOW,
)
from bilbo.storage.db import get_db
from bilbo.pipeline.vision import _build_ssl_context

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
    recent = get_db().get_recent_entries(WAKE_WINDOW)
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


def should_alert_asleep(current_entry: dict) -> bool:
    """Mirror of should_burst, for Asleep-transition alerts.

    Triggers when:
      - Baby is present and smoothed state is "Asleep"
      - Previous entries show baby was "Awake" recently (drifted off,
        not placed-already-asleep)
      - Not in cooldown from a recent asleep alert
    """
    if not current_entry.get("babyPresent"):
        return False
    if current_entry.get("state") != "Asleep":
        return False

    recent = get_db().get_recent_entries(WAKE_WINDOW)
    recent_present = [e for e in recent if e.get("babyPresent")]
    if not recent_present:
        return False
    states = [e.get("state", "Unknown") for e in recent_present]
    if "Awake" not in states:
        log.debug("asleep-alert: Asleep detected but no prior Awake in window, skipping")
        return False

    if ALERT_STATE_FILE.exists():
        try:
            alert_state = json.loads(ALERT_STATE_FILE.read_text())
            last_alert = alert_state.get("lastAsleepAlert")
            if last_alert:
                last_ts = datetime.fromisoformat(last_alert.replace("Z", "+00:00")).replace(tzinfo=None)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                elapsed_min = (now - last_ts).total_seconds() / 60
                if elapsed_min < ASLEEP_COOLDOWN_MIN:
                    log.info("asleep-alert: Asleep detected but in cooldown (%.0f min since last alert)",
                             elapsed_min)
                    return False
        except Exception as e:
            log.debug("asleep-alert: error reading alert state: %s", e)

    log.info("asleep-alert: Asleep confirmed after Awake — triggering notification")
    return True


def check_asleep_confirmation(current_entry: dict) -> dict | None:
    """Confirm Asleep transition by looking at recent smoothed states.

    Mirrors check_wake_confirmation. The smoothed `state` field is already
    temporally confirmed (STATE_CONFIRM_RUN of STATE_CONFIRM_WINDOW), so
    this is a light second guard — 2+ of the last 3 baby-present frames
    smoothed to Asleep.
    """
    recent = get_db().get_recent_entries(6)
    present = [e for e in recent if e.get("babyPresent")][-3:]
    all_states = [e.get("state", "Unknown") for e in present]

    asleep_count = all_states.count("Asleep")
    log.info("asleep-confirm: recent states=%s asleep=%d/%d threshold=%d",
             all_states, asleep_count, len(all_states), BURST_AWAKE_THRESHOLD)

    if asleep_count >= BURST_AWAKE_THRESHOLD:
        log.info("asleep-confirm: CONFIRMED asleep (%d/%d Asleep)", asleep_count, len(all_states))
        return {
            "type": "asleep",
            "states": all_states,
            "asleep_count": asleep_count,
            "total_frames": len(all_states),
            "last_state": all_states[-1] if all_states else "Unknown",
            "last_position": current_entry.get("sleepPosition", "Unknown"),
            "timestamp": current_entry.get("timestamp", ""),
        }
    log.info("asleep-confirm: NOT confirmed (%d/%d Asleep) — suppressing",
             asleep_count, len(all_states))
    return None


def check_wake_confirmation(current_entry: dict) -> dict | None:
    """Confirm wake by looking at recent natural entries (no extra captures).

    With 1-min capture intervals, the last 3 entries span ~3 minutes — enough
    to confirm a real wake vs a single noisy frame. No blocking sleep needed.

    Returns alert dict if confirmed (2+ of last 3 entries show Awake), None otherwise.
    """
    # By the time check_wake_confirmation runs, monitor.py has already
    # persisted `current_entry` via db.insert_entry — so it's already in
    # the result of get_recent_entries(). Fetch a slightly larger window
    # so "last 3 baby-present" can survive an interleaved not_present
    # frame without shrinking. Don't re-append current_entry; that used
    # to double-count it when the JSONL path was in use.
    recent = get_db().get_recent_entries(6)
    present = [e for e in recent if e.get("babyPresent")][-3:]
    all_states = [e.get("state", "Unknown") for e in present]

    awake_count = all_states.count("Awake")
    log.info("wake-confirm: recent states=%s awake=%d/%d threshold=%d",
             all_states, awake_count, len(all_states), BURST_AWAKE_THRESHOLD)

    if awake_count >= BURST_AWAKE_THRESHOLD:
        log.info("wake-confirm: CONFIRMED active wake (%d/%d Awake)", awake_count, len(all_states))
        return {
            "type": "active_wake",
            "burst_states": all_states,
            "awake_count": awake_count,
            "total_frames": len(all_states),
            "last_state": all_states[-1] if all_states else "Unknown",
            "last_position": current_entry.get("sleepPosition", "Unknown"),
            "timestamp": current_entry.get("timestamp", ""),
        }
    else:
        log.info("wake-confirm: NOT confirmed (%d/%d Awake) — suppressing", awake_count, len(all_states))
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
    elif alert_type == "asleep":
        state["lastAsleepAlert"] = now
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
    """Reset wake + asleep alert cooldowns (called when baby is taken out)."""
    if not ALERT_STATE_FILE.exists():
        return
    try:
        state = json.loads(ALERT_STATE_FILE.read_text())
        changed = False
        for key in ("lastActiveWakeAlert", "lastAsleepAlert"):
            if key in state:
                del state[key]
                changed = True
        if changed:
            ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))
            log.debug("alert-state: reset wake + asleep cooldowns (baby removed)")
    except Exception:
        pass


def send_telegram_alert(message: str, env: dict, alert_id: str = None):
    """Send alert via Telegram Bot API, optionally with feedback buttons."""
    # Global kill-switch — see lib/config.py::TELEGRAM_ALERTS_ENABLED. Gating
    # here (rather than at every call site in monitor.py / watchdog.py) means
    # one flag silences wake, asleep, edge, safety, AND watchdog notifications.
    if not TELEGRAM_ALERTS_ENABLED:
        log.info("telegram-alert: suppressed (TELEGRAM_ALERTS_ENABLED=False) — would have sent: %s",
                 message.splitlines()[0][:120] if message else "")
        return False

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


# NOTE: edge alert (`check_edge_alert` above) reads `entry["bassinetLocation"]`,
# a field only the cloud API populates. Post BIRDEYE-primary flip the cloud API
# only runs on BIRDEYE fallback (~0.6% of frames), so the edge alert is
# effectively disabled until a trained BassinetLocationClassifier ships —
# see github issue #3 for the implementation plan. A geometric stopgap was
# tried (face_cy > 0.70 + presence + persistence) and backtested at recall
# 0.79 / precision 0.06 → 42 alerts/wk on ~3 true events/wk, far too noisy
# to ship.
