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

import argparse
import base64
import json
import logging
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
FRAMES_DIR = DATA_DIR / "frames"
LOG_FILE = DATA_DIR / "system.log"
JSONL_FILE = DATA_DIR / "sleep-log.jsonl"
PROMPT_FILE = SKILL_DIR / "references" / "prompt.md"
ENV_FILE = Path("~/.openclaw/workspace/.env.baby-monitor")

MAX_FRAMES_KB = 1 * 1024 * 1024  # 1 GB
REFS_DIR = DATA_DIR / "references"

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Model fallback chain: primary → fallback1 → fallback2
MODEL_CHAIN = [
    {"provider": "openai", "model": "gpt-4o-mini", "timeout": 20},
    {"provider": "openai", "model": "gpt-4o", "timeout": 30},
    {"provider": "anthropic", "model": "claude-sonnet-4-6", "timeout": 60},
]

API_RETRIES = 2  # retries per model (before falling through to next)
CAPTURE_TIMEOUT = 30

# Pixel-diff empty detection: skip API if diff between current and previous
# frame is below this threshold (when previous was also empty).
# Calibrated against 811 labeled frames: 0 real baby misses at threshold=5.
PIXEL_DIFF_THRESHOLD = 5
PIXEL_DIFF_TIMEOUT = 10

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


# ---------------------------------------------------------------------------
# Frame capture
# ---------------------------------------------------------------------------

