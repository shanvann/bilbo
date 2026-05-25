"""Report sections: feeding, pumping, diapers, weight."""

from collections import defaultdict

from .loaders import parse_duration_str, parse_ml


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
            ml = parse_ml(end_cond)
            total_bottle_ml += ml
            if cond == 'Formula':
                bottle_formula_count += 1
            else:
                bottle_bm_count += 1
        else:
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
        color = (d.get('Duration') or '').strip().lower()

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
