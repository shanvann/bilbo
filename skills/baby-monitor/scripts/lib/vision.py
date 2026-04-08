"""Vision API calls (OpenAI + Anthropic) with fallback chain, and response flattening."""

import base64
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import (
    ANTHROPIC_API_URL,
    API_RETRIES,
    MODEL_CHAIN,
    OPENAI_API_URL,
    PROMPT_FILE,
)

log = logging.getLogger("monitor")


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
    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai library not installed. Please run: pip install -r requirements.txt")
        raise RuntimeError("OpenAI library not found")

    client = OpenAI(api_key=api_key, timeout=timeout)

    response = client.chat.completions.create(
        model=model,
        messages=[
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
        max_tokens=1024,
    )

    raw = response.choices[0].message.content
    cleaned = _strip_markdown_fences(raw)
    analysis = json.loads(cleaned)
    model_used = response.model
    return analysis, model_used


def analyze_frame(image_path: Path, api_key: str, anthropic_key: str = None) -> dict:
    """Analyze a frame using the model fallback chain.

    Chain: gpt-4o-mini -> gpt-4o -> claude-sonnet-4-6
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
                    raw_body = e.read().decode(errors="replace")[:500]
                    last_err = f"{provider}/{model} HTTP {e.code}: {raw_body}"
                    log.warning("analyze: %s failed (HTTP %d, %.1fs). RAW: %s",
                                model, e.code, elapsed, raw_body)
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
                # Bad response -> try next model immediately (don't retry same model)
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
    "Head position in frame": "headPosition",
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