def capture_frame(rtsp_url: str) -> Path:
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = FRAMES_DIR / f"frame_{timestamp}.jpg"

    log.info("capture: starting -> %s", output)
    log.debug("capture: rtsp_url=%s timeout=%ds", rtsp_url.split("@")[-1], CAPTURE_TIMEOUT)
    t0 = time.monotonic()
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "fatal",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1",
            "-q:v", "2",
            str(output),
        ]
        log.debug("capture: cmd=%s", " ".join(cmd[:6]) + " ...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CAPTURE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.error("capture: FAILED - ffmpeg timed out after %ds", CAPTURE_TIMEOUT)
        raise RuntimeError("ffmpeg timed out")

    elapsed = time.monotonic() - t0

    if not output.exists():
        msg = result.stderr.strip() or "no output file produced"
        log.error("capture: FAILED after %.1fs - exit_code=%d stderr=%s", elapsed, result.returncode, msg)
        raise RuntimeError(f"capture failed: {msg}")

    size_kb = output.stat().st_size // 1024
    log.info("capture: success (%dKB, %.1fs) -> %s", size_kb, elapsed, output)
    if size_kb < 10:
        log.warning("capture: frame suspiciously small (%dKB) — possible corrupt image", size_kb)
    return output


# ---------------------------------------------------------------------------
# Disk limit enforcement
# ---------------------------------------------------------------------------

def enforce_disk_limit():
    frames = sorted(FRAMES_DIR.glob("frame_*.jpg"), key=lambda p: p.stat().st_mtime)
    total_kb = sum(f.stat().st_size for f in frames) // 1024
    log.debug("cleanup: %d frames, %dKB total, limit %dKB", len(frames), total_kb, MAX_FRAMES_KB)
    if total_kb <= MAX_FRAMES_KB:
        log.debug("cleanup: within limit, nothing to do")
        return
    log.info("cleanup: frames dir at %dKB, exceeds %dKB limit", total_kb, MAX_FRAMES_KB)
    deleted = 0
    for f in frames:
        if total_kb <= MAX_FRAMES_KB:
            break
        size = f.stat().st_size // 1024
        f.unlink()
        total_kb -= size
        deleted += 1
        log.debug("cleanup: deleted %s (%dKB)", f.name, size)
    log.info("cleanup: deleted %d frames, now at %dKB", deleted, total_kb)


# ---------------------------------------------------------------------------
# Pixel-diff empty bassinet detection
# ---------------------------------------------------------------------------

def compute_diff_score(frame_path: Path, reference_path: Path) -> float:
    """Compute average pixel difference between center crops of two frames.
    
    Uses ffmpeg to crop center 60%, scale to 320x180, compute per-pixel
    difference in grayscale, and return the average value (0-255).
    Low score = frames are very similar (bassinet still empty).
    High score = something changed (baby placed).
    Returns -1 on error.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "fatal",
        "-i", str(frame_path),
        "-i", str(reference_path),
        "-filter_complex",
        "[0:v]crop=1152:648:384:216,scale=320:180[a];"
        "[1:v]crop=1152:648:384:216,scale=320:180[b];"
        "[a][b]blend=all_mode=difference,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=PIXEL_DIFF_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("pixel-diff: ffmpeg timed out after %ds", PIXEL_DIFF_TIMEOUT)
        return -1
    if result.returncode != 0 or not result.stdout:
        log.warning("pixel-diff: ffmpeg failed (exit=%d, stdout=%d bytes)",
                    result.returncode, len(result.stdout))
        return -1
    pixels = result.stdout
    return sum(pixels) / len(pixels)


def detect_empty_bassinet(frame_path: Path) -> tuple[bool, float]:
    """Check if bassinet is empty using pixel-diff against previous frame.
    
    Returns (is_empty, diff_score).
    Only returns is_empty=True if:
      1. Previous JSONL entry exists and was empty (babyPresent=false)
      2. Diff score between current and previous frame is below threshold
    On any error or uncertainty, returns (False, score) → API will be called.
    """
    prev = _get_last_entry()
    if not prev:
        log.debug("pixel-diff: no previous entry, skipping detection")
        return False, -1

    if prev.get("babyPresent", True):
        log.debug("pixel-diff: previous frame had baby, skipping detection")
        return False, -1

    prev_frame = prev.get("frame", "")
    if not prev_frame or not Path(prev_frame).exists():
        log.debug("pixel-diff: previous frame file missing, skipping detection")
        return False, -1

    score = compute_diff_score(frame_path, Path(prev_frame))
    if score < 0:
        log.debug("pixel-diff: computation failed, defaulting to API")
        return False, score

    is_empty = score < PIXEL_DIFF_THRESHOLD
    log.info("pixel-diff: score=%.2f threshold=%d → %s",
             score, PIXEL_DIFF_THRESHOLD, "EMPTY (skip API)" if is_empty else "CHANGED (call API)")
    return is_empty, score


EDGE_ALERT_COOLDOWN_MIN = 30


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


def _make_empty_entry(frame_path: Path, diff_score: float) -> dict:
    """Create a JSONL entry for an empty bassinet (API skipped)."""
    return {
        "babyPresent": False,
        "sleepPosition": "Unknown",
        "objectsInBassinet": "Unknown",
        "swaddle": "Unknown",
        "headCovering": "Unknown",
        "lighting": "Unknown",
        "state": "Unknown",
        "bodyPosture": "Unknown",
        "pacifierEngaged": "Unknown",
        "bassinetCondition": "Unknown",
        "hazards": "Unknown",
        "captureMode": "Unknown",
        "cameraTimestamp": None,
        "detectionMethod": "pixel-diff",
        "diffScore": round(diff_score, 2),
        "alerts": [],
    }


# ---------------------------------------------------------------------------
# Vision analysis via OpenAI API
# ---------------------------------------------------------------------------

def _build_ssl_context():
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        log.debug("ssl: using certifi ca bundle")
        return ctx
    except ImportError:
        log.debug("ssl: certifi not available, using system certs")
        return ssl.create_default_context()


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _call_anthropic(image_b64: str, prompt_text: str, api_key: str, model: str, timeout: int) -> dict:
    """Call Anthropic Messages API with vision."""
    payload = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt_text},
                ],
            }
        ],
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    ctx = _build_ssl_context()
    req = urllib.request.Request(ANTHROPIC_API_URL, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = json.loads(resp.read())

    raw = body["content"][0]["text"]
    cleaned = _strip_markdown_fences(raw)
    return json.loads(cleaned)


def _call_openai(image_b64: str, prompt_text: str, api_key: str,
                  model: str, timeout: int) -> tuple[dict, str]:
    """Call OpenAI vision API. Returns (analysis_dict, model_used)."""
    payload = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    ctx = _build_ssl_context()
    req = urllib.request.Request(OPENAI_API_URL, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = json.loads(resp.read())

    raw = body["choices"][0]["message"]["content"]
    cleaned = _strip_markdown_fences(raw)
    analysis = json.loads(cleaned)
    model_used = body.get("model", model)
    return analysis, model_used


def analyze_frame(image_path: Path, api_key: str, anthropic_key: str = None) -> dict:
    """Analyze a frame using the model fallback chain.
    
    Chain: gpt-4o-mini → gpt-4o → claude-sonnet-4-6
    Each model gets API_RETRIES attempts before falling to the next.
    """
    log.info("analyze: reading prompt from %s", PROMPT_FILE)
    prompt_text = PROMPT_FILE.read_text()
    lines = prompt_text.splitlines()
    if lines and lines[0].startswith("#"):
        prompt_text = "\n".join(lines[1:]).strip()

    image_bytes = image_path.read_bytes()
    image_b64 = base64.b64encode(image_bytes).decode()
    log.debug("analyze: image size=%dKB", len(image_bytes) // 1024)

    last_err = None

    for model_cfg in MODEL_CHAIN:
        provider = model_cfg["provider"]
        model = model_cfg["model"]
        timeout = model_cfg["timeout"]

        # Skip Anthropic if no key
        if provider == "anthropic" and not anthropic_key:
            log.debug("analyze: skipping %s/%s (no API key)", provider, model)
            continue

        for attempt in range(1, API_RETRIES + 1):
            t0 = time.monotonic()
            log.info("analyze: trying %s/%s attempt %d/%d (timeout=%ds)",
                     provider, model, attempt, API_RETRIES, timeout)
            try:
                if provider == "openai":
                    analysis, model_used = _call_openai(image_b64, prompt_text, api_key, model, timeout)
                else:
                    analysis = _call_anthropic(image_b64, prompt_text, anthropic_key, model, timeout)
                    model_used = model

                elapsed = time.monotonic() - t0
                n_attrs = len(analysis.get("attributes", []))
                attrs = {a["attribute"]: a["observedValue"] for a in analysis.get("attributes", [])}
                log.info("analyze: success (%.1fs, %s/%s, %d attrs) baby=%s position=%s state=%s",
                         elapsed, provider, model_used, n_attrs,
                         attrs.get("isBabyPresent", "?"),
                         attrs.get("Sleep Position", "?"),
                         attrs.get("Awake vs asleep", "?"))
                analysis["_model"] = f"{provider}/{model_used}"
                return analysis

            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                elapsed = time.monotonic() - t0
                if isinstance(e, urllib.error.HTTPError):
                    detail = e.read().decode(errors="replace")[:200]
                    last_err = f"{provider}/{model} HTTP {e.code}: {detail}"
                    log.warning("analyze: %s failed (HTTP %d, %.1fs): %s",
                                model, e.code, elapsed, detail[:100])
                    # Retry on rate limit or server error
                    if e.code == 429 or e.code >= 500:
                        if attempt < API_RETRIES:
                            wait = 2 ** attempt
                            log.info("analyze: retrying %s in %ds", model, wait)
                            time.sleep(wait)
                            continue
                else:
                    last_err = f"{provider}/{model}: {e}"
                    log.warning("analyze: %s failed (%.1fs): %s", model, elapsed, e)
                    if attempt < API_RETRIES:
                        time.sleep(2 ** attempt)
                        continue
                # Fall through to next model
                log.info("analyze: %s/%s exhausted, falling to next model", provider, model)
                break

            except (json.JSONDecodeError, KeyError) as e:
                elapsed = time.monotonic() - t0
                last_err = f"{provider}/{model}: {e}"
                log.warning("analyze: %s/%s invalid response (%.1fs): %s", provider, model, elapsed, e)
                # Bad response → try next model immediately (don't retry same model)
                break

    log.error("analyze: all models exhausted, last error: %s", last_err)
    raise RuntimeError(f"All models failed: {last_err}")


# ---------------------------------------------------------------------------
# Flatten vision API response into agent-friendly schema
# ---------------------------------------------------------------------------

# Map from vision schema attribute names to flat field names
_ATTR_MAP = {
    "isBabyPresent": "babyPresent",
    "Sleep Position": "sleepPosition",
    "Objects in bassinet": "objectsInBassinet",
    "Swaddle presence": "swaddle",
    "Head covering": "headCovering",
    "Lighting": "lighting",
    "Awake vs asleep": "state",
    "Body posture": "bodyPosture",
    "isPacifierEngaged": "pacifierEngaged",
    "Bassinet condition": "bassinetCondition",
    "External hazards (inside bassinet)": "hazards",
    "Baby location in bassinet": "bassinetLocation",
}

# Values that become booleans
_BOOL_FIELDS = {
    "babyPresent": {"Yes": True, "No": False},
    "pacifierEngaged": {"Yes": True, "No": False},
}


def flatten_analysis(analysis: dict, frame_path: str) -> dict:
    """Convert nested vision API response to flat, agent-friendly dict."""
    ctx = analysis.get("imageContext", {})
    flat = {
        "captureMode": ctx.get("captureMode", "Unknown"),
        "cameraTimestamp": ctx.get("inferredDateTime"),
    }

    for attr in analysis.get("attributes", []):
        src_name = attr.get("attribute", "")
        dst_name = _ATTR_MAP.get(src_name)
        if not dst_name:
            log.debug("flatten: skipping unknown attribute %s", src_name)
            continue
        value = attr.get("observedValue", "Unknown")
        if dst_name in _BOOL_FIELDS:
            value = _BOOL_FIELDS[dst_name].get(value, value)
        flat[dst_name] = value

    flat["detectionMethod"] = "vision-api"
    flat["modelUsed"] = analysis.get("_model", "unknown")
    log.debug("flatten: %d fields mapped from %d attributes", len(flat), len(analysis.get("attributes", [])))
    return flat



# ---------------------------------------------------------------------------
# Last entry helper (for heuristics)
# ---------------------------------------------------------------------------

def _get_last_entry() -> dict | None:
    """Read the last line from the JSONL log, or None if unavailable."""
    if not JSONL_FILE.exists():
        return None
    try:
        # Read last line efficiently
        with open(JSONL_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            # Read last 2KB (more than enough for one entry)
            f.seek(max(0, size - 2048))
            chunk = f.read().decode("utf-8", errors="replace")
            lines = chunk.strip().splitlines()
            if lines:
                return json.loads(lines[-1])
    except Exception as e:
        log.debug("_get_last_entry: failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Alert detection (works on flat entries)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Active wake detection
# ---------------------------------------------------------------------------

ALERT_STATE_FILE = DATA_DIR / "alert-state.json"
WAKE_SCORE_THRESHOLD = 4
WAKE_COOLDOWN_MIN = 30
WAKE_WINDOW = 6  # number of recent entries to analyze


def _get_recent_entries(n: int) -> list[dict]:
    """Read the last N entries from JSONL."""
    if not JSONL_FILE.exists():
        return []
    try:
        with open(JSONL_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            # Read enough for N entries (~500 bytes each)
            read_size = min(size, n * 600)
            f.seek(max(0, size - read_size))
            chunk = f.read().decode("utf-8", errors="replace")
            lines = chunk.strip().splitlines()
            entries = []
            for line in lines[-n:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return entries
    except Exception as e:
        log.debug("_get_recent_entries: failed: %s", e)
        return []


def _is_meaningful_position_change(pos_a: str, pos_b: str) -> bool:
    """Check if position change is meaningful (not Side<->Stomach noise)."""
    if pos_a == pos_b:
        return False
    # Side<->Stomach is vision model noise, ignore it
    noise_pair = {"Side", "Stomach"}
    if {pos_a, pos_b} == noise_pair:
        return False
    # Unknown doesn't count
    if pos_a == "Unknown" or pos_b == "Unknown":
        return False
    return True


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
    recent = _get_recent_entries(WAKE_WINDOW)
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


BURST_INTERVAL_SEC = 60  # seconds between burst captures
BURST_CONFIRM_COUNT = 2  # number of confirmation frames
BURST_AWAKE_THRESHOLD = 2  # minimum Awake readings (out of 3 total) to confirm wake


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


def _save_alert_state(alert_type: str):
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


ALERT_FEEDBACK_FILE = DATA_DIR / "alert-feedback.jsonl"


def _log_alert_feedback(alert_id: str, wake_alert: dict):
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


def _reset_wake_cooldown():
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


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Safety alert rules (legacy, currently disabled)
# ---------------------------------------------------------------------------

ALERT_RULES = {
    # "sleepPosition": {"Stomach"},  # Disabled — vision model misclassifies positions
    # "objectsInBassinet": {"Blanket", "Pillow", "Mixed"},  # Disabled by user
    # "hazards": {"Loose items", "Cords nearby"},  # Disabled by user
}

# Human-readable names for alert messages
_ALERT_LABELS = {
    "sleepPosition": "Sleep Position",
    "objectsInBassinet": "Objects in bassinet",
    "hazards": "Hazards",
}

def check_alerts(flat: dict) -> list[str]:
    alerts = []
    checked = 0
    for field, trigger_values in ALERT_RULES.items():
        value = flat.get(field, "Unknown")
        checked += 1
        if value in trigger_values:
            label = _ALERT_LABELS.get(field, field)
            log.debug("alert: %s=%s (rule matched)", field, value)
            alerts.append(f"{label}: {value}")
    log.debug("alert check: %d rules evaluated, %d alerts triggered", checked, len(alerts))
    return alerts


# ---------------------------------------------------------------------------
# Debug: --last N
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Debug: --status
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Backtest mode
# ---------------------------------------------------------------------------

def cmd_backtest(last_n: int = None, from_date: str = None, quick: bool = False,
                  alerts: bool = False):
    """Replay historical JSONL entries to test current detection logic.
    
    --quick: Only tests the pixel-diff gate (no API calls).
    --alerts: Test active wake detection and show when alerts would fire.
    """
    if not JSONL_FILE.exists():
        print("No JSONL log file found.", file=sys.stderr)
        return 1

    lines = JSONL_FILE.read_text().strip().splitlines()
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

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
        print(f"ACTIVE WAKE ALERT BACKTEST (burst confirmation, {BURST_AWAKE_THRESHOLD}/{BURST_CONFIRM_COUNT+1} Awake required)")
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


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------

def _output(status: str, **kwargs):
    print(json.dumps({"status": status, **kwargs}))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    p.add_argument("--dry-run", action="store_true",
                   help="run full pipeline but do not write to the JSONL log")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print all log messages to stderr (not just warnings)")
    p.add_argument("--quick", action="store_true",
                   help="(backtest) skip API calls, only test pixel-diff gate")
    p.add_argument("--alerts", action="store_true",
                   help="(backtest) test active wake alert detection")
    p.add_argument("--from-date", metavar="DATE",
                   help="(backtest) only test entries from this date (YYYY-MM-DD)")
    p.add_argument("--count", metavar="N", type=int,
                   help="(backtest) only test last N entries")
    return p.parse_args()


def main():
    args = parse_args()

    if args.verbose:
        _sh.setLevel(logging.DEBUG)

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
        return cmd_backtest(
            last_n=args.count,
            from_date=args.from_date,
            quick=args.quick,
            alerts=args.alerts,
        )

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

    # --- Pixel-diff empty detection ---
    is_empty, diff_score = detect_empty_bassinet(frame_path)

    if is_empty:
        flat = _make_empty_entry(frame_path, diff_score)
        alerts = []
        log.info("pipeline: bassinet empty (pixel-diff score=%.2f), API skipped", diff_score)
    else:
        # --- Full OpenAI vision analysis ---
        log.info("pipeline: sending frame to vision API")
        try:
            analysis = analyze_frame(frame_path, api_key, anthropic_key)
        except RuntimeError as e:
            log.error("pipeline: analysis failed, aborting - %s", e)
            _output("error", error=str(e), frame=str(frame_path))
            return 1

        # Flatten + check alerts
        flat = flatten_analysis(analysis, str(frame_path))
        flat["diffScore"] = round(diff_score, 2) if diff_score >= 0 else None

        # Heuristic: if state is "Unknown", baby is present, and position matches
        # the previous frame, infer "Asleep" (baby hasn't moved, likely sleeping)
        if flat.get("state") == "Unknown" and flat.get("babyPresent"):
            prev = _get_last_entry()
            if prev and prev.get("babyPresent") and prev.get("sleepPosition") == flat.get("sleepPosition"):
                if flat.get("sleepPosition") != "Unknown":
                    log.info("heuristic: state Unknown -> Asleep (position unchanged: %s)", flat.get("sleepPosition"))
                    flat["state"] = "Asleep"
                    flat["stateInferred"] = True

        alerts = check_alerts(flat)

    # Build flat log entry
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {"timestamp": now, "frame": str(frame_path), **flat, "alerts": alerts}

    # Log to JSONL
    if args.dry_run:
        log.info("pipeline: dry-run, skipping JSONL write")
    else:
        entry_json = json.dumps(entry)
        log.debug("pipeline: JSONL entry size=%d bytes", len(entry_json))
        with open(JSONL_FILE, "a") as f:
            f.write(entry_json + "\n")
        log.info("pipeline: logged entry to %s at %s", JSONL_FILE.name, now)
    elapsed = time.monotonic() - t_start

    # --- Safety alerts ---
    if not args.dry_run:
        check_edge_alert(entry, env)

    # --- Active wake detection (burst confirmation) ---
    if not args.dry_run:
        if entry.get("babyPresent"):
            if should_burst(entry):
                wake_alert = run_burst_confirmation(rtsp_url, api_key, anthropic_key, entry)
                if wake_alert:
                    log.info("pipeline: ACTIVE WAKE confirmed (%d/%d Awake)",
                             wake_alert["awake_count"], wake_alert["total_frames"])
                    ts_str = wake_alert["timestamp"]
                    try:
                        ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        # Convert UTC to ET
                        import zoneinfo
                        et_tz = zoneinfo.ZoneInfo("America/New_York")
                        local_time = ts_dt.astimezone(et_tz).strftime("%I:%M %p")
                    except Exception:
                        local_time = ts_str
                    # Generate alert ID for feedback tracking
                    alert_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                    wake_msg = (
                        f"🍼 Baby waking up!\n"
                        f"Confirmed: {wake_alert['awake_count']}/{wake_alert['total_frames']} "
                        f"frames show Awake (burst check over ~2 min)\n"
                        f"First detected at {local_time}\n\n"
                        f"Was this correct?"
                    )
                    send_telegram_alert(wake_msg, env, alert_id=alert_id)
                    _save_alert_state("active_wake")
                    _log_alert_feedback(alert_id, wake_alert)
        else:
            # Baby removed — reset cooldown so next wake can trigger
            _reset_wake_cooldown()

    if alerts:
        summary = "ALERT: " + "; ".join(alerts)
        log.warning("pipeline: %d alerts detected in %.1fs: %s", len(alerts), elapsed, summary)
        _output("alert", frame=str(frame_path), alerts=alerts, summary=summary)
    else:
        log.info("pipeline: completed in %.1fs, no alerts", elapsed)
        _output("ok", frame=str(frame_path), alerts=[], summary="Check completed, no safety alerts.")

    log.info("--- run end: %.1fs ---", elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
