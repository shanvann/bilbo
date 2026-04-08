"""Monitor performance section — BIRDEYE vs cloud API metrics.

Analyzes sleep-log.jsonl entries to report on detection method usage,
birdeye accuracy indicators, API cost savings, and operational health.

All functions are importable for use by the dashboard or other tools.
"""

from collections import Counter
from datetime import datetime

from .config import SLEEP_JSONL


# ---------------------------------------------------------------------------
# Data analysis (pure functions — importable by dashboard)
# ---------------------------------------------------------------------------

def analyze_monitor_entries(entries: list[dict]) -> dict:
    """Analyze a list of sleep-log entries and return structured metrics.

    Args:
        entries: list of dicts from sleep-log.jsonl, each with '_ts' datetime.

    Returns dict with keys:
        total, methods, states, birdeye, cloud_api, pixel_diff, gaps, alerts,
        transitions, cost_estimate
    """
    total = len(entries)
    if total == 0:
        return {"total": 0}

    # --- Detection method breakdown ---
    methods = Counter(e.get("detectionMethod", "unknown") for e in entries)

    birdeye = [e for e in entries if e.get("detectionMethod") == "birdeye"]
    cloud = [e for e in entries if e.get("detectionMethod") in ("vision-api", "openai-vision")]
    pixel_diff = [e for e in entries if e.get("detectionMethod") == "pixel-diff"]

    # --- State distribution ---
    states = Counter()
    for e in entries:
        if not e.get("babyPresent", False):
            states["not_present"] += 1
        else:
            states[e.get("state", "Unknown")] += 1

    # --- Birdeye metrics ---
    birdeye_states = Counter()
    presence_confs = []
    eye_confs = []
    timings = []

    for e in birdeye:
        if not e.get("babyPresent", False):
            birdeye_states["not_present"] += 1
        else:
            birdeye_states[e.get("state", "Unknown")] += 1
        if e.get("presenceConfidence") is not None:
            presence_confs.append(e["presenceConfidence"])
        if e.get("eyeConfidence") is not None:
            eye_confs.append(e["eyeConfidence"])
        t = e.get("birdeyeTimings", {})
        if "total" in t:
            timings.append(t["total"])

    # --- Cloud API metrics ---
    cloud_models = Counter(e.get("modelUsed", "unknown") for e in cloud)
    cloud_states = Counter()
    for e in cloud:
        if not e.get("babyPresent", False):
            cloud_states["not_present"] += 1
        else:
            cloud_states[e.get("state", "Unknown")] += 1

    # --- Gaps > 10 min ---
    gaps = []
    for i in range(1, len(entries)):
        gap_sec = (entries[i]["_ts"] - entries[i - 1]["_ts"]).total_seconds()
        if gap_sec > 600:
            gaps.append({
                "start": entries[i - 1]["_ts"],
                "end": entries[i]["_ts"],
                "minutes": gap_sec / 60,
            })

    # --- Alerts ---
    alert_entries = [e for e in entries if e.get("alerts")]
    alert_types = Counter()
    for e in alert_entries:
        for a in e.get("alerts", []):
            alert_types[a] += 1

    # --- State transitions (birdeye only, baby present) ---
    birdeye_present = [e for e in birdeye if e.get("babyPresent")]
    transitions = Counter()
    for i in range(1, len(birdeye_present)):
        prev = birdeye_present[i - 1].get("state", "Unknown")
        curr = birdeye_present[i].get("state", "Unknown")
        if prev != curr:
            transitions[(prev, curr)] += 1

    # --- Cost estimate ---
    api_saved = len(birdeye) + len(pixel_diff)
    est_cost = len(cloud) * 0.01
    est_saved = api_saved * 0.01

    return {
        "total": total,
        "methods": dict(methods),
        "states": dict(states),
        "birdeye": {
            "count": len(birdeye),
            "rate": round(len(birdeye) / total, 3) if total else 0,
            "states": dict(birdeye_states),
            "confidence": {
                "presence": _stats(presence_confs),
                "eye": _stats(eye_confs),
            },
            "timing": _stats(timings),
        },
        "cloud_api": {
            "count": len(cloud),
            "models": dict(cloud_models),
            "states": dict(cloud_states),
        },
        "pixel_diff": {"count": len(pixel_diff)},
        "gaps": gaps,
        "alerts": {"count": len(alert_entries), "types": dict(alert_types)},
        "transitions": {f"{a}->{b}": c for (a, b), c in transitions.most_common()},
        "cost": {"api_calls": len(cloud), "est_cost": round(est_cost, 2),
                 "api_avoided": api_saved, "est_saved": round(est_saved, 2)},
    }


