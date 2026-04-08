"""Report generation — text and JSON output."""

import json

from .loaders import load_activity_csv, load_sleep_log, parse_duration_str
from .sleep import analyze_sleep_monitor, sleep_section
from .sections import feeding_section, pump_section, diaper_section, weight_section
from .monitor import analyze_monitor_entries, monitor_section


def generate_report(start, end, sections=None, csv_path=None):
    """Generate the full text report."""
    num_days = max((end - start).total_seconds() / 86400, 1)

    rows = load_activity_csv(start, end, csv_path=csv_path)
    monitor_entries = load_sleep_log(start, end)

    all_sections = sections or ['sleep', 'feeding', 'pumping', 'diapers', 'weight', 'monitor']

    if num_days <= 1:
        range_label = f"Last {int(num_days * 24)}h"
    else:
        range_label = f"{start.strftime('%b %d')} – {end.strftime('%b %d')}"

    parts = [f"**📊 Baby Activity Report — {range_label}**\n"]

    feed_weight_notes = []
    if 'feeding' in all_sections or 'weight' in all_sections:
        feed_text, feed_weight_notes = feeding_section(rows, num_days)

    if 'sleep' in all_sections:
        parts.append(sleep_section(rows, monitor_entries, num_days, start, end))
    if 'feeding' in all_sections:
        parts.append(feed_text)
    if 'pumping' in all_sections:
        parts.append(pump_section(rows, num_days))
    if 'diapers' in all_sections:
        parts.append(diaper_section(rows, num_days))
    if 'weight' in all_sections and feed_weight_notes:
        parts.append(weight_section(feed_weight_notes))
    if 'monitor' in all_sections:
        parts.append(monitor_section(monitor_entries, num_days, start, end))

    return '\n\n'.join(parts)


def generate_json_report(start, end, csv_path=None):
    """Generate structured JSON report."""
    num_days = max((end - start).total_seconds() / 86400, 1)
    rows = load_activity_csv(start, end, csv_path=csv_path)
    monitor_entries = load_sleep_log(start, end)

    monitor_stretches = analyze_sleep_monitor(monitor_entries)

    # CSV sleep for days without camera data
    csv_sleep_min = 0
    csv_sleep_blocks = 0
    monitor_days = set(s['start'].strftime('%Y-%m-%d') for s in monitor_stretches)
    for r in rows:
        if r['Type'].strip() == 'Sleep' and r['_ts'].strftime('%Y-%m-%d') not in monitor_days:
            dur = parse_duration_str(r.get('Duration', ''))
            if dur > 0:
                csv_sleep_min += dur
                csv_sleep_blocks += 1

    monitor_total_min = int(sum(s['duration'] for s in monitor_stretches))
    total_sleep_min = csv_sleep_min + monitor_total_min
    total_blocks = csv_sleep_blocks + len(monitor_stretches)

    longest_monitor = max(monitor_stretches, key=lambda x: x['duration']) if monitor_stretches else None
    longest_min = int(longest_monitor['duration']) if longest_monitor else 0

    feed_rows = [r for r in rows if r['Type'].strip() == 'Feed']
    pump_rows = [r for r in rows if r['Type'].strip() == 'Pump']
    diaper_rows = [r for r in rows if r['Type'].strip() == 'Diaper']

    # Monitor performance metrics
    monitor_metrics = analyze_monitor_entries(monitor_entries)
    # Remove non-serializable datetime objects from gaps
    serializable_gaps = []
    for g in monitor_metrics.get("gaps", []):
        serializable_gaps.append({
            "start": g["start"].isoformat() if hasattr(g["start"], "isoformat") else str(g["start"]),
            "end": g["end"].isoformat() if hasattr(g["end"], "isoformat") else str(g["end"]),
            "minutes": round(g["minutes"], 1),
        })
    monitor_metrics["gaps"] = serializable_gaps

    report = {
        'range': {'start': start.isoformat(), 'end': end.isoformat(), 'days': round(num_days, 1)},
        'sleep': {
            'total_minutes': total_sleep_min,
            'avg_per_day_hours': round(total_sleep_min / num_days / 60, 1) if num_days > 0 else 0,
            'block_count': total_blocks,
            'longest_stretch_min': longest_min,
        },
        'feeding': {
            'total_feeds': len(feed_rows),
            'feeds_per_day': round(len(feed_rows) / num_days, 1) if num_days > 0 else 0,
        },
        'pumping': {
            'sessions': len(pump_rows),
            'sessions_per_day': round(len(pump_rows) / num_days, 1) if num_days > 0 else 0,
        },
        'diapers': {
            'total': len(diaper_rows),
            'per_day': round(len(diaper_rows) / num_days, 1) if num_days > 0 else 0,
        },
        'monitor': monitor_metrics,
    }
    return json.dumps(report, indent=2)
