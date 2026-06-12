#!/usr/bin/env python3
"""Backfill the smoothed `state` field across existing entries.

Walks the Postgres `entries` table in timestamp order, applies `smooth_state_temporal`
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
import sys

from bilbo.config import (
    FALLING_ASLEEP_MAX_MINUTES,
    STATE_CONFIRM_WINDOW,
    UNKNOWN_ABSORB_MAX_MINUTES,
)
from bilbo.state import _parse_ts, smooth_state_temporal


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute changes without writing")
    ap.add_argument("--verbose", action="store_true",
                    help="Print every changed entry (not just the summary)")
    args = ap.parse_args()

    # Connection comes from DATABASE_URL (Postgres) via the shared helper.
    from bilbo.storage.db import get_connection, get_db
    get_db()  # ensure tables exist
    conn = get_connection()

    rows = conn.execute(
        "SELECT timestamp, state, data FROM entries ORDER BY timestamp ASC"
    ).fetchall()
    print(f"scanned {len(rows)} entries from Postgres")

    # --- Pass 1: smooth every entry ---
    # Rolling window of already-smoothed entries. We feed smoothed `state`
    # back into the window so carry-forward behaves identically to the live
    # path (which reads history from DB where `state` is already smoothed).
    window: list[dict] = []
    all_entries: list[tuple[str, dict, str]] = []  # (timestamp, entry, stored_state)

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

        window.append(entry)
        all_entries.append((row["timestamp"], entry, stored_state))

    # --- Pass 2: Unknown → Awake absorption ---
    # Walk forward, accumulate contiguous Unknown+babyPresent runs, and when
    # we hit a terminating Awake, check whether the run's span is within
    # UNKNOWN_ABSORB_MAX_MINUTES. If so, flip each Unknown in the run to
    # Awake in-place (mutating the entry dict in all_entries).
    n_absorbed = 0
    pending_run: list[tuple[str, dict]] = []
    for ts, entry, _ in all_entries:
        smoothed = entry["state"]
        is_unk_present = (smoothed == "Unknown" and entry.get("babyPresent"))

        if is_unk_present:
            pending_run.append((ts, entry))
            continue

        if smoothed == "Awake" and pending_run:
            first_ts = _parse_ts(pending_run[0][0])
            curr_ts = _parse_ts(ts)
            if first_ts and curr_ts:
                span_min = (curr_ts - first_ts).total_seconds() / 60.0
                if span_min < UNKNOWN_ABSORB_MAX_MINUTES:
                    for _u_ts, u_entry in pending_run:
                        u_entry["state"] = "Awake"
                        n_absorbed += 1
        pending_run = []

    # --- Pass 3: Putdown-pattern absorption ---
    # Walk forward. When we hit a terminating Asleep AND the preceding
    # contiguous Unknown+babyPresent run is bookended by a not_present
    # frame, rewrite the run to FallingAsleep (≤30m) or Awake (>30m).
    # This mirrors lib.state.putdown_prefix_to_absorb but runs over the
    # whole DB in one forward pass instead of re-walking from each Asleep.
    #
    # Ordering vs Pass 2: if the same Unknown run is also eligible for
    # Unknown → Awake absorption (because a later Awake also satisfies the
    # short-run rule), Pass 2 wins because it runs first and flips the
    # entries to Awake — by the time we walk the run here, there are no
    # Unknowns left to reclassify. That's the desired behaviour: a
    # contiguous run terminated by BOTH a nearby Awake AND a later Asleep
    # is really just a wake-up briefly preceding sleep, and the live path
    # would also see the Awake first.
    n_putdown_falling = 0
    n_putdown_awake = 0
    pending_run = []
    pre_run_was_not_present = False  # is the frame immediately before the run not_present?
    prev_was_not_present = True  # treat start-of-history as not_present-safe
    for ts, entry, _ in all_entries:
        smoothed = entry["state"]
        is_unk_present = (smoothed == "Unknown" and entry.get("babyPresent"))
        is_not_present = not entry.get("babyPresent")

        if is_unk_present:
            if not pending_run:
                # Starting a new run — remember whether the frame right
                # before the run was not_present.
                pre_run_was_not_present = prev_was_not_present
            pending_run.append((ts, entry))
            prev_was_not_present = False
            continue

        if smoothed == "Asleep" and pending_run and pre_run_was_not_present:
            first_ts = _parse_ts(pending_run[0][0])
            curr_ts = _parse_ts(ts)
            if first_ts and curr_ts:
                span_min = (curr_ts - first_ts).total_seconds() / 60.0
                new_state = "FallingAsleep" if span_min <= FALLING_ASLEEP_MAX_MINUTES else "Awake"
                for _u_ts, u_entry in pending_run:
                    u_entry["state"] = new_state
                    if new_state == "FallingAsleep":
                        n_putdown_falling += 1
                    else:
                        n_putdown_awake += 1

        pending_run = []
        pre_run_was_not_present = False
        prev_was_not_present = is_not_present

    # --- Pass 4: compare against stored_state and build update list ---
    updates: list[tuple[str, str, str]] = []
    changed_by_direction: dict[str, int] = {}
    for ts, entry, stored_state in all_entries:
        smoothed = entry["state"]
        if smoothed != stored_state:
            direction = f"{stored_state} -> {smoothed}"
            changed_by_direction[direction] = changed_by_direction.get(direction, 0) + 1
            if args.verbose:
                print(f"  {ts}  raw={entry.get('rawState')}  {direction}")
            updates.append((ts, smoothed, json.dumps(entry)))

    print(
        f"\nchanges: {len(updates)} entries "
        f"(including {n_absorbed} absorbed Unknown→Awake, "
        f"{n_putdown_falling} Unknown→FallingAsleep, "
        f"{n_putdown_awake} Unknown→Awake from putdown pattern)"
    )
    for direction, count in sorted(changed_by_direction.items(), key=lambda x: -x[1]):
        print(f"  {direction:40s} {count}")

    if args.dry_run:
        print("\ndry-run: no writes")
        return 0

    if not updates:
        print("\nnothing to write")
        return 0

    print(f"\nwriting {len(updates)} updates ...")
    # One explicit transaction (autocommit conn) for atomicity + speed.
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "UPDATE entries SET state = %s, data = %s WHERE timestamp = %s",
            [(state, data, ts) for (ts, state, data) in updates],
        )
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
