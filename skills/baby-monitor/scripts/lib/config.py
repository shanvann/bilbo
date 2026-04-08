"""Constants, paths, model chain config, and logging setup."""

import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SKILL_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = SKILL_DIR / "data"
FRAMES_DIR = DATA_DIR / "frames"
LOG_FILE = DATA_DIR / "system.log"
JSONL_FILE = DATA_DIR / "sleep-log.jsonl"
PROMPT_FILE = SKILL_DIR / "references" / "prompt.md"
ENV_FILE = Path("/Users/shanit/.openclaw/workspace/.env.baby-monitor")
MODELS_DIR = SKILL_DIR / "pipeline" / "models"

MAX_FRAMES_KB = 6 * 1024 * 1024  # 6 GB (~7 days at 1-min intervals)
REFS_DIR = DATA_DIR / "references"

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
WAKE_WINDOW = 6  # number of recent entries to analyze

BURST_AWAKE_THRESHOLD = 2  # minimum Awake readings (out of last 3 entries) to confirm wake

# ---------------------------------------------------------------------------
# Edge alert
# ---------------------------------------------------------------------------
EDGE_ALERT_COOLDOWN_MIN = 30

# ---------------------------------------------------------------------------
# Alert state files
# ---------------------------------------------------------------------------
ALERT_STATE_FILE = DATA_DIR / "alert-state.json"
ALERT_FEEDBACK_FILE = DATA_DIR / "alert-feedback.jsonl"
HEAD_STATE_FILE = DATA_DIR / "head-state.json"
CORRECTIONS_FILE = DATA_DIR / "corrections.jsonl"
AUDIT_LOG_FILE = DATA_DIR / "audit-log.jsonl"

# Audit settings
AUDIT_SAMPLE_SIZE = 50  # frames to spot-check per audit run

# ---------------------------------------------------------------------------
# Birdeye classifier config
# ---------------------------------------------------------------------------
# Fixed crop region for baby presence classifier (fraction of frame)
# Crops the center of the bassinet, excluding the walls
BASSINET_CROP = {"x": 0.15, "y": 0.10, "w": 0.70, "h": 0.80}
# Head crop size as fraction of frame dimensions (square crop around head center)
HEAD_CROP_SIZE = 0.30
# Default head position (center-upper area of bassinet) when no state file exists
DEFAULT_HEAD_POS = {"x": 0.50, "y": 0.35}
# Classifier model paths — load from "latest" symlink if it exists, else top-level
_MODELS_LATEST = MODELS_DIR / "latest"
_MODEL_BASE = _MODELS_LATEST if _MODELS_LATEST.exists() else MODELS_DIR
PRESENCE_MODEL = _MODEL_BASE / "presence_classifier.pt"
EYE_STATE_MODEL = _MODEL_BASE / "eye_state_classifier.pt"

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

_fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_fh.setFormatter(UTCFormatter(LOG_FMT))
log.addHandler(_fh)

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

def load_env(path: Path) -> dict:
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
