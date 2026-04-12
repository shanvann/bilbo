"""Monitor performance section — production decision stats + BIRDEYE shadow analysis.

Two distinct things are reported:

1. **Production decision source** — what actually decided each entry:
   `vision-api` (cloud API), `pixel-diff` (empty-bassinet fast path), or
   `birdeye` (only when BIRDEYE is promoted out of shadow mode).

2. **Shadow BIRDEYE performance** — BIRDEYE runs in parallel on every frame.
   Its outputs live in the `shadow` sub-dict on each entry. We report latency,
   confidences, fallback usage, and an **eye-state** evaluation (eyes_open vs
   eyes_closed) against ground truth. Body-state (Asleep/Awake) analysis is
   intentionally omitted — that taxonomy is being redefined upstream.

All functions are pure (no I/O) — loaders hand them a list of entries.
"""

from collections import Counter


# ---------------------------------------------------------------------------
# Production-decision analysis (stable shape — consumed by JSON output)
# ---------------------------------------------------------------------------

def analyze_monitor_entries(entries: list[dict]) -> dict:
    """Analyze entries and return structured metrics.

    Returns a dict with top-level keys: total, methods, birdeye, cloud_api,
    pixel_diff, gaps, alerts, cost, shadow. The `shadow` key is populated when
    entries carry a `shadow` sub-dict (DB-loaded entries do).
    """
    total = len(entries)
    if total == 0:
        return {"total": 0}

    methods = Counter(e.get("detectionMethod", "unknown") for e in entries)

    birdeye_prod = [e for e in entries if e.get("detectionMethod") == "birdeye"]
    cloud = [e for e in entries if e.get("detectionMethod") in ("vision-api", "openai-vision")]
    pixel_diff = [e for e in entries if e.get("detectionMethod") == "pixel-diff"]

    cloud_models = Counter(e.get("modelUsed", "unknown") for e in cloud)

    gaps = []
    for i in range(1, len(entries)):
        gap_sec = (entries[i]["_ts"] - entries[i - 1]["_ts"]).total_seconds()
        if gap_sec > 600:
            gaps.append({
                "start": entries[i - 1]["_ts"],
                "end": entries[i]["_ts"],
                "minutes": gap_sec / 60,
            })

    alert_entries = [e for e in entries if e.get("alerts")]
    alert_types = Counter()
    for e in alert_entries:
        for a in e.get("alerts", []):
            alert_types[a] += 1

    api_saved = len(birdeye_prod) + len(pixel_diff)
    est_cost = len(cloud) * 0.01
    est_saved = api_saved * 0.01

    result = {
        "total": total,
        "methods": dict(methods),
        "birdeye": {
            "count": len(birdeye_prod),
            "rate": round(len(birdeye_prod) / total, 3) if total else 0,
        },
        "cloud_api": {
            "count": len(cloud),
            "models": dict(cloud_models),
        },
        "pixel_diff": {"count": len(pixel_diff)},
        "gaps": gaps,
        "alerts": {"count": len(alert_entries), "types": dict(alert_types)},
        "cost": {"api_calls": len(cloud), "est_cost": round(est_cost, 2),
                 "api_avoided": api_saved, "est_saved": round(est_saved, 2)},
    }

    shadow = analyze_shadow_performance(entries)
    if shadow["count"] > 0:
        result["shadow"] = shadow

    return result


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    s = sorted(values)
    return {
        "avg": round(sum(s) / len(s), 4),
        "min": round(s[0], 4),
        "max": round(s[-1], 4),
        "p50": round(s[len(s) // 2], 4),
        "p95": round(s[min(int(len(s) * 0.95), len(s) - 1)], 4),
        "count": len(s),
    }


# ---------------------------------------------------------------------------
# Shadow-mode BIRDEYE analysis (eye-state focused)
# ---------------------------------------------------------------------------

# Eye states BIRDEYE actually classifies. Other values the cloud API emits
# (face_not_visible, not_in_bassinet, None) indicate the eye-state classifier
# can't or shouldn't run, and are handled separately as diagnostics.
_EYE_CLASS_LABELS = ("eyes_open", "eyes_closed")
_EYE_NON_CLASS = ("face_not_visible", "not_in_bassinet")


def analyze_shadow_performance(entries: list[dict]) -> dict:
    """Analyze BIRDEYE shadow-mode performance.

    Reports operational metrics (latency, confidences, fallbacks, model
    versions) plus an eye-state evaluation:

      - alignment vs prod (on frames where prod has a real eye label)
      - confusion matrix (2x2: eyes_open vs eyes_closed)
      - per-class P/R/F1
      - ground-truth slice (entries with _reviewed=True, using _eye_state_gt
        as the label since that column is always authoritative post-review)
      - diagnostics: unclassified (BIRDEYE None when GT is a real eye label),
        hallucinated (BIRDEYE returned a real label when GT was
        face_not_visible / not_in_bassinet)
    """
    # "BIRDEYE ran on this frame" — test multiple fields so we don't break if
    # the legacy `birdeyeState` key is ever dropped from the JSON shadow blob.
    shadow_entries = [
        e for e in entries
        if isinstance(e.get("shadow"), dict)
        and (
            e["shadow"].get("birdeyeState")
            or e["shadow"].get("eyeState")
            or e["shadow"].get("birdeyeTimings")
        )
    ]
    count = len(shadow_entries)
    if count == 0:
        return {"count": 0}

    # --- Operational metrics ---
    presence_confs = [e["shadow"].get("presenceConfidence") for e in shadow_entries
                      if e["shadow"].get("presenceConfidence") is not None]
    eye_confs = [e["shadow"].get("eyeConfidence") for e in shadow_entries
                 if e["shadow"].get("eyeConfidence") is not None]
    timings = [e["shadow"].get("birdeyeTimings", {}).get("total")
               for e in shadow_entries]
    timings = [t for t in timings if t is not None]

    fallbacks = Counter()
    for e in shadow_entries:
        fb = e["shadow"].get("fallback")
        if fb:
            fallbacks[fb] += 1

    versions = Counter(e.get("shadowModelVersion") for e in shadow_entries
                       if e.get("shadowModelVersion"))

    # --- Eye-state evaluation ---
    eye_block = _analyze_eye_state(shadow_entries)

    return {
        "count": count,
        "confidence": {
            "presence": _stats(presence_confs),
            "eye": _stats(eye_confs),
        },
        "timing_total": _stats(timings),
        "fallbacks": dict(fallbacks),
        "model_versions": dict(versions),
        "eye_state": eye_block,
    }


def _birdeye_eye(e) -> str | None:
    """BIRDEYE's eye-state prediction for this frame (None if not classified)."""
    return e.get("shadow", {}).get("eyeState")


def _prod_eye(e) -> str | None:
    """Production (cloud API / corrected) eye-state label, or None.

    Pulled from the DB's top-level `eye_state` column which is always the
    current authoritative label: it gets overwritten on correction and is
    what a reviewer saw when clicking 'reviewed'.
    """
    return e.get("_eye_state_gt")


def _analyze_eye_state(shadow_entries: list[dict]) -> dict:
    """Eye-state (eyes_open vs eyes_closed) analysis — alignment, confusion, P/R/F1."""

    # Base population: frames where BOTH sides produced one of the two real
    # eye labels. Everything else is pushed to diagnostics.
    paired = []
    for e in shadow_entries:
        prod = _prod_eye(e)
        birdeye = _birdeye_eye(e)
        if prod in _EYE_CLASS_LABELS and birdeye in _EYE_CLASS_LABELS:
            paired.append((e, prod, birdeye))

    # --- Diagnostics ---
    unclassified = 0     # prod has real eye label, BIRDEYE returned None
    hallucinated = 0     # prod is face_not_visible / not_in_bassinet, BIRDEYE returned a real label
    declined_ok = 0      # prod is face_not_visible / not_in_bassinet, BIRDEYE also None
    birdeye_unexpected = 0  # BIRDEYE returned a real eye label on a frame with no prod eye label at all
    for e in shadow_entries:
        prod = _prod_eye(e)
        birdeye = _birdeye_eye(e)
        if prod in _EYE_CLASS_LABELS and birdeye not in _EYE_CLASS_LABELS:
            unclassified += 1
        elif prod in _EYE_NON_CLASS and birdeye in _EYE_CLASS_LABELS:
            hallucinated += 1
        elif prod in _EYE_NON_CLASS and birdeye not in _EYE_CLASS_LABELS:
            declined_ok += 1
        elif prod is None and birdeye in _EYE_CLASS_LABELS:
            birdeye_unexpected += 1

    # --- Alignment vs prod (on paired rows) ---
    alignment = None
    confusion = []
    per_class = {}
    if paired:
        agreed = sum(1 for _, p, b in paired if p == b)
        alignment = {"agreed": agreed, "total": len(paired),
                     "rate": round(agreed / len(paired), 3)}

        confusion_counter = Counter()
        for _, p, b in paired:
            confusion_counter[(p, b)] += 1
        confusion = [
            {"prod": p, "birdeye": b, "count": n, "agreed": p == b}
            for (p, b), n in sorted(confusion_counter.items(), key=lambda x: -x[1])
        ]

        per_class = {lbl: _per_class_metrics(paired, lbl) for lbl in _EYE_CLASS_LABELS}

    # --- Ground-truth slice ---
    # GT rules for eye state:
    #   - If frame has `_eye_state_edited=True`, the top-level eye_state is a
    #     corrected label → authoritative GT.
    #   - Else if `_reviewed=True`, the reviewer saw the label and didn't
    #     change it → treat it as GT.
    #   - Else skip.
    gt_paired = []
    gt_sources = Counter()
    for e in shadow_entries:
        prod = _prod_eye(e)
        birdeye = _birdeye_eye(e)
        if prod not in _EYE_CLASS_LABELS or birdeye not in _EYE_CLASS_LABELS:
            continue
        if e.get("_eye_state_edited"):
            gt_sources["corrected"] += 1
            gt_paired.append((e, prod, birdeye))
        elif e.get("_reviewed"):
            gt_sources["reviewed_uncorrected"] += 1
            gt_paired.append((e, prod, birdeye))

    gt_block = None
    if gt_paired:
        gt_correct = sum(1 for _, p, b in gt_paired if p == b)
        gt_per_class = {lbl: _per_class_metrics(gt_paired, lbl) for lbl in _EYE_CLASS_LABELS}
        gt_block = {
            "count": len(gt_paired),
            "sources": dict(gt_sources),
            "correct": gt_correct,
            "accuracy": round(gt_correct / len(gt_paired), 3),
            "per_class": gt_per_class,
        }

    return {
        "paired_count": len(paired),
        "alignment": alignment,
        "confusion": confusion,
        "per_class": per_class,
        "ground_truth": gt_block,
        "diagnostics": {
            "unclassified": unclassified,
            "hallucinated": hallucinated,
            "declined_correctly": declined_ok,
            "birdeye_unexpected": birdeye_unexpected,
        },
    }


def _per_class_metrics(paired: list, label: str) -> dict:
    tp = sum(1 for _, p, b in paired if p == label and b == label)
    fp = sum(1 for _, p, b in paired if p != label and b == label)
    fn = sum(1 for _, p, b in paired if p == label and b != label)
    support = tp + fn
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / support if support else None
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "support": support,
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
    }


# ---------------------------------------------------------------------------
# Text section
# ---------------------------------------------------------------------------

def monitor_section(entries: list[dict], num_days: float, start, end) -> str:
    m = analyze_monitor_entries(entries)

    if m["total"] == 0:
        return "**🔍 Monitor Performance**\nNo entries in this period."

    total = m["total"]
    span_hours = (end - start).total_seconds() / 3600
    expected = int(span_hours * 60 / 4)

    lines = ["**🔍 Monitor Performance**"]
    lines.append(f"- Entries: {total} (expected ~{expected}, coverage {_pct(total, expected)})")

    b = m["birdeye"]
    c = m["cloud_api"]
    p = m["pixel_diff"]
    lines.append(
        f"- Production decision source: cloud API {c['count']} ({_pct(c['count'], total)}), "
        f"pixel-diff {p['count']} ({_pct(p['count'], total)}), "
        f"birdeye {b['count']} ({_pct(b['count'], total)})"
    )

    cost = m["cost"]
    lines.append(
        f"- API calls: {cost['api_calls']} (est. ${cost['est_cost']:.2f}), "
        f"avoided: {cost['api_avoided']} (est. ${cost['est_saved']:.2f} saved)"
    )

    if c["count"] > 0:
        models = ", ".join(f"{k} ({v})" for k, v in c["models"].items())
        lines.append(f"- Cloud models: {models}")

    gaps = m["gaps"]
    if gaps:
        lines.append(f"- Gaps >10min: {len(gaps)}")
        for g in gaps[:3]:
            lines.append(
                f"  - {g['start'].strftime('%m/%d %H:%M')} → "
                f"{g['end'].strftime('%H:%M')} ({g['minutes']:.0f}min)"
            )
        if len(gaps) > 3:
            lines.append(f"  - ...and {len(gaps) - 3} more")

    if m["alerts"]["count"] > 0:
        lines.append(f"- Alerts: {m['alerts']['count']}")

    shadow = m.get("shadow")
    if shadow:
        lines.append("")
        lines.extend(_shadow_lines(shadow))

    return "\n".join(lines)


def _shadow_lines(shadow: dict) -> list[str]:
    lines = ["**🦅 BIRDEYE Shadow Performance**"]

    versions = shadow.get("model_versions") or {}
    if versions:
        sorted_v = sorted(versions.items(), key=lambda x: -x[1])
        top = sorted_v[:3]
        vs = ", ".join(f"{v} ({n})" for v, n in top)
        if len(sorted_v) > 3:
            others = sum(n for _, n in sorted_v[3:])
            vs += f", +{len(sorted_v) - 3} older ({others})"
        lines.append(f"- Shadow model: {vs}")

    t = shadow.get("timing_total")
    if t:
        lines.append(
            f"- Inference latency: avg {t['avg']*1000:.0f}ms, "
            f"p50 {t['p50']*1000:.0f}ms, p95 {t['p95']*1000:.0f}ms, max {t['max']*1000:.0f}ms"
        )

    conf = shadow.get("confidence") or {}
    pc, ec = conf.get("presence"), conf.get("eye")
    if pc:
        lines.append(f"- Presence confidence: avg {pc['avg']:.3f}, min {pc['min']:.3f}")
    if ec:
        lines.append(f"- Eye classifier confidence: avg {ec['avg']:.3f}, min {ec['min']:.3f}")

    fallbacks = shadow.get("fallbacks") or {}
    if fallbacks:
        fb = ", ".join(f"{k} ({v})" for k, v in fallbacks.items())
        lines.append(f"- Fallbacks used: {fb}")

    eye = shadow.get("eye_state") or {}
    lines.extend(_eye_state_lines(eye))

    return lines


def _eye_state_lines(eye: dict) -> list[str]:
    lines = ["", "**👁  Eye-state (eyes_open vs eyes_closed)**"]

    align = eye.get("alignment")
    if align:
        lines.append(
            f"- Alignment vs prod: {align['agreed']}/{align['total']} "
            f"({align['rate']*100:.1f}%) on frames where both sides classified eye state"
        )
    else:
        lines.append("- No paired frames (prod + birdeye both classified eye state) in this window.")

    confusion = eye.get("confusion") or []
    if confusion:
        lines.append("- Confusion (prod → birdeye):")
        for r in confusion:
            mark = "✓" if r["agreed"] else "✗"
            lines.append(f"    {mark} {r['prod']} → {r['birdeye']}: {r['count']}")

    per_class = eye.get("per_class") or {}
    small_sample = False
    printed_any = False
    for label in _EYE_CLASS_LABELS:
        pc = per_class.get(label)
        if not pc or pc["support"] == 0:
            continue
        printed_any = True
        if pc["support"] < 20:
            small_sample = True
        lines.append(f"    {label} (n={pc['support']}): "
                     f"P={_fmt(pc['precision'])} R={_fmt(pc['recall'])} F1={_fmt(pc['f1'])}")
    if printed_any and small_sample:
        lines.append("    ⚠ support <20; P/R/F1 are indicative, not statistically meaningful")

    gt = eye.get("ground_truth")
    if gt:
        src_str = ", ".join(f"{k}={v}" for k, v in gt["sources"].items())
        lines.append(
            f"- Ground truth: {gt['correct']}/{gt['count']} correct "
            f"({gt['accuracy']*100:.1f}%)  [{src_str}]"
        )
        gt_small = False
        for label in _EYE_CLASS_LABELS:
            pc = gt["per_class"].get(label)
            if not pc or pc["support"] == 0:
                continue
            if pc["support"] < 20:
                gt_small = True
            lines.append(f"    {label} (n={pc['support']}): "
                         f"P={_fmt(pc['precision'])} R={_fmt(pc['recall'])} F1={_fmt(pc['f1'])}")
        if gt_small:
            lines.append("    ⚠ GT support <20; P/R/F1 are indicative, not statistically meaningful")

    diag = eye.get("diagnostics") or {}
    diag_bits = []
    if diag.get("unclassified"):
        diag_bits.append(f"unclassified {diag['unclassified']} (prod had eye label, BIRDEYE returned none)")
    if diag.get("hallucinated"):
        diag_bits.append(f"hallucinated {diag['hallucinated']} (prod said face_not_visible / not_in_bassinet, BIRDEYE returned a label)")
    if diag.get("declined_correctly"):
        diag_bits.append(f"declined correctly {diag['declined_correctly']}")
    if diag.get("birdeye_unexpected"):
        diag_bits.append(
            f"birdeye unexpected {diag['birdeye_unexpected']} "
            "(BIRDEYE returned an eye label on a frame with no prod eye label at all)"
        )
    if diag_bits:
        lines.append("- Diagnostics: " + "; ".join(diag_bits))

    return lines


def _fmt(v) -> str:
    return f"{v:.2f}" if v is not None else "—"


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{n * 100 / total:.0f}%"
