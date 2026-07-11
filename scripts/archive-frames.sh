#!/usr/bin/env bash
# Archive data/frames/ to external storage, indefinitely, ahead of the local
# 10GB/~17-day retention cap in bilbo.config.MAX_FRAMES_KB.
#
# Local retention is intentionally left untouched (fast working set for the
# dashboard/training container) — this script just mirrors frames out to
# external storage on a schedule (crontab) before the local cap prunes them.
# See docs/design-decisions.md > Frame Retention.
#
# Usage:   ./scripts/archive-frames.sh
# Crontab: 0 3 * * *  /Users/shanit/projects/bilbo/scripts/archive-frames.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT}/data/frames/"
DEST_VOLUME="/Volumes/TOSHIBA EXT"
DEST="${DEST_VOLUME}/bilbo-frames/"
LOG_FILE="${ROOT}/data/frame-archive.log"
ENV_FILE="${ROOT}/.env"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >>"${LOG_FILE}"; }

# The Toshiba enclosure has its own idle-spindown timer (independent of
# macOS's disksleep, which is disabled) — after a long idle stretch (e.g.
# overnight before the midnight cron run) the mount point can be present
# but unresponsive for the ~5-30s it takes the platters to spin back up.
# Give it a bounded window of exponential-backoff retries instead of
# failing on the first touch.
SPINUP_TIMEOUT_SECONDS=90
SPINUP_INITIAL_DELAY_SECONDS=5

retry_with_backoff() {
  local timeout="$1" delay="$2"
  shift 2
  local start elapsed attempt=1
  start="$(date +%s)"
  until "$@" 2>/dev/null; do
    elapsed=$(( $(date +%s) - start ))
    if (( elapsed >= timeout )); then
      return 1
    fi
    local remaining=$(( timeout - elapsed ))
    (( delay > remaining )) && delay="${remaining}"
    log "drive not ready yet (attempt ${attempt}, ${elapsed}s elapsed) — retrying in ${delay}s"
    sleep "${delay}"
    delay=$(( delay * 2 ))
    attempt=$(( attempt + 1 ))
  done
  if (( attempt > 1 )); then
    log "drive became responsive after ${attempt} attempt(s)"
  fi
}

# Sends regardless of bilbo.config.TELEGRAM_ALERTS_ENABLED — that kill switch
# gates baby-monitoring alerts (wake/asleep/edge/watchdog); this is an
# unrelated infra concern (backup silently stopped), not the reliability
# issue that flag was disabled for.
telegram_alert() {
  local msg="$1"
  [[ -f "${ENV_FILE}" ]] || return 0
  local token chat_id
  token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "${ENV_FILE}" | cut -d= -f2-)"
  chat_id="$(grep -E '^TELEGRAM_CHAT_ID=' "${ENV_FILE}" | cut -d= -f2-)"
  [[ -n "${token}" && -n "${chat_id}" ]] || return 0
  curl -fsS -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    -d "chat_id=${chat_id}" \
    --data-urlencode "text=${msg}" \
    >/dev/null 2>&1 || true
}

fail() {
  local msg="$1"
  log "ERROR: ${msg}"
  telegram_alert "⚠️ BILBO frame archive failed: ${msg}"
  exit 1
}

if [[ ! -d "${DEST_VOLUME}" ]]; then
  fail "${DEST_VOLUME} mount point doesn't exist. mount: $(mount | tr '\n' ';')"
fi

# Directory existing isn't proof the volume is actually responsive (a stale/
# hung USB mount can linger in the mount table) — do a real write probe.
PROBE="${DEST_VOLUME}/.bilbo-heartbeat"
probe_write() { ( : >"${PROBE}" ) 2>/dev/null && rm -f "${PROBE}" 2>/dev/null; }

if ! retry_with_backoff "${SPINUP_TIMEOUT_SECONDS}" "${SPINUP_INITIAL_DELAY_SECONDS}" probe_write; then
  fail "${DEST_VOLUME} exists but isn't writable after ${SPINUP_TIMEOUT_SECONDS}s of retries (drive may have failed to spin up). mount: $(mount | tr '\n' ';')"
fi

mkdir -p "${DEST}"

if ! STATS="$(rsync -a --stats "${SRC}" "${DEST}" 2>&1)"; then
  fail "rsync exited non-zero: $(printf '%s' "${STATS}" | tail -3 | tr '\n' ' ')"
fi
SUMMARY="$(printf '%s\n' "${STATS}" | grep -E 'Number of files transferred|Total transferred file size' || true)"
log "OK: synced ${SRC} -> ${DEST} | ${SUMMARY//$'\n'/, }"
