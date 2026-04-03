"""Constants, paths, model chain config, and logging setup."""

import logging
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

MAX_FRAMES_KB = 1 * 1024 * 1024  # 1 GB
REFS_DIR = DATA_DIR / "references"

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# ---------------------------------------------------------------------------
# Model fallback chain: primary -> fallback1 -> fallback2
# ---------------------------------------------------------------------------
MODEL_CHAIN = [
    {"provider": "openai", "model": "gpt-4o-mini", "timeout": 20},
    {"provider": "openai", "model": "gpt-4o", "timeout": 30},
    {"provider": "anthropic", "model": "claude-sonnet-4-6", "timeout": 60},
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

BURST_INTERVAL_SEC = 60   # seconds between burst captures
BURST_CONFIRM_COUNT = 2   # number of confirmation frames
BURST_AWAKE_THRESHOLD = 2  # minimum Awake readings (out of 3 total) to confirm wake

# ---------------------------------------------------------------------------
# Edge alert
# ---------------------------------------------------------------------------
EDGE_ALERT_COOLDOWN_MIN = 30

# ---------------------------------------------------------------------------
# Alert state files
# ---------------------------------------------------------------------------
ALERT_STATE_FILE = DATA_DIR / "alert-state.json"
ALERT_FEEDBACK_FILE = DATA_DIR / "alert-feedback.jsonl"

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

_fh = logging.FileHandler(LOG_FILE)
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
