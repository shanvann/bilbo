"""Monitor performance section — thin HTTP client of the dashboard's API.

baby-report no longer computes BIRDEYE metrics locally. It queries the
dashboard's /api/safety-stats and /api/monitor-stats endpoints, so the
agent reading the report sees the exact same numbers as the live UI.
This is intentional: there's no local SQL or F1 computation that could
drift from the dashboard.

If the dashboard is unreachable the section returns a clear error
pointing at the launchctl command to start it. There is no silent
fallback to local SQL — that's how drift starts.

Important: the dashboard's /api/safety-stats GT pairs are computed over
ALL TIME, not the request window. The `hours` parameter only scopes the
windowed shadow / face-detection / production stats. We surface this in
the section header so the agent knows the F1 numbers aren't windowed.
"""

import json
import urllib.error
import urllib.request

from .config import DASHBOARD_URL


class DashboardUnreachable(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP fetching
# ---------------------------------------------------------------------------

def _fetch(path: str, hours: float | None = None) -> dict:
    url = DASHBOARD_URL.rstrip("/") + path
    if hours is not None:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}hours={hours}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        raise DashboardUnreachable(
            f"Could not reach {url}: {e}.\n"
            "Is the dashboard running? Check:\n"
            "  launchctl list | grep baby-monitor-dashboard\n"
            "  launchctl load ~/Library/LaunchAgents/com.baby-monitor-dashboard.plist"
        ) from e


def fetch_monitor_stats(hours: float) -> dict:
    return _fetch("/api/monitor-stats", hours=hours)


def fetch_safety_stats(hours: float) -> dict:
    return _fetch("/api/safety-stats", hours=hours)


def fetch_all(hours: float) -> dict:
    """Fetch both endpoints and return as a single dict for the JSON output."""
    return {
        "monitor_stats": fetch_monitor_stats(hours),
        "safety_stats": fetch_safety_stats(hours),
    }


# ---------------------------------------------------------------------------
# Text section
# ---------------------------------------------------------------------------

def monitor_section(num_days: float, start, end) -> str:
    """Render the monitor section by calling the dashboard APIs."""
    span_hours = (end - start).total_seconds() / 3600
    try:
        ms = fetch_monitor_stats(span_hours)
        ss = fetch_safety_stats(span_hours)
    except DashboardUnreachable as e:
        return "**🔍 Monitor Performance**\n⚠ " + str(e).replace("\n", "\n  ")

    lines = []
    lines.extend(_render_production(ms, span_hours))
    lines.append("")
    lines.extend(_render_classifiers(ss))
    return "\n".join(lines)


def _render_production(ms: dict, span_hours: float) -> list[str]:
    total = ms.get("total", 0)
    if total == 0:
        return ["**🔍 Monitor Performance**", "No entries in this window."]

    expected = int(span_hours * 60 / 4)  # one capture every 4 minutes
    methods = ms.get("methods", {}) or {}
    cloud = methods.get("cloud_api", 0)
    pixel = methods.get("pixel_diff", 0)
    bird = methods.get("birdeye", 0)
    cost = ms.get("cost", {}) or {}

    lines = ["**🔍 Monitor Performance** (last {0:.0f}h)".format(span_hours)]
    lines.append(
        f"- Entries: {total} (expected ~{expected}, coverage {_pct(total, expected)})"
    )
    lines.append(
        f"- Production decision source: cloud API {cloud} ({_pct(cloud, total)}), "
        f"pixel-diff {pixel} ({_pct(pixel, total)}), birdeye {bird} ({_pct(bird, total)})"
    )
    lines.append(
        f"- API calls: {cost.get('apiCalls', 0)} (est. ${cost.get('estCost', 0):.2f}), "
        f"avoided: {cost.get('apiAvoided', 0)} (est. ${cost.get('estSaved', 0):.2f} saved)"
    )

    cloud_models = ms.get("cloudModels", {}) or {}
    if cloud_models:
        bits = ", ".join(f"{k} ({v})" for k, v in cloud_models.items())
        lines.append(f"- Cloud models: {bits}")

    timing = ms.get("timing")
    if timing:
        lines.append(
            f"- BIRDEYE inference latency: avg {timing['avg']*1000:.0f}ms, "
            f"p50 {timing['p50']*1000:.0f}ms, p99 {timing['p99']*1000:.0f}ms, "
            f"max {timing['max']*1000:.0f}ms"
        )

    conf = ms.get("confidence", {}) or {}
    pc, ec = conf.get("presence"), conf.get("eye")
    if pc:
        lines.append(f"- Presence confidence: avg {pc['avg']:.3f}, min {pc['min']:.3f}")
    if ec:
        lines.append(f"- Eye confidence: avg {ec['avg']:.3f}, min {ec['min']:.3f}")

    gaps = ms.get("gaps", 0)
    if gaps:
        lines.append(f"- Capture gaps >10min: {gaps}")

    shadow = ms.get("shadow", {}) or {}
    if shadow.get("total"):
        rate = shadow.get("agreementRate")
        rate_str = f"{rate*100:.1f}%" if rate is not None else "—"
        lines.append(
            f"- Shadow agreement (BIRDEYE vs prod state, when both produced one): "
            f"{shadow.get('agreed', 0)}/{shadow['total']} ({rate_str})"
        )

    return lines