def _stats(values: list[float]) -> dict | None:
    """Compute avg/min/max/p50/p95 for a list of floats."""
    if not values:
        return None
    s = sorted(values)
    return {
        "avg": round(sum(s) / len(s), 4),
        "min": round(s[0], 4),
        "max": round(s[-1], 4),
        "p50": round(s[len(s) // 2], 4),
        "p95": round(s[int(len(s) * 0.95)], 4),
        "count": len(s),
    }


# ---------------------------------------------------------------------------
# Text section (for report.py text output)
# ---------------------------------------------------------------------------

def monitor_section(entries: list[dict], num_days: float, start, end) -> str:
    """Generate the monitor performance text section."""
    m = analyze_monitor_entries(entries)

    if m["total"] == 0:
        return "**🔍 Monitor Performance**\nNo entries in this period."

    total = m["total"]
    span_hours = (end - start).total_seconds() / 3600
    expected = int(span_hours * 60 / 4)

    lines = ["**🔍 Monitor Performance**"]
    lines.append(f"- Entries: {total} (expected ~{expected}, coverage {_pct(total, expected)})")

    # Method breakdown
    b = m["birdeye"]
    c = m["cloud_api"]
    p = m["pixel_diff"]
    lines.append(f"- Detection: birdeye {b['count']} ({_pct(b['count'], total)}), "
                 f"cloud API {c['count']} ({_pct(c['count'], total)}), "
                 f"pixel-diff {p['count']} ({_pct(p['count'], total)})")

    # Cost
    cost = m["cost"]
    lines.append(f"- API calls: {cost['api_calls']} (est. ${cost['est_cost']:.2f}), "
                 f"avoided: {cost['api_avoided']} (est. ${cost['est_saved']:.2f} saved)")

    # Birdeye detail
    if b["count"] > 0:
        lines.append(f"- Birdeye states: " + ", ".join(
            f"{k} {v}" for k, v in sorted(b["states"].items(), key=lambda x: -x[1])))

        if b["confidence"]["presence"]:
            pc = b["confidence"]["presence"]
            lines.append(f"- Presence confidence: avg {pc['avg']:.3f}, min {pc['min']:.3f}")

        if b["confidence"]["eye"]:
            ec = b["confidence"]["eye"]
            lines.append(f"- Eye state confidence: avg {ec['avg']:.3f}, min {ec['min']:.3f}")

        if b["timing"]:
            t = b["timing"]
            lines.append(f"- Inference: avg {t['avg']:.3f}s, p50 {t['p50']:.3f}s, p95 {t['p95']:.3f}s")

    # Cloud API models
    if c["count"] > 0:
        models = ", ".join(f"{k} ({v})" for k, v in c["models"].items())
        lines.append(f"- Cloud models: {models}")

    # Gaps
    gaps = m["gaps"]
    if gaps:
        lines.append(f"- Gaps >10min: {len(gaps)}")
        for g in gaps[:3]:
            lines.append(f"  - {g['start'].strftime('%m/%d %H:%M')} → "
                         f"{g['end'].strftime('%H:%M')} ({g['minutes']:.0f}min)")
        if len(gaps) > 3:
            lines.append(f"  - ...and {len(gaps) - 3} more")

    # Transitions
    if m["transitions"]:
        lines.append(f"- State transitions: " + ", ".join(
            f"{k} ({v})" for k, v in m["transitions"].items()))

    # Alerts
    if m["alerts"]["count"] > 0:
        lines.append(f"- Alerts: {m['alerts']['count']}")

    return "\n".join(lines)


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{n * 100 / total:.0f}%"
