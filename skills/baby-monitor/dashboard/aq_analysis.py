"""Air-quality analysis helpers for the BILBO dashboard's Air Quality tab.

Pure functions — no Flask, no I/O — so the route handler stays a thin shim
around DB access plus calls into here. Thresholds are rooted in indoor-air
guidance (ASHRAE, EPA, WELL) tightened for a nursery context (lower CO2
ceiling, narrower humidity band).

All functions accept dict-shaped rows in the same shape returned by the
/api/air-quality SQL projection: {t, co2, pm25, temp, rh, tvoc_index}.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


# Status levels.
GOOD, MODERATE, POOR, CRITICAL = "good", "moderate", "poor", "critical"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Per-metric status + baby-focused interpretation
# ---------------------------------------------------------------------------
# Each function returns (level, headline, detail). `level` is one of the
# constants above or None when no data is available.

def status_co2(ppm):
    if ppm is None:
        return None, "No reading", ""
    if ppm < 800:
        return GOOD, "Excellent", "Fresh, well-ventilated air."
    if ppm < 1000:
        return MODERATE, "Acceptable", "Slightly stale; consider a window crack soon."
    if ppm < 1500:
        return POOR, "Poor ventilation", "May affect sleep depth — open a window."
    return CRITICAL, "Stuffy", "Strongly affects sleep & alertness — ventilate now."


def status_pm25(ug):
    if ug is None:
        return None, "No reading", ""
    if ug < 12:
        return GOOD, "Clean", "Low particulate — safe for baby."
    if ug < 25:
        return MODERATE, "Slightly elevated", "Cooking or dust nearby? Watch the trend."
    if ug < 35:
        return POOR, "Unhealthy for sensitive groups", "Air purifier recommended."
    return CRITICAL, "Unhealthy", "Run HEPA purifier; reduce sources."


def status_temp_c(c):
    if c is None:
        return None, "No reading", ""
    if 20 <= c <= 23:
        return GOOD, "Comfortable", "Within the safe-sleep nursery range."
    if 19 <= c < 20 or 23 < c <= 24:
        return MODERATE, "Edge of range", "Slightly outside ideal — adjust layers."
    return POOR, "Out of range", "Aim for 20–23 °C (68–73 °F) for sleep."


def status_humidity(rh):
    if rh is None:
        return None, "No reading", ""
    if 40 <= rh <= 60:
        return GOOD, "Comfortable", "Mucous membranes happy."
    if 30 <= rh < 40 or 60 < rh <= 65:
        return MODERATE, "Drifting", "Within tolerance but trending dry/humid."
    if rh < 30:
        return POOR, "Too dry", "Risk of dryness, congestion. Run humidifier."
    return POOR, "Too humid", "Risk of mold; open window or run dehumidifier."


def status_tvoc(idx):
    if idx is None:
        return None, "No reading", ""
    if idx < 150:
        return GOOD, "Clean", "Normal VOC baseline."
    if idx < 250:
        return MODERATE, "Elevated", "Recent product use? Watch the spike."
    if idx < 400:
        return POOR, "Strong source", "VOC source nearby — ventilate."
    return CRITICAL, "Very high", "Identify and remove the VOC source."


def latest_with_status(latest):
    """Decorate a `latest` dict with per-metric status blocks for the hero
    cards. Returns a dict keyed by metric with {value, level, headline,
    detail, unit}; missing metrics map to None."""
    if not latest:
        return None

    def pack(level, headline, detail, value, unit):
        return None if level is None else {
            "value": value, "level": level, "headline": headline,
            "detail": detail, "unit": unit,
        }

    co2_l, co2_h, co2_d = status_co2(latest.get("co2"))
    pm_l, pm_h, pm_d = status_pm25(latest.get("pm25"))
    t_l, t_h, t_d = status_temp_c(latest.get("temp"))
    rh_l, rh_h, rh_d = status_humidity(latest.get("rh"))
    tv_l, tv_h, tv_d = status_tvoc(latest.get("tvoc_index"))
    return {
        "co2":      pack(co2_l, co2_h, co2_d, latest.get("co2"), "ppm"),
        "pm25":     pack(pm_l, pm_h, pm_d, latest.get("pm25"), "µg/m³"),
        "temp":     pack(t_l,  t_h,  t_d,  latest.get("temp"), "°C"),
        "humidity": pack(rh_l, rh_h, rh_d, latest.get("rh"), "%"),
        "tvoc":     pack(tv_l, tv_h, tv_d, latest.get("tvoc_index"), "idx"),
    }


# ---------------------------------------------------------------------------
# Comfort score — 0–100 weighted composite
# ---------------------------------------------------------------------------

WEIGHTS = {"pm25": 0.30, "co2": 0.25, "temp": 0.20, "humidity": 0.15, "tvoc": 0.10}


def _sub_co2(ppm):
    if ppm is None: return None
    if ppm <= 800: return 100.0
    if ppm >= 2500: return 0.0
    return _clamp(100 * (1 - (ppm - 800) / (2500 - 800)), 0, 100)


def _sub_pm25(ug):
    if ug is None: return None
    if ug <= 12: return 100.0
    if ug >= 50: return 0.0
    return _clamp(100 * (1 - (ug - 12) / (50 - 12)), 0, 100)


def _sub_temp(c):
    if c is None: return None
    if 21 <= c <= 23: return 100.0
    delta = (21 - c) if c < 21 else (c - 23)
    return _clamp(100 - delta * 20, 0, 100)  # -20pts/°C outside


def _sub_humidity(rh):
    if rh is None: return None
    if 45 <= rh <= 55: return 100.0
    delta = (45 - rh) if rh < 45 else (rh - 55)
    return _clamp(100 - delta * 4, 0, 100)


def _sub_tvoc(idx):
    if idx is None: return None
    if idx <= 100: return 100.0
    if idx >= 500: return 0.0
    return _clamp(100 * (1 - (idx - 100) / (500 - 100)), 0, 100)


_METRIC_LABEL = {
    "pm25": "PM2.5", "co2": "CO₂", "temp": "Temperature",
    "humidity": "Humidity", "tvoc": "TVOC",
}


def comfort_score(latest):
    """Returns {score, label, drivers}. Drivers are sorted worst-first so the
    UI can highlight what's pulling the score down. Missing metrics are
    proportionally reweighted across the present ones."""
    if not latest:
        return None
    subs = {
        "pm25":     _sub_pm25(latest.get("pm25")),
        "co2":      _sub_co2(latest.get("co2")),
        "temp":     _sub_temp(latest.get("temp")),
        "humidity": _sub_humidity(latest.get("rh")),
        "tvoc":     _sub_tvoc(latest.get("tvoc_index")),
    }
    present = {k: v for k, v in subs.items() if v is not None}
    if not present:
        return None
    total_w = sum(WEIGHTS[k] for k in present)
    score = sum(present[k] * WEIGHTS[k] for k in present) / total_w
    score = round(score, 1)
    label = (
        "Excellent" if score >= 90 else
        "Good" if score >= 75 else
        "Needs attention" if score >= 55 else
        "Poor"
    )
    drivers = sorted(
        [{
            "metric": k,
            "label": _METRIC_LABEL[k],
            "sub": round(present[k], 1),
            "weight": round(WEIGHTS[k] * 100),
        } for k in present],
        key=lambda d: d["sub"],
    )
    return {"score": score, "label": label, "drivers": drivers}


# ---------------------------------------------------------------------------
# Alerts — current-state breaches + recent anomalies
# ---------------------------------------------------------------------------

ACTION = {
    "co2_high":        "Open a window or door for 10–15 minutes.",
    "co2_critical":    "Ventilate immediately. Reduce occupancy if possible.",
    "pm25_mod":        "Check for cooking smoke or dust; avoid generating more.",
    "pm25_high":       "Run HEPA air purifier. Close window if outside air is poor.",
    "temp_low":        "Add a sleep layer or close drafts.",
    "temp_high":       "Lower the thermostat or open a window.",
    "humidity_low":    "Run a humidifier.",
    "humidity_high":   "Run a dehumidifier or improve airflow.",
    "tvoc_high":       "Identify the VOC source (cleaner, paint, plug-in). Ventilate.",
}


def _alert(severity, metric, value, unit, t, action_key):
    return {
        "severity": severity,
        "metric": metric,
        "value": value,
        "unit": unit,
        "t": t,
        "action": ACTION[action_key],
    }


def _detect_pm25_spike(rows):
    if len(rows) < 90:
        return None
    recent = [r["pm25"] for r in rows[-15:] if r.get("pm25") is not None]
    baseline = [r["pm25"] for r in rows[-360:-15] if r.get("pm25") is not None]
    if len(recent) < 5 or len(baseline) < 30:
        return None
    cur = sum(recent) / len(recent)
    base = sum(baseline) / len(baseline)
    if base < 0.5 or cur < 5:
        return None
    if cur >= base * 3:
        return {
            "severity": "info", "metric": "PM2.5",
            "value": round(cur, 1), "unit": "µg/m³",
            "t": rows[-1]["t"],
            "action": f"Recent PM2.5 is {cur/base:.1f}× the 6 h baseline — likely cooking, candles, or vacuuming.",
        }
    return None


def compute_alerts(rows, latest):
    alerts = []
    if latest:
        co2 = latest.get("co2")
        if co2 is not None:
            if co2 > 1500:
                alerts.append(_alert("critical", "CO₂", co2, "ppm", latest["t"], "co2_critical"))
            elif co2 > 1000:
                alerts.append(_alert("warning", "CO₂", co2, "ppm", latest["t"], "co2_high"))
        pm = latest.get("pm25")
        if pm is not None:
            if pm > 25:
                alerts.append(_alert("warning", "PM2.5", round(pm, 1), "µg/m³", latest["t"], "pm25_high"))
            elif pm > 12:
                alerts.append(_alert("info", "PM2.5", round(pm, 1), "µg/m³", latest["t"], "pm25_mod"))
        c = latest.get("temp")
        if c is not None:
            if c < 19:
                alerts.append(_alert("warning", "Temperature", round(c, 1), "°C", latest["t"], "temp_low"))
            elif c > 24:
                alerts.append(_alert("warning", "Temperature", round(c, 1), "°C", latest["t"], "temp_high"))
        rh = latest.get("rh")
        if rh is not None:
            if rh < 30:
                alerts.append(_alert("warning", "Humidity", round(rh, 1), "%", latest["t"], "humidity_low"))
            elif rh > 65:
                alerts.append(_alert("warning", "Humidity", round(rh, 1), "%", latest["t"], "humidity_high"))
        tv = latest.get("tvoc_index")
        if tv is not None and tv > 250:
            alerts.append(_alert("warning", "TVOC", int(tv), "idx", latest["t"], "tvoc_high"))
    if rows:
        spike = _detect_pm25_spike(rows)
        if spike:
            alerts.append(spike)
    # Critical first, then warning, then info.
    sev_rank = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: sev_rank.get(a["severity"], 9))
    return alerts


# ---------------------------------------------------------------------------
# Insights — pattern-based, written like a smart-assistant comment
# ---------------------------------------------------------------------------

_ET = timezone(timedelta(hours=-4))


def _et_hour(iso_z):
    ts = datetime.fromisoformat(iso_z.replace("Z", "+00:00"))
    return ts.astimezone(_ET).hour


def _overnight_co2(rows):
    night = [r["co2"] for r in rows
             if r.get("co2") is not None and 0 <= _et_hour(r["t"]) < 6]
    if len(night) < 30:
        return None
    avg = sum(night) / len(night)
    over = sum(1 for v in night if v > 1000)
    if avg > 900 or over > len(night) * 0.3:
        return {
            "id": "overnight_co2",
            "title": "CO₂ rises overnight",
            "body": (
                f"Overnight (12 AM–6 AM) average CO₂ was {avg:.0f} ppm, with "
                f"{over} of {len(night)} samples above 1000 ppm. Consider "
                "cracking a window during sleep — better ventilation often "
                "improves sleep depth."
            ),
        }
    return None


def _evening_pm25(rows):
    eve, base = [], []
    for r in rows:
        if r.get("pm25") is None:
            continue
        h = _et_hour(r["t"])
        (eve if 17 <= h <= 22 else base).append(r["pm25"])
    if len(eve) < 15 or len(base) < 30:
        return None
    eve_avg = sum(eve) / len(eve)
    base_avg = sum(base) / len(base)
    if base_avg < 0.5 or eve_avg < 5:
        return None
    if eve_avg > base_avg * 2:
        return {
            "id": "evening_pm25",
            "title": "PM2.5 spikes in the evening",
            "body": (
                f"Evening (5–10 PM) PM2.5 averaged {eve_avg:.1f} µg/m³ vs "
                f"{base_avg:.1f} µg/m³ otherwise. Likely cooking — running the "
                "kitchen exhaust fan or closing the nursery door at dinner can help."
            ),
        }
    return None


def _humidity_drift(rows):
    rh = [r["rh"] for r in rows if r.get("rh") is not None]
    if len(rh) < 30:
        return None
    low = sum(1 for v in rh if v < 35)
    high = sum(1 for v in rh if v > 65)
    if low > len(rh) * 0.5:
        return {
            "id": "rh_low",
            "title": "Air is dry",
            "body": (
                f"{low} of {len(rh)} readings were below 35 % humidity. Dry "
                "air can irritate baby's airways and dry out skin. A cool-mist "
                "humidifier in the nursery usually fixes this."
            ),
        }
    if high > len(rh) * 0.5:
        return {
            "id": "rh_high",
            "title": "Air is humid",
            "body": (
                f"{high} of {len(rh)} readings were above 65 % humidity. "
                "Watch for condensation or mold; improve airflow."
            ),
        }
    return None


def _overnight_summary(rows):
    """If the rendered window covers a night, summarize how it went."""
    night_co2 = [r for r in rows
                 if r.get("co2") is not None and 1 <= _et_hour(r["t"]) <= 4]
    if len(night_co2) < 30:
        return None
    bad = [r for r in night_co2 if r["co2"] > 1000]
    if not bad:
        return None
    pct = 100 * len(bad) / len(night_co2)
    if pct < 25:
        return None
    return {
        "id": "overnight_summary",
        "title": "Overnight summary",
        "body": (
            f"Air quality was poor for ~{pct:.0f}% of the 1–4 AM window — "
            "primarily high CO₂. The room may be sealed too tightly during sleep."
        ),
    }


def compute_insights(rows, latest):
    out = []
    if not rows:
        return out
    for fn in (_overnight_co2, _evening_pm25, _humidity_drift, _overnight_summary):
        try:
            r = fn(rows)
        except Exception:
            r = None
        if r:
            out.append(r)
    if not out and latest:
        score = comfort_score(latest)
        if score and score["score"] >= 85:
            out.append({
                "id": "all_good",
                "title": "All clear",
                "body": "Air quality is in the comfortable range across all metrics. Nothing to do.",
            })
    return out


# ---------------------------------------------------------------------------
# Recommendations — right-now, action-oriented
# ---------------------------------------------------------------------------

def compute_recommendations(latest):
    if not latest:
        return []
    rec = []
    co2 = latest.get("co2")
    pm = latest.get("pm25")
    c = latest.get("temp")
    rh = latest.get("rh")
    tv = latest.get("tvoc_index")
    if co2 is not None and co2 > 1000:
        rec.append({
            "id": "ventilate", "icon": "🪟",
            "title": "Open a window for 10–15 minutes",
            "body": f"CO₂ is {co2:.0f} ppm. A short cross-ventilation usually drops it 200–400 ppm.",
        })
    if pm is not None and pm > 15:
        rec.append({
            "id": "purifier", "icon": "🌬️",
            "title": "Run the HEPA air purifier",
            "body": f"PM2.5 is {pm:.1f} µg/m³. A purifier on medium clears most of this in 15–30 min.",
        })
    if rh is not None and rh < 35:
        rec.append({
            "id": "humidify", "icon": "💧",
            "title": "Run the humidifier",
            "body": f"Humidity is {rh:.0f} %. Aim for 45–55 %.",
        })
    if rh is not None and rh > 65:
        rec.append({
            "id": "dehumidify", "icon": "💨",
            "title": "Improve airflow",
            "body": f"Humidity is {rh:.0f} %. Open the door or run a fan to prevent mold.",
        })
    if c is not None and c < 19:
        f = c * 9 / 5 + 32
        rec.append({
            "id": "warm_up", "icon": "🌡️",
            "title": "Add a layer or close drafts",
            "body": f"Temperature is {c:.1f} °C ({f:.1f} °F). Aim for 20–23 °C.",
        })
    if c is not None and c > 24:
        f = c * 9 / 5 + 32
        rec.append({
            "id": "cool_down", "icon": "❄️",
            "title": "Cool the room",
            "body": f"Temperature is {c:.1f} °C ({f:.1f} °F). Lower the thermostat or open a window.",
        })
    if tv is not None and tv > 200:
        rec.append({
            "id": "ventilate_voc", "icon": "🌿",
            "title": "Ventilate to clear VOCs",
            "body": "Recent product use (cleaner, plug-in, paint)? Open a window until the index drops below 150.",
        })
    if not rec:
        rec.append({
            "id": "no_action", "icon": "✓",
            "title": "Nothing to do right now",
            "body": "All metrics are within the comfortable range. Keep an eye on the trend overnight.",
        })
    return rec


# ---------------------------------------------------------------------------
# Bad-zone shading — contiguous spans where a metric is in the 'poor' band
# ---------------------------------------------------------------------------

def compute_bad_zones(rows):
    """For each metric, find contiguous spans where the value is in the
    'poor' band, returned as {metric: [{tStart, tEnd}]}. Used to draw a
    translucent backdrop on the corresponding chart."""
    metrics = {
        "co2":        (lambda v: v is not None and v > 1000),
        "pm25":       (lambda v: v is not None and v > 25),
        "temp":       (lambda v: v is not None and (v < 19 or v > 24)),
        "rh":         (lambda v: v is not None and (v < 30 or v > 65)),
        "tvoc_index": (lambda v: v is not None and v > 250),
    }
    out = {k: [] for k in metrics}
    for k, predicate in metrics.items():
        run_start = None
        last_t = None
        for r in rows:
            if predicate(r.get(k)):
                if run_start is None:
                    run_start = r["t"]
                last_t = r["t"]
            else:
                if run_start is not None and last_t is not None:
                    out[k].append({"tStart": run_start, "tEnd": last_t})
                    run_start = None
                    last_t = None
        if run_start is not None and last_t is not None:
            out[k].append({"tStart": run_start, "tEnd": last_t})
    return out


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------

def compute_health(rows, latest, expected_interval_seconds: int = 60):
    now = datetime.now(timezone.utc)
    seconds_since = None
    if latest:
        last_t = datetime.fromisoformat(latest["t"].replace("Z", "+00:00"))
        seconds_since = int((now - last_t).total_seconds())

    expected = None
    missing_pct = None
    if rows and len(rows) >= 2:
        first_t = datetime.fromisoformat(rows[0]["t"].replace("Z", "+00:00"))
        last_t = datetime.fromisoformat(rows[-1]["t"].replace("Z", "+00:00"))
        span_seconds = (last_t - first_t).total_seconds()
        expected = max(1, math.floor(span_seconds / expected_interval_seconds) + 1)
        received = len(rows)
        missing_pct = max(0.0, min(100.0, 100 * (1 - received / expected)))

    # Health verdict.
    verdict = "ok"
    if seconds_since is None:
        verdict = "no_data"
    elif seconds_since > 600:
        verdict = "stale"
    elif missing_pct is not None and missing_pct > 5:
        verdict = "gappy"

    return {
        "lastReading": latest["t"] if latest else None,
        "secondsSinceLast": seconds_since,
        "samples": len(rows),
        "expected": expected,
        "missingPct": round(missing_pct, 1) if missing_pct is not None else None,
        "verdict": verdict,
    }
