#!/usr/bin/env python3
"""Backfill the smoothed `state` field across existing entries.

Walks `data/monitor.db` in timestamp order, applies `smooth_state_temporal`
against a rolling window of prior entries, and updates:

  - `entries.state` indexed column
  - `data` JSON blob: sets `state` to the smoothed value and `rawState` to
    the original per-frame state (preserved for re-smoothing later)

Only touches rows where the smoothed state differs from the stored state.
User eye-state corrections are respected implicitly — the smoother reads
`eyeState` from each entry's data blob, which already reflects any
dashboard corrections made via `update_entry`.

JSONL backup is not modified (append-only). The dashboard reads SQLite, so
this backfill is sufficient for Timeline / Events to reflect the new rule.

Run once after deploying the smoothing change, or any time
STATE_CONFIRM_WINDOW / STATE_CONFIRM_RUN are adjusted.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib.config import DATA_DIR, STATE_CONFIRM_WINDOW
from lib.state import smooth_state_temporal


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DATA_DIR / "monitor.db"),
                    help="Path to monitor.db (defaults to data/monitor.db)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute changes without writing")
    ap.add_argument("--verbose", action="store_true",
                    help="Print every changed entry (not just the summary)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT timestamp, state, data FROM entries ORDER BY timestamp ASC"
    ).fetchall()
    print(f"scanned {len(rows)} entries from {args.db}")

    # Rolling window of already-smoothed entries. We feed smoothed `state`
    # back into the window so carry-forward behaves identically to the live
    # path (which reads history from DB where `state` is already smoothed).
    window: list[dict] = []
    updates: list[tuple[str, str, str]] = []  # (timestamp, smoothed_state, data_json)
    changed_by_direction: dict[str, int] = {}

    for row in rows:
        entry = json.loads(row["data"])
        stored_state = row["state"]

        # rawState preserves the per-frame reading. If we've already written
        # a rawState from a prior backfill, keep it — the original raw
        # state is immutable. Otherwise seed it from the current stored
        # state (which is the pre-smoothing per-frame value for entries
        # that predate the smoothing change).
        raw_state = entry.get("rawState") or stored_state
        entry["rawState"] = raw_state

        window_slice = window[-(STATE_CONFIRM_WINDOW - 1):]
        smoothed = smooth_state_temporal(entry, window_slice)
        entry["state"] = smoothed

        if smoothed != stored_state:
            direction = f"{stored_state} -> {smoothed}"
            changed_by_direction[direction] = changed_by_direction.get(direction, 0) + 1
            if args.verbose:
                print(f"  {row['timestamp']}  raw={raw_state}  {direction}")
            updates.append((entry["timestamp"], smoothed, json.dumps(entry)))
        elif entry.get("rawState") != stored_state and not entry.get("rawState"):
            # Shouldn't happen — rawState was just seeded — but be safe.
            updates.append((entry["timestamp"], smoothed, json.dumps(entry)))

        # Append the post-smoothing entry to the window so downstream frames
        # see the same history the live pipeline would have seen.
        window.append(entry)

    print(f"\nchanges: {len(updates)} entries")
    for direction, count in sorted(changed_by_direction.items(), key=lambda x: -x[1]):
        print(f"  {direction:40s} {count}")

    if args.dry_run:
        print("\ndry-run: no writes")
        return 0

    if not updates:
        print("\nnothing to write")
        return 0

    print(f"\nwriting {len(updates)} updates ...")
    conn.executemany(
        "UPDATE entries SET state = ?, data = ? WHERE timestamp = ?",
        [(state, data, ts) for (ts, state, data) in updates],
    )
    conn.commit()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
