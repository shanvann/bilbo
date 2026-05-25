"""Sleep analysis from CSV and camera monitor data."""

from collections import defaultdict

from .loaders import parse_duration_str


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


def sleep_section(rows, monitor_entries, num_days, range_start, range_end):
    """Generate sleep report section.
    
    Uses camera monitor data when available (ground truth).
    Falls back to CSV for days without camera data.
    """
    csv_blocks = []
    csv_total_min = 0
    monitor_stretches = analyze_sleep_monitor(monitor_entries)

    # Determine which days have camera data
    monitor_days = set()
    for s in monitor_stretches:
        monitor_days.add(s['start'].strftime('%Y-%m-%d'))

    # Use CSV sleep for days without camera data
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

    all_durations = [b['duration'] for b in csv_blocks] + [s['duration'] for s in monitor_stretches]

    if not all_durations:
        return "**😴 Sleep**\n- No sleep data for this period"

    avg_per_day = total_min / num_days if num_days > 0 else total_min
    shortest_dur = min(all_durations)

    # Find the longest stretch with its time info
    longest_csv_block = max(csv_blocks, key=lambda x: x['duration']) if csv_blocks else None
    longest_monitor = max(monitor_stretches, key=lambda x: x['duration']) if monitor_stretches else None

    if longest_csv_block and longest_monitor:
        if longest_csv_block['duration'] >= longest_monitor['duration']:
            longest_block, longest_source = longest_csv_block, 'csv'
        else:
            longest_block, longest_source = longest_monitor, 'monitor'
    elif longest_csv_block:
        longest_block, longest_source = longest_csv_block, 'csv'
    else:
        longest_block, longest_source = longest_monitor, 'monitor'

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

    # Daily breakdown
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
