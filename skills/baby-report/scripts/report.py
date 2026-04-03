#!/usr/bin/env python3
"""
Baby Activity Report Generator

Generates a comprehensive report from:
  1. Activity log CSV (feeds, pumps, diapers, sleep from manual tracking)
  2. Sleep monitor JSONL (camera-based bassinet monitoring)

Usage:
  report.py --range 24h          # Last 24 hours
  report.py --range 7d           # Last 7 days
  report.py --from 2026-03-25 --to 2026-03-31
  report.py --range 24h --section sleep   # Only sleep section
  report.py --range 7d --format json      # JSON output
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# Paths
SKILL_DIR = Path(__file__).resolve().parent.parent
MONITOR_DIR = SKILL_DIR.parent / "baby-monitor" / "data"
ACTIVITY_CSV = MONITOR_DIR / "activity-log.csv"
SLEEP_JSONL = MONITOR_DIR / "sleep-log.jsonl"

# Feeding schedule times (hour in local time)
FEED_HOURS = [23, 2, 5, 8, 11, 14, 17, 20]


def parse_time_range(args):
    """Parse --range or --from/--to into (start, end) datetimes."""
    now = datetime.now()
    if args.range:
        m = re.match(r'^(\d+)(h|d|w)$', args.range)
        if not m:
            print(f"Error: invalid range '{args.range}'. Use e.g. 24h, 7d, 2w", file=sys.stderr)
            sys.exit(1)
        val, unit = int(m.group(1)), m.group(2)
        delta = {'h': timedelta(hours=val), 'd': timedelta(days=val), 'w': timedelta(weeks=val)}[unit]
        return now - delta, now
    elif args.start and args.end:
        try:
            start = datetime.strptime(args.start, '%Y-%m-%d')
            end = datetime.strptime(args.end, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            return start, end
        except ValueError:
            print("Error: dates must be YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: provide --range or both --from and --to", file=sys.stderr)
        sys.exit(1)


def load_activity_csv(start, end):
    """Load and filter activity CSV entries."""
    if not ACTIVITY_CSV.exists():
        return []
    rows = []
    with open(ACTIVITY_CSV) as f:
        reader = csv.DictReader(f)
        for r in reader:
            start_str = (r.get('Start') or '').strip()
            if not start_str:
                continue
            try:
                ts = datetime.strptime(start_str, '%Y-%m-%d %H:%M')
            except ValueError:
                continue
            if start <= ts <= end:
                r['_ts'] = ts
                rows.append(r)
    return rows


def load_sleep_log(start, end):
    """Load and filter camera sleep log entries."""
    if not SLEEP_JSONL.exists():
        return []
    entries = []
    with open(SLEEP_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00')).replace(tzinfo=None)
                if start <= ts <= end:
                    e['_ts'] = ts
                    entries.append(e)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    entries.sort(key=lambda x: x['_ts'])
    return entries


def parse_duration_str(dur_str):
    """Parse HH:MM duration string to minutes."""
    if not dur_str or not dur_str.strip():
        return 0
    parts = dur_str.strip().split(':')
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


def parse_ml(val):
    """Extract ml from a string like '60ml', '0.5oz', '30ml'."""
    if not val:
        return 0
    val = val.strip().lower()
    if 'ml' in val:
        try:
            return int(re.search(r'(\d+)', val).group(1))
        except (AttributeError, ValueError):
            return 0
    if 'oz' in val:
        try:
            oz = float(re.search(r'([\d.]+)', val).group(1))
            return int(oz * 29.57)
        except (AttributeError, ValueError):
            return 0
    try:
        return int(val)
    except ValueError:
        return 0


# ── Section generators ──

def analyze_sleep_csv(rows):
    """Analyze sleep from activity CSV."""
    sleep_rows = [r for r in rows if r['Type'].strip() == 'Sleep']
    blocks = []
    total_min = 0
    for s in sleep_rows:
        dur_min = parse_duration_str(s.get('Duration', ''))
        loc = (s.get('Start Location') or '').strip()
        start = s['_ts']
        if dur_min > 0:
            blocks.append({'start': start, 'duration': dur_min, 'location': loc})
            total_min += dur_min
    return blocks, total_min


def analyze_sleep_monitor(entries):
    """Analyze in-bassinet stretches from camera data."""
    stretches = []
    current_start = None
    current_end = None
    for e in entries:
        ts = e['_ts']
        if e.get('babyPresent', False):
            if current_start is None:
                current_start = ts
            current_end = ts
        else:
            if current_start is not None:
                dur = (current_end - current_start).total_seconds() / 60
                if dur >= 4:  # minimum 4 min to count
                    stretches.append({'start': current_start, 'end': current_end, 'duration': dur})
                current_start = None
                current_end = None
    if current_start is not None:
        dur = (current_end - current_start).total_seconds() / 60
        if dur >= 4:
            stretches.append({'start': current_start, 'end': current_end, 'duration': dur})
    return stretches


# Camera monitor data starts on this date; use CSV for sleep before this
MONITOR_START = datetime(2026, 3, 28, 0, 0)


def sleep_section(rows, monitor_entries, num_days, range_start, range_end):
    """Generate sleep report section.
    
    Uses camera monitor data from 3/28 onwards (ground truth).
    Falls back to CSV for dates before 3/28.
    """
    # Split the range at MONITOR_START
    csv_blocks = []
    csv_total_min = 0
    monitor_stretches = analyze_sleep_monitor(monitor_entries)

    # Determine which days have camera data
    monitor_days = set()
    for s in monitor_stretches:
        monitor_days.add(s['start'].strftime('%Y-%m-%d'))

    # Use CSV sleep for days before monitor started OR days with no camera data
    all_csv_sleep = [r for r in rows if r['Type'].strip() == 'Sleep']
    for s in all_csv_sleep:
        day_str = s['_ts'].strftime('%Y-%m-%d')
        if day_str not in monitor_days:
            dur_min = parse_duration_str(s.get('Duration', ''))
            if dur_min > 0:
                csv_blocks.append({'start': s['_ts'], 'duration': dur_min,
                                   'location': (s.get('Start Location') or '').strip()})
                csv_total_min += dur_min

    monitor_total_min = sum(s['duration'] for s in monitor_stretches)
    total_min = csv_total_min + monitor_total_min

    # Combine all blocks for stats
    all_durations = [b['duration'] for b in csv_blocks] + [s['duration'] for s in monitor_stretches]

    if not all_durations:
        return "**😴 Sleep**\n- No sleep data for this period"

    avg_per_day = total_min / num_days if num_days > 0 else total_min
    longest_dur = max(all_durations)
    shortest_dur = min(all_durations)

    # Find the longest stretch with its time info
    longest_csv_block = max(csv_blocks, key=lambda x: x['duration']) if csv_blocks else None
    longest_monitor = max(monitor_stretches, key=lambda x: x['duration']) if monitor_stretches else None

    if longest_csv_block and longest_monitor:
        if longest_csv_block['duration'] >= longest_monitor['duration']:
            longest_block = longest_csv_block
            longest_source = 'csv'
        else:
            longest_block = longest_monitor
            longest_source = 'monitor'
    elif longest_csv_block:
        longest_block = longest_csv_block
        longest_source = 'csv'
    else:
        longest_block = longest_monitor
        longest_source = 'monitor'

    lines = ["**😴 Sleep**"]
    lines.append(f"- Total sleep: {int(total_min) // 60}h {int(total_min) % 60}m ({avg_per_day / 60:.1f}h/day avg)")
    lines.append(f"- {len(all_durations)} sleep blocks, avg {int(sum(all_durations) // len(all_durations))}min")

    if longest_source == 'monitor':
        lines.append(f"- Longest uninterrupted stretch: {int(longest_block['duration']) // 60}h {int(longest_block['duration']) % 60}m "
                      f"({longest_block['start'].strftime('%m/%d %I:%M %p')} – {longest_block['end'].strftime('%I:%M %p')})")
    else:
        lines.append(f"- Longest logged nap: {int(longest_block['duration']) // 60}h {int(longest_block['duration']) % 60}m "
                      f"({longest_block['start'].strftime('%m/%d %I:%M %p')})")
    lines.append(f"- Shortest: {int(shortest_dur)}min")

    # Daily breakdown — combine both sources
    daily = defaultdict(float)
    for b in csv_blocks:
        daily[b['start'].strftime('%m/%d')] += b['duration']
    for s in monitor_stretches:
        daily[s['start'].strftime('%m/%d')] += s['duration']
    if len(daily) > 1:
        lines.append("- Daily breakdown:")
        for day in sorted(daily.keys()):
            h, m = divmod(int(daily[day]), 60)
            lines.append(f"  - {day}: {h}h {m}m")

    # Data source note if mixed
    if csv_blocks and monitor_stretches:
        csv_days = sorted(set(b['start'].strftime('%m/%d') for b in csv_blocks))
        lines.append(f"- _Note: days {', '.join(csv_days)} from manual log; rest from camera monitor_")

    # Out-of-bassinet gaps (monitor data only)
    if monitor_stretches:
        gaps = []
        sorted_stretches = sorted(monitor_stretches, key=lambda x: x['start'])
        for i in range(len(sorted_stretches) - 1):
            gap_start = sorted_stretches[i]['end']
            gap_end = sorted_stretches[i + 1]['start']
            gap_min = (gap_end - gap_start).total_seconds() / 60
            if gap_min >= 10:
                gaps.append({'start': gap_start, 'end': gap_end, 'duration': gap_min})

        if gaps:
            top_gaps = sorted(gaps, key=lambda x: -x['duration'])[:5]
            lines.append("- Longest out-of-bassinet gaps:")
            for g in top_gaps:
                lines.append(f"  - {int(g['duration'])}min ({g['start'].strftime('%m/%d %I:%M %p')} – {g['end'].strftime('%I:%M %p')})")

    return '\n'.join(lines)


def feeding_section(rows, num_days):
    """Generate feeding report section."""
    feed_rows = [r for r in rows if r['Type'].strip() == 'Feed']
    breast_count = 0
    bottle_bm_count = 0
    bottle_formula_count = 0
    total_bottle_ml = 0
    breast_durations = []
    weight_notes = []

    for f in feed_rows:
        cond = (f.get('Start Condition') or '').strip()
        loc = (f.get('Start Location') or '').strip()
        end_cond = (f.get('End Condition') or '').strip()
        notes = (f.get('Notes') or '').strip()
        dur = parse_duration_str(f.get('Duration', ''))

        if loc == 'Breast' or 'Breast' in loc:
            breast_count += 1
            if dur > 0:
                breast_durations.append(dur)
        elif loc == 'Bottle' or 'Bottle' in loc:
            ml = parse_ml(end_cond)
            total_bottle_ml += ml
            if cond == 'Formula':
                bottle_formula_count += 1
            else:
                bottle_bm_count += 1
        elif cond in ('Breast Milk', 'Formula'):
            # Bottle feed where location says something else
            ml = parse_ml(end_cond)
            total_bottle_ml += ml
            if cond == 'Formula':
                bottle_formula_count += 1
            else:
                bottle_bm_count += 1
        else:
            # Likely breast if has R/L timing
            if 'R' in cond or 'L' in end_cond or 'Breast' in end_cond:
                breast_count += 1
                if dur > 0:
                    breast_durations.append(dur)

        if notes and ('weight' in notes.lower() or 'Weight' in notes):
            weight_notes.append((f['_ts'].strftime('%m/%d %I:%M %p'), notes))

    total_bottle = bottle_bm_count + bottle_formula_count
    total_feeds = breast_count + total_bottle

    lines = ["**🍼 Feeding**"]
    if total_feeds > 0:
        feeds_per_day = total_feeds / num_days if num_days > 0 else total_feeds
        lines.append(f"- ~{feeds_per_day:.0f} feeds/day ({total_feeds} total: {breast_count} breast + {total_bottle} bottle)")
        if total_bottle > 0:
            avg_bottle = total_bottle_ml // total_bottle if total_bottle else 0
            lines.append(f"- Bottle avg: {avg_bottle}ml | Total bottle volume: {total_bottle_ml}ml")
        if bottle_bm_count > 0 or bottle_formula_count > 0:
            parts = []
            if bottle_bm_count > 0:
                parts.append(f"{bottle_bm_count} breast milk")
            if bottle_formula_count > 0:
                parts.append(f"{bottle_formula_count} formula")
            lines.append(f"- Bottle breakdown: {', '.join(parts)}")
        if breast_durations:
            avg_breast = sum(breast_durations) // len(breast_durations)
            lines.append(f"- Avg breast session: {avg_breast}min")
    else:
        lines.append("- No feed entries for this period")

    return '\n'.join(lines), weight_notes


def pump_section(rows, num_days):
    """Generate pumping report section."""
    pump_rows = [r for r in rows if r['Type'].strip() == 'Pump']
    total_ml = 0
    count = len(pump_rows)

    for p in pump_rows:
        r_val = (p.get('Start Condition') or '').strip()
        l_val = (p.get('End Condition') or '').strip()
        total_ml += parse_ml(r_val)
        total_ml += parse_ml(l_val)

    lines = ["**🤱 Pumping**"]
    if count > 0:
        avg_per_day = count / num_days if num_days > 0 else count
        avg_ml = total_ml // count
        ml_per_day = total_ml / num_days if num_days > 0 else total_ml
        lines.append(f"- {count} sessions (~{avg_per_day:.0f}/day)")
        lines.append(f"- Total pumped: {total_ml}ml (~{ml_per_day:.0f}ml/day)")
        lines.append(f"- Avg per session: {avg_ml}ml (both sides)")
    else:
        lines.append("- No pump entries for this period")

    return '\n'.join(lines)


def diaper_section(rows, num_days):
    """Generate diaper report section.
    
    CSV column mapping for Diaper rows:
      Duration → stool color (yellow/green/brown)
      Start Condition → consistency (Loose/Runny/Solid/Mucousy)
      End Condition → contents (Poo:small, Pee:medium, Both, etc.)
    """
    diaper_rows = [r for r in rows if r['Type'].strip() == 'Diaper']
    total = len(diaper_rows)
    poo_count = 0
    pee_count = 0
    colors = defaultdict(int)

    for d in diaper_rows:
        notes = (d.get('Notes') or '').lower()
        end_cond = (d.get('End Condition') or '').lower()
        # Color is in the Duration column for diaper rows
        color = (d.get('Duration') or '').strip().lower()
        # Consistency is in Start Condition
        consistency = (d.get('Start Condition') or '').strip().lower()

        if 'poo' in notes or 'poo' in end_cond:
            poo_count += 1
        if 'pee' in notes or 'pee' in end_cond or 'both' in end_cond:
            pee_count += 1
        if color and color not in ('unknown', ''):
            colors[color] += 1

    lines = ["**🧷 Diapers**"]
    if total > 0:
        per_day = total / num_days if num_days > 0 else total
        lines.append(f"- {total} changes (~{per_day:.0f}/day)")
        lines.append(f"- With poo: {poo_count} | With pee: {pee_count}")
        if colors:
            color_str = ', '.join(f"{c}: {n}" for c, n in sorted(colors.items(), key=lambda x: -x[1]))
            lines.append(f"- Stool colors: {color_str}")
    else:
        lines.append("- No diaper entries for this period")

    return '\n'.join(lines)


def weight_section(weight_notes):
    """Generate weight tracking section."""
    if not weight_notes:
        return ""
    lines = ["**⚖️ Weight Checks**"]
    for dt, note in weight_notes:
        for part in note.replace('\\n', '\n').split('\n'):
            part = part.strip()
            if part:
                lines.append(f"- {dt}: {part}")
    return '\n'.join(lines)


def generate_report(start, end, sections=None):
    """Generate the full report."""
    num_days = max((end - start).total_seconds() / 86400, 1)

    # Load data
    rows = load_activity_csv(start, end)
    monitor_entries = load_sleep_log(start, end)

    all_sections = sections or ['sleep', 'feeding', 'pumping', 'diapers', 'weight']

    # Header
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

    return '\n\n'.join(parts)


def generate_json_report(start, end):
    """Generate structured JSON report."""
    num_days = max((end - start).total_seconds() / 86400, 1)
    rows = load_activity_csv(start, end)
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
    }
    return json.dumps(report, indent=2)


def main():
    parser = argparse.ArgumentParser(description='Baby Activity Report Generator')
    parser.add_argument('--range', help='Time range: e.g. 24h, 7d, 2w')
    parser.add_argument('--from', dest='start', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--to', dest='end', help='End date (YYYY-MM-DD)')
    parser.add_argument('--section', help='Only show specific section: sleep, feeding, pumping, diapers, weight')
    parser.add_argument('--format', default='text', choices=['text', 'json'], help='Output format')
    parser.add_argument('--csv', help='Path to activity CSV (overrides default)')
    args = parser.parse_args()

    global ACTIVITY_CSV
    if args.csv:
        ACTIVITY_CSV = Path(args.csv)

    start, end = parse_time_range(args)

    if args.format == 'json':
        print(generate_json_report(start, end))
    else:
        sections = [args.section] if args.section else None
        print(generate_report(start, end, sections))


if __name__ == '__main__':
    main()
