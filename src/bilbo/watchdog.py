"""Capture watchdog — Telegram alert when the monitor stops capturing.

Runs as its own launchd job (com.baby-monitor-watchdog) every 2 min.
Looks at the newest timestamp in monitor.db; if it's older than
WATCHDOG_ALERT_AFTER_MIN, treats it as an outage and notifies via
Telegram. Tracks outage_started_at / last_alert_at in a tiny JSON
state file so a multi-hour outage doesn't spam.

Covers: RTSP network unreachable, launchd stall, monitor.py crash.
Does NOT cover: laptop off/unplugged/asleep — nothing runs in that
case. A push-style cloud heartbeat would be needed for that.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from bilbo.alerts import send_telegram_alert
from bilbo.config import (
    ENV_FILE,
    WATCHDOG_ALERT_AFTER_MIN,
    WATCHDOG_REMINDER_AFTER_MIN,
    WATCHDOG_STATE_FILE,
    load_env,
)
from bilbo.storage.db import get_last_entry


log = logging.getLogger("watchdog")


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_state() -> dict:
    if WATCHDOG_STATE_FILE.exists():
        try:
            return json.loads(WATCHDOG_STATE_FILE.read_text())
        except Exception as e:
            log.warning("corrupt state file, resetting: %s", e)
    return {}


def _save_state(state: dict) -> None:
    WATCHDOG_STATE_FILE.write_text(json.dumps(state, indent=2))


def run_once() -> int:
    env = load_env(ENV_FILE) if ENV_FILE.exists() else {}
    state = _load_state()
    now = datetime.now(timezone.utc)

    last = get_last_entry()
    if not last or not last.get("timestamp"):
        log.warning("no entries in DB; skipping (fresh install?)")
        return 0

    last_ts = _parse_iso(last["timestamp"])
    age_min = (now - last_ts).total_seconds() / 60
    outage_started = state.get("outage_started_at")
    last_alert_at = state.get("last_alert_at")
    last_alert_kind = state.get("last_alert_kind")

    if age_min >= WATCHDOG_ALERT_AFTER_MIN:
        # In outage.
        if outage_started is None:
            msg = (
                f"⚠️ BILBO hasn't captured a frame in {int(age_min)} min.\n"
                f"Last frame: {last['timestamp']}"
            )
            sent = send_telegram_alert(msg, env)
            state["outage_started_at"] = _iso_now()
            if sent:
                state["last_alert_at"] = _iso_now()
                state["last_alert_kind"] = "outage_start"
            log.warning(
                "outage_start age=%.1fmin telegram=%s", age_min, "ok" if sent else "failed",
            )
            _save_state(state)
            return 0

        # Outage still ongoing — send a reminder every REMINDER_AFTER_MIN.
        should_remind = False
        if last_alert_at:
            elapsed = (now - _parse_iso(last_alert_at)).total_seconds() / 60
            if elapsed >= WATCHDOG_REMINDER_AFTER_MIN:
                should_remind = True
        else:
            # Initial alert failed to send; retry.
            should_remind = True

        if should_remind:
            outage_min = (now - _parse_iso(outage_started)).total_seconds() / 60
            msg = (
                f"⚠️ BILBO still down — no capture in {int(outage_min)} min.\n"
                f"Last frame: {last['timestamp']}"
            )
            sent = send_telegram_alert(msg, env)
            if sent:
                state["last_alert_at"] = _iso_now()
                state["last_alert_kind"] = "outage_reminder"
            log.warning(
                "outage_reminder outage=%.0fmin telegram=%s",
                outage_min, "ok" if sent else "failed",
            )
            _save_state(state)
        else:
            log.info("outage ongoing age=%.1fmin (silent until reminder)", age_min)
        return 0

    # Healthy capture (age < threshold).
    if outage_started is not None:
        outage_min = (last_ts - _parse_iso(outage_started)).total_seconds() / 60
        msg = f"✅ BILBO captures resumed after {int(outage_min)} min of downtime."
        sent = send_telegram_alert(msg, env)
        log.info("recovery outage=%.0fmin telegram=%s", outage_min, "ok" if sent else "failed")
        _save_state({})
    else:
        log.info("healthy age=%.1fmin", age_min)
    return 0


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)sZ] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    try:
        sys.exit(run_once())
    except Exception:
        log.exception("watchdog crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