def _render_classifiers(ss: dict) -> list[str]:
    """Render the BIRDEYE Classifiers card data from /api/safety-stats."""
    deployed = ss.get("deployedVersion") or "(none)"
    rolled_back = ss.get("rolledBack")
    shadow_total = ss.get("shadowTotal", 0)
    gt = ss.get("groundTruth", {}) or {}

    lines = ["**🦅 BIRDEYE Classifiers** (vs Ground Truth — across all reviewed/corrected frames, NOT windowed)"]
    deploy_line = f"- Deployed model: {deployed}"
    if rolled_back:
        deploy_line += f"  ⚠ rolled back from {ss.get('latestTrainedVersion')}"
    lines.append(deploy_line)
    lines.append(
        f"- Shadow frames in window: {shadow_total} "
        "(BIRDEYE only runs on baby-in-bassinet frames)"
    )
    lines.append(
        f"- Ground truth labels: {gt.get('total', 0)} "
        f"({gt.get('reviewed', 0)} reviewed, {gt.get('corrected', 0)} corrected)"
    )

    fd = ss.get("faceDetection") or {}
    if fd.get("total"):
        lines.append(
            f"- Face detection: {fd.get('detected', 0)}/{fd['total']} "
            f"({_safe_pct(fd.get('detectionRate'))} detection, "
            f"{_safe_pct(fd.get('fallbackRate'))} fallback)"
        )

    lines.append("")
    lines.extend(_render_classifier_panel(
        "Presence (in bassinet vs not)",
        ss.get("presence") or {},
        ["not_present", "present"],
    ))
    lines.append("")
    lines.extend(_render_classifier_panel(
        "Eye-state (eyes_open vs eyes_closed)",
        ss.get("eyeState") or {},
        ["eyes_open", "eyes_closed"],
    ))
    return lines


def _render_classifier_panel(title: str, panel: dict, classes: list[str]) -> list[str]:
    lines = [f"**{title}**"]
    bird = panel.get("birdeyeVsGT")
    cloud = panel.get("cloudVsGT")

    if not bird and not cloud:
        lines.append("- No ground truth data yet.")
        return lines

    if bird:
        lines.append(
            f"- BIRDEYE: macro-F1 {bird['macroF1']*100:.1f}%, "
            f"accuracy {bird['accuracy']*100:.1f}%  (n={bird['total']})"
        )
        for cls in classes:
            pc = (bird.get("perClass") or {}).get(cls)
            if not pc or not pc.get("support"):
                continue
            lines.append(
                f"    {cls} (n={pc['support']}): "
                f"P={_fmt(pc.get('precision'))} R={_fmt(pc.get('recall'))} "
                f"F1={_fmt(pc.get('f1'))}"
            )

    if cloud:
        lines.append(
            f"- Cloud API: macro-F1 {cloud['macroF1']*100:.1f}%, "
            f"accuracy {cloud['accuracy']*100:.1f}%  (n={cloud['total']})"
        )
        for cls in classes:
            pc = (cloud.get("perClass") or {}).get(cls)
            if not pc or not pc.get("support"):
                continue
            lines.append(
                f"    {cls} (n={pc['support']}): "
                f"P={_fmt(pc.get('precision'))} R={_fmt(pc.get('recall'))} "
                f"F1={_fmt(pc.get('f1'))}"
            )

    return lines


# ---------------------------------------------------------------------------
# JSON section (consumed by --format json)
# ---------------------------------------------------------------------------

def monitor_metrics_dict(start, end) -> dict:
    """Return the structured monitor metrics for the JSON report.

    Returns the merged dashboard payload, or a dict with `error` set if
    the dashboard is unreachable. The shape is intentionally
    pass-through so downstream consumers see the dashboard's API
    contract directly.
    """
    span_hours = (end - start).total_seconds() / 3600
    try:
        return {
            "monitor_stats": fetch_monitor_stats(span_hours),
            "safety_stats": fetch_safety_stats(span_hours),
        }
    except DashboardUnreachable as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "—"


def _pct(n: int, total: int) -> str:
    if not total:
        return "0%"
    return f"{n * 100 / total:.0f}%"


def _safe_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.0f}%"
