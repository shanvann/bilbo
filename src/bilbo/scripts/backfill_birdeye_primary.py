#!/usr/bin/env python3
"""Re-run BIRDEYE on historical frames and write results into primary fields.

Different from `monitor.py --backfill-shadow`:
    --backfill-shadow writes BIRDEYE output only into the `shadow` audit
    dict and the `shadow_birdeye_*` indexed columns, leaving the primary
    `eyeState` / `faceBbox` / `presenceConfidence` / `eyeConfidence`
    fields untouched. That's correct for audit — the primary fields are
    the pipeline's historical record of what actually ran live and may
    have been user-corrected.

This script is for the opposite case: you've deployed a new eye-state
model and you want the primary fields (the ones the dashboard Timeline
and the temporal state smoother read) repopulated with the new model's
predictions, retroactively, for a specific time window.

Safety:
    - Skips entries with `eye_state_edited = 1` so user corrections are
      preserved as ground truth.
    - Skips entries where the baby is not present (nothing to classify).
    - Does NOT touch `detection_method`, `state`, `modelUsed` — these
      describe what actually ran live and should stay immutable. Only
      the classifier outputs are refreshed.
    - Also refreshes the `shadow` dict and `shadow_birdeye_*` columns
      (via db.update_entry's indexed-column derivation) so the audit
      trail stays in sync with the primary fields.

After running this, re-run `scripts/backfill_state.py` to re-smooth the
derived `state` field over the new eye-state signal.
"""

import argparse
import os
import sys
import time as _time
from pathlib import Path

from bilbo.config import MODELS_DIR
from bilbo.storage.db import get_db
import bilbo.pipeline.local_pipeline as _lp
from bilbo.pipeline.local_pipeline import run_birdeye_inference


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True,
                    help="ISO start timestamp (e.g. 2026-04-02T00:00:00Z)")
    ap.add_argument("--end", default=None,
                    help="ISO end timestamp (defaults to now)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run inference but don't persist — reports what would change")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of entries processed")
    ap.add_argument("--include-corrected", action="store_true",
                    help="Also overwrite entries with eye_state_edited=1 "
                         "(destroys user corrections — opt-in)")
    args = ap.parse_args()

    latest_link = MODELS_DIR / "latest"
    if not latest_link.is_symlink():
        print(f"No deployed model — {latest_link} is not a symlink", file=sys.stderr)
        return 1
    deployed_version = Path(os.readlink(latest_link)).name
    print(f"deployed model: {deployed_version}")

    # Force classifier reload so we pick up the current weights on disk.
    _lp._presence_clf = None
    _lp._eye_state_clf = None
    _lp._face_detector = None
    _lp._face_detector_fallback = None
    _lp._available = None

    db = get_db()
    entries = db.get_entries(start=args.start, end=args.end)
    print(f"window: {args.start} → {args.end or 'now'} — {len(entries)} entries")

    # Pre-filter: baby present + not corrected.
    filtered = []
    skipped_absent = 0
    skipped_edited = 0
    for e in entries:
        if not e.get("babyPresent"):
            skipped_absent += 1
            continue
        if e.get("eyeStateEdited") and not args.include_corrected:
            skipped_edited += 1
            continue
        filtered.append(e)
    print(f"  skipped {skipped_absent} not-present, {skipped_edited} corrected")
    print(f"  {len(filtered)} candidates")

    if args.limit:
        filtered = filtered[:args.limit]
        print(f"  --limit: capped to {len(filtered)}")

    n_total = len(filtered)
    n_processed = 0
    n_missing_frame = 0
    n_hard_error = 0
    n_updated = 0
    n_eye_changed = 0
    n_no_eye = 0  # BIRDEYE bailed (no_face_detected / low_confidence)
    t0 = _time.monotonic()

    for i, entry in enumerate(filtered, 1):
        ts = entry.get("timestamp")
        frame = entry.get("frame")
        if not frame or not Path(frame).exists():
            n_missing_frame += 1
            continue

        try:
            result = run_birdeye_inference(Path(frame))
        except Exception as exc:  # noqa: BLE001
            print(f"  [{ts}] inference error: {exc}", file=sys.stderr)
            n_hard_error += 1
            continue

        if result is None:
            n_hard_error += 1
            continue

        n_processed += 1

        new_eye = result.get("eyeState")
        old_eye = entry.get("eyeState")
        if new_eye != old_eye:
            n_eye_changed += 1

        if new_eye is None:
            # BIRDEYE couldn't classify the eyes (no face / low conf).
            # Leave eyeState alone but still refresh face detection
            # metadata and the shadow audit.
            n_no_eye += 1

        # Primary field updates. Only overwrite eyeState when BIRDEYE
        # actually produced one — don't clobber a good historical value
        # with None just because this run bailed on the face stage.
        updates: dict = {}
        if new_eye is not None:
            updates["eyeState"] = new_eye
        if result.get("eyeConfidence") is not None:
            updates["eyeConfidence"] = result["eyeConfidence"]
        if result.get("presenceConfidence") is not None:
            updates["presenceConfidence"] = result["presenceConfidence"]
        if result.get("faceBbox") is not None:
            updates["faceBbox"] = result["faceBbox"]
        if result.get("faceConfidence") is not None:
            updates["faceConfidence"] = result["faceConfidence"]

        # Refresh the shadow audit dict (same shape cmd_backfill_shadow
        # writes) so the indexed shadow_birdeye_* columns stay in sync.
        birdeye_state = result.get("state", "Unknown")
        if not result.get("babyPresent", False):
            birdeye_state = "not_present"
        prod_state = entry.get("state", "Unknown")
        if not entry.get("babyPresent", False):
            prod_state = "not_present"
        updates["shadow"] = {
            "birdeyeState": birdeye_state,
            "prodState": prod_state,
            "agreed": birdeye_state.lower() == prod_state.lower(),
            "presenceConfidence": result.get("presenceConfidence"),
            "eyeConfidence": result.get("eyeConfidence"),
            "eyeState": result.get("eyeState"),
            "birdeyeTimings": result.get("birdeyeTimings"),
            "fallback": result.get("fallback"),
        }
        updates["shadowModelVersion"] = deployed_version

        if not args.dry_run:
            db.update_entry(ts, updates)
        n_updated += 1

        if i % 100 == 0 or i == n_total:
            elapsed = _time.monotonic() - t0
            rate = n_processed / elapsed if elapsed else 0
            eta = (n_total - i) / rate if rate else 0
            print(
                f"  [{i}/{n_total}] processed={n_processed} updated={n_updated} "
                f"eye_changed={n_eye_changed} no_eye={n_no_eye} "
                f"missing={n_missing_frame} errors={n_hard_error} "
                f"rate={rate:.1f}/s eta={eta:.0f}s",
                flush=True,
            )

    elapsed = _time.monotonic() - t0
    print(
        f"\ndone in {elapsed:.1f}s — total={n_total} processed={n_processed} "
        f"updated={n_updated} eye_changed={n_eye_changed} no_eye={n_no_eye} "
        f"missing_frame={n_missing_frame} hard_error={n_hard_error}"
        + (" (dry-run)" if args.dry_run else "")
    )

    if not args.dry_run and n_updated:
        print("\nNext step: re-run `scripts/backfill_state.py` to re-smooth "
              "the primary state field over the refreshed eyeState signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
