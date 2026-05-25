"""Constants, paths, model chain config, and logging setup.

Path layout is env-var-driven so the same code can run inside the Docker
containers (where everything roots at /app) and on a host for development
(where the user sets BILBO_ROOT or accepts the defaults below).

  BILBO_ROOT          — repo / install root. Default: /app inside Docker,
                        else the parent of the bilbo package (so `pip install -e .`
                        from the repo Just Works without an env var).
  BILBO_DATA_DIR      — SQLite, JSONL, frames, alert state, logs. Default $ROOT/data.
  BILBO_MODELS_DIR    — model weights + the `latest` symlink. Default $ROOT/pipeline/models.
  BILBO_ENV_FILE      — secrets file (Telegram, OpenAI, RTSP URL). Default $ROOT/.env.
  BILBO_REFERENCES_DIR — prompt + baby profile. Default $ROOT/references.
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (env-var driven, with sensible defaults for both Docker and host dev)
# ---------------------------------------------------------------------------
def _default_root() -> Path:
    """Resolve BILBO_ROOT default.

    /app is the right answer inside the Docker image. On a developer host
    (where /app doesn't exist), fall back to the repo root — two dirs up
    from this file (src/bilbo/config.py → repo/).
    """
    if Path("/app").exists():
        return Path("/app")
    return Path(__file__).resolve().parent.parent.parent

BILBO_ROOT = Path(os.environ.get("BILBO_ROOT", _default_root())).resolve()
DATA_DIR = Path(os.environ.get("BILBO_DATA_DIR", BILBO_ROOT / "data"))
MODELS_DIR = Path(os.environ.get("BILBO_MODELS_DIR", BILBO_ROOT / "pipeline" / "models"))
ENV_FILE = Path(os.environ.get("BILBO_ENV_FILE", BILBO_ROOT / ".env"))
REFERENCES_DIR = Path(os.environ.get("BILBO_REFERENCES_DIR", BILBO_ROOT / "references"))

FRAMES_DIR = DATA_DIR / "frames"
LOG_FILE = DATA_DIR / "system.log"
JSONL_FILE = DATA_DIR / "sleep-log.jsonl"
PROMPT_FILE = REFERENCES_DIR / "prompt.md"
REFS_DIR = DATA_DIR / "references"

MAX_FRAMES_KB = 10 * 1024 * 1024  # 10 GB (~17 days at 1-min intervals; was ~67 days at 4-min. Oldest-first pruning kicks in at the cap.)

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# ---------------------------------------------------------------------------
# Cloud API model chain (backup to birdeye — only called on ~2% of frames)
# Use the best model first since volume is low and accuracy matters.
# ---------------------------------------------------------------------------
MODEL_CHAIN = [
    {"provider": "openai", "model": "gpt-4o", "timeout": 30},
]

API_RETRIES = 2  # retries per model (before falling through to next)
CAPTURE_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Pixel-diff empty detection
# ---------------------------------------------------------------------------
# Calibrated against 811 labeled frames: 0 real baby misses at threshold=5.
PIXEL_DIFF_THRESHOLD = 5
PIXEL_DIFF_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Wake detection / burst confirmation
# ---------------------------------------------------------------------------
WAKE_SCORE_THRESHOLD = 4
WAKE_COOLDOWN_MIN = 30
ASLEEP_COOLDOWN_MIN = 30  # mirror of WAKE_COOLDOWN_MIN for Asleep-transition alerts
WAKE_WINDOW = 24  # number of recent entries to scan for a prior Asleep state
                  # gating the wake alert. At 1-min capture cadence this is
                  # ~24 minutes of lookback (was 6 = 24 min at the old 4-min
                  # cadence). Keeping the time semantic stable across
                  # sampling-rate changes — if you change the capture
                  # interval, scale this alongside it.

BURST_AWAKE_THRESHOLD = 2  # minimum Awake readings (out of last 3 entries) to confirm wake.
                           # Note: the '3' is hardcoded in alerts.check_wake_confirmation
                           # as `[-3:]`. At 1-min capture cadence this means
                           # confirmation takes ~3 minutes of consecutive
                           # captures; at 4-min it was ~12 minutes. If you
                           # want a wider confirmation window, refactor the
                           # hardcoded slice to be parameterized.
                           # NOTE: after the 2026-04-14 state-smoothing change
                           # (STATE_CONFIRM_* below), the primary `state` field
                           # is itself temporally confirmed (4-of-6 consecutive),
                           # so this 2-of-3 check is now trivially satisfied on
                           # any Asleep→Awake transition. Kept as a second
                           # guard and a cooldown/prior-Asleep gate.

# ---------------------------------------------------------------------------
# Temporal state smoothing (added 2026-04-14)
# ---------------------------------------------------------------------------
# The raw per-frame Awake/Asleep reading (from BIRDEYE's eyeState mapping or
# the cloud API's state field) is noisy — a single mis-classified frame or a
# brief eye-open blink during REM can flip the primary state. The primary
# `state` field is now only allowed to flip to Awake/Asleep when
# STATE_CONFIRM_RUN consecutive raw readings agree within the last
# STATE_CONFIRM_WINDOW baby-present frames. Between flips, the previous
# smoothed state is carried forward. The raw per-frame reading is preserved
# in `rawState` for future re-smoothing or backfill.
STATE_CONFIRM_WINDOW = 6
STATE_CONFIRM_RUN = 4

# Post-smoothing Unknown absorption: when a new Awake state is confirmed,
# any immediately-preceding contiguous run of Unknown (baby-present) frames
# whose total span is less than this many minutes is retroactively
# reclassified as Awake. Rationale: if BIRDEYE briefly lost the face (e.g.
# hand covering eyes, crib shift) and then caught 4 consecutive eyes_open,
# the gap was almost certainly "awake but momentarily unreadable".
# Asymmetric by design — the Unknown → Asleep direction is not applied,
# because pre-wake ambiguity is a weaker signal than the wake itself.
UNKNOWN_ABSORB_MAX_MINUTES = 15

# Putdown-pattern absorption: when a new Asleep state is confirmed AND the
# immediately-preceding contiguous Unknown+baby-present run is bookended by
# an out-of-bassinet (not_present) frame, classify the Unknown run based on
# its total span:
#   - span ≤ this many minutes → FallingAsleep (putdown-to-sleep transition)
#   - span  > this many minutes → Awake (baby was crib-awake before dozing off)
# Only triggers for the specific "put down → settled into sleep" pattern. Awake
# → Unknown → Asleep (no not_present on the prior side) is intentionally left
# alone — that's the same pre-sleep ambiguity the Unknown → Awake absorption
# is asymmetric about.
FALLING_ASLEEP_MAX_MINUTES = 30

# ---------------------------------------------------------------------------
# Edge alert
# ---------------------------------------------------------------------------
EDGE_ALERT_COOLDOWN_MIN = 30

# Global kill-switch for outbound Telegram alerts. When False, every call to
# `send_telegram_alert` short-circuits with a debug log instead of hitting the
# Bot API. Disabled 2026-04-30 while reliability work is in progress — flip
# back to True (and review wake/asleep/edge/safety/watchdog rules) before
# re-enabling notifications.
TELEGRAM_ALERTS_ENABLED = False

# ---------------------------------------------------------------------------
# Alert state files
# ---------------------------------------------------------------------------
ALERT_STATE_FILE = DATA_DIR / "alert-state.json"
ALERT_FEEDBACK_FILE = DATA_DIR / "alert-feedback.jsonl"
HEAD_STATE_FILE = DATA_DIR / "head-state.json"
CORRECTIONS_FILE = DATA_DIR / "corrections.jsonl"
AUDIT_LOG_FILE = DATA_DIR / "audit-log.jsonl"
TRAINING_STATE_FILE = DATA_DIR / "training-state.json"
WATCHDOG_STATE_FILE = DATA_DIR / "watchdog-state.json"

# ---------------------------------------------------------------------------
# Capture watchdog
# ---------------------------------------------------------------------------
# Fires a Telegram alert when the DB hasn't seen a new entry in this many
# minutes. Catches RTSP outages, launchd stalls, and script crashes — but
# NOT laptop-off (nothing runs at all in that case).
WATCHDOG_ALERT_AFTER_MIN = 5
# If an outage is still ongoing after the first alert, send one reminder
# every N minutes instead of going silent or spamming.
WATCHDOG_REMINDER_AFTER_MIN = 60

# Audit settings
AUDIT_SAMPLE_SIZE = 50  # frames to spot-check per --audit run

# Shadow mode: birdeye runs on every frame but results are logged,
# not used for decisions. Cloud API remains the production pipeline.
# When shadow agreement exceeds this threshold, birdeye can be promoted.
SHADOW_PROMOTION_THRESHOLD = 0.95  # 95% agreement to promote birdeye to prod

# ---------------------------------------------------------------------------
# Birdeye classifier config
# ---------------------------------------------------------------------------
# Fixed crop region for baby presence classifier (fraction of frame)
# Crops the center of the bassinet, excluding the walls
BASSINET_CROP = {"x": 0.15, "y": 0.10, "w": 0.70, "h": 0.80}
# Head crop size as fraction of frame dimensions (square crop around head center)
HEAD_CROP_SIZE = 0.30
# Eye-state classifier confidence threshold — below this, fall back to cloud API
EYE_STATE_CONFIDENCE_THRESHOLD = 0.7
# Face detector (YuNet) config — used as fallback
FACE_DETECT_MODEL = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
FACE_DETECT_SCORE_THRESHOLD = 0.5
FACE_DETECT_NMS_THRESHOLD = 0.3
FACE_CROP_PADDING = 0.3  # expand detected bbox by 30% on each side for context
# Default head position (center-upper area of bassinet) when no state file exists
DEFAULT_HEAD_POS = {"x": 0.50, "y": 0.35}
# Classifier model paths — load from "latest" symlink if it exists, else top-level
_MODELS_LATEST = MODELS_DIR / "latest"
_MODEL_BASE = _MODELS_LATEST if _MODELS_LATEST.exists() else MODELS_DIR
PRESENCE_MODEL = _MODEL_BASE / "presence_classifier.pt"
EYE_STATE_MODEL = _MODEL_BASE / "eye_state_classifier.pt"
# Trainable face detector (MobileNetV3-Small + bbox regression)
FACE_DETECT_MODEL_PT = _MODEL_BASE / "face_detector.pt"
FACE_DETECT_PT_CONFIDENCE_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# Model-adjacent metadata (sidecar JSON next to the weights)
# ---------------------------------------------------------------------------
#
# Some pipeline parameters are coupled to the deployed model — changing the
# model without changing them silently produces degraded predictions. The
# canonical example is the eye-state classifier's input resolution: the
# MobileNetV3-Small backbone accepts any spatial extent via its adaptive
# pool, so a 448-trained checkpoint will HAPPILY run at 224 input and vice
# versa, but the learned feature positions don't match and accuracy drops.
#
# To keep promotion / rollback atomic with the weights, these parameters
# live in ``pipeline/models/latest/meta.json`` alongside the checkpoint
# files. The `latest` symlink is the single source of truth — switching
# versions swaps the meta automatically.
#
# Shape of meta.json:
#     {
#       "eye_state_crop_size": 448,
#       "deployed_at": "2026-04-14T15:28:01Z",
#       "source": "eye_state_448 experiment",
#       "notes": "flipped from 224 after shadow showed +34 pts on corrections"
#     }
#
# Only ``eye_state_crop_size`` is read by the pipeline today; other fields
# are captured for archaeology and the promote_experiment script.
import json as _json
def _load_model_meta() -> dict:
    meta_path = _MODEL_BASE / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return _json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {}

_MODEL_META = _load_model_meta()

# Eye-state classifier input resolution (square). Read from the sidecar so
# a promotion can change it without editing Python source. Defaults to 224
# when the sidecar is absent — this is the conservative assumption because
# 224 was the torchvision default and all checkpoints before
# 2026-04-14 were trained at it.
EYE_STATE_INPUT_SIZE: int = int(_MODEL_META.get("eye_state_crop_size", 224))

# ---------------------------------------------------------------------------
# Legacy alert rules (currently disabled)
# ---------------------------------------------------------------------------
ALERT_RULES = {
    # "sleepPosition": {"Stomach"},  # Disabled — vision model misclassifies positions
    # "objectsInBassinet": {"Blanket", "Pillow", "Mixed"},  # Disabled by user
    # "hazards": {"Loose items", "Cords nearby"},  # Disabled by user
}

# Human-readable names for alert messages
ALERT_LABELS = {
    "sleepPosition": "Sleep Position",
    "objectsInBassinet": "Objects in bassinet",
    "hazards": "Hazards",
}

# ---------------------------------------------------------------------------
# Logging — file + optional stderr, stdout reserved for JSON output
# ---------------------------------------------------------------------------

class UTCFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


LOG_FMT = "[%(asctime)s] monitor: %(message)s"

log = logging.getLogger("monitor")
log.setLevel(logging.DEBUG)

# File handler is best-effort: in containers DATA_DIR is a bind mount that
# always exists; on a fresh host import the directory may not exist yet, in
# which case we log to stderr only rather than crashing at import time.
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, delay=True)
    _fh.setFormatter(UTCFormatter(LOG_FMT))
    log.addHandler(_fh)
except OSError:
    pass

_sh = logging.StreamHandler(sys.stderr)
_sh.setLevel(logging.WARNING)
_sh.setFormatter(UTCFormatter(LOG_FMT))
log.addHandler(_sh)


def set_verbose():
    """Enable debug-level output to stderr."""
    _sh.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

_ENV_KEYS = (
    "RTSP_STREAM_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET",
)


def load_env(path: Path) -> dict:
    """Load secrets from `path`; fall back to os.environ if the file is missing.

    Docker compose `env_file:` injects the same vars into the container env,
    so containers can run without a bind-mounted .env file. On a host dev
    setup the file is the canonical source.
    """
    if path.exists():
        log.debug("loading env from %s", path)
        env = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            env[key] = value.strip().strip('"').strip("'")
            log.debug("env: %s=%s", key, "***" if "KEY" in key or "SECRET" in key or "PASSWORD" in key else env[key][:40])
        log.info("env loaded: %d vars from %s", len(env), path.name)
        return env

    env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    if env:
        log.info("env loaded: %d vars from os.environ (file %s not found)",
                 len(env), path)
    else:
        log.warning("env empty: %s missing and no matching os.environ keys", path)
    return env
