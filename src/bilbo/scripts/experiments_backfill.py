#!/usr/bin/env python3
"""Backfill registered shadow experiments against historical frames.

The production monitor.py calls ``experiments.run_all()`` on every new
capture, but when a fresh experiment is registered (or a retrained
checkpoint drops in) there's no way to see its metrics until enough
new frames have accumulated. This script runs the currently-registered
experiments against historical frames and stores the results in each
entry's ``data.experiments`` field, so the dashboard has immediate
comparison data instead of waiting a week.

Each experiment's own ``run()`` decides whether it's applicable to a
given frame — None is returned for frames the experiment can't score,
same as the live path. Exceptions are logged and skipped. Nothing the
experiment does is allowed to mutate the entry's primary fields.

Usage:
    python scripts/experiments_backfill.py
        Run every registered experiment on every frame that has a
        faceBbox and isn't already cached against the current model
        version.

    python scripts/experiments_backfill.py --name eye_state_hires_448_retrained
        Run a single experiment by name.

    python scripts/experiments_backfill.py --since 2026-04-10
        Only frames captured at/after this ISO date.

    python scripts/experiments_backfill.py --hours 168
        Shorthand for "only frames from the last N hours".

    python scripts/experiments_backfill.py --limit 50 --verbose
        Cap frame count and print per-frame progress — useful for
        iterating on a new experiment.

    python scripts/experiments_backfill.py --force
        Recompute even for frames that already have a cached result
        against the current model version. Use after a code change
        to the experiment's run() method.

    python scripts/experiments_backfill.py --dry-run
        Compute but don't persist.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bilbo.storage.db import get_db
from bilbo.experiments import get_registry, get_experiment, run_all as _run_all


def _resolve_frame_path(raw: str) -> Path | None:
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return p
    here = Path(__file__).resolve().parent.parent
    candidate = (here / raw).resolve()
    return candidate if candidate.exists() else None


def _should_process(entry: dict, experiment_name: str, *, force: bool) -> bool:
    """Return True if this frame needs the experiment run against it.

    Skips frames that already have a cached result with a matching model
    version — re-running would be wasted work. ``--force`` bypasses this.
    """
    if force:
        return True
    experiments = entry.get("experiments") or {}
    cached = experiments.get(experiment_name)
    if not isinstance(cached, dict):
        return True
    exp = get_experiment(experiment_name)
    if exp is None:
        return False
    # Compare cached model version against what the experiment currently
    # reports. If the experiment doesn't expose its version, default to
    # "recompute" — cheap relative to a stale metric.
    cached_version = cached.get("modelVersion")
    if cached_version is None:
        return True
    # Ask the experiment what version it would report now — the class
    # may have a `_model_version` attribute after lazy-init. If we can't
    # tell, recompute (safer than showing stale numbers).
    current_version = getattr(exp, "_model_version", None)
    if current_version is None:
        return True
    return cached_version != current_version


def find_candidates(
    db,
    *,
    experiment_name: str | None,
    since: str | None,
    hours: float | None,
    force: bool,
) -> list[dict]:
    """Select entries eligible for experiment backfill.

    Eligibility rules match the live path: the entry must have a face
    bbox (so the experiment's standard-shape ``run()`` has something to
    work on), and must not be an obvious skip. The experiment's own
    ``run()`` is the final authority on applicability and may still
    return None.
    """
    entries = db.get_entries()

    if hours is not None and since is None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        since = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates: list[dict] = []
    for entry in entries:
        ts = entry.get("timestamp", "")
        if since and ts < since:
            continue
        if not entry.get("faceBbox"):
            continue
        # When a single experiment is selected, pre-filter by per-entry
        # cache to keep verbose output meaningful. When running all
        # experiments, process every eligible frame and let each
        # experiment decide on its own.
        if experiment_name is not None:
            if not _should_process(entry, experiment_name, force=force):
                continue
        candidates.append(entry)
    return candidates


def main():
    ap = argparse.ArgumentParser(
        description="Run shadow experiments against historical frames",
    )
    ap.add_argument(
        "--name", default=None,
        help="Run a single experiment by registry name. Omit to run all "
             "registered experiments.",
    )
    ap.add_argument(
        "--since", default=None,
        help="ISO-8601 cutoff timestamp (frames at or after this time)",
    )
    ap.add_argument(
        "--hours", type=float, default=None,
        help="Shorthand for --since: last N hours",
    )
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap total frames processed")
    ap.add_argument("--force", action="store_true",
                    help="Recompute even if cached against the current model version")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute but do not persist to the database")
    ap.add_argument("--verbose", action="store_true",
                    help="Print per-frame progress to stderr")
    args = ap.parse_args()

    # Validate the --name filter before loading data or lazy-initing
    # classifiers.
    registry = get_registry()
    if not registry:
        print(json.dumps({"ok": False, "error": "no experiments registered"}))
        return 1
    if args.name is not None:
        if get_experiment(args.name) is None:
            known = [exp.name for exp in registry]
            print(json.dumps({
                "ok": False,
                "error": f"unknown experiment {args.name!r}",
                "registered": known,
            }))
            return 1
        filter_names = [args.name]
    else:
        filter_names = None

    db = get_db()
    candidates = find_candidates(
        db,
        experiment_name=args.name,
        since=args.since,
        hours=args.hours,
        force=args.force,
    )
    if args.limit:
        candidates = candidates[: args.limit]

    print(
        f"experiments_backfill: experiments="
        f"{filter_names or [e.name for e in registry]} candidates={len(candidates)}",
        file=sys.stderr,
    )

    processed = 0
    skipped_no_result = 0
    per_experiment_counts: dict[str, int] = {}
    t0 = time.monotonic()

    for i, entry in enumerate(candidates):
        frame_path = _resolve_frame_path(entry.get("frame", ""))
        if frame_path is None:
            if args.verbose:
                print(
                    f"  [{i+1}/{len(candidates)}] SKIP {entry.get('timestamp', '?')} "
                    f"(frame file missing)",
                    file=sys.stderr,
                )
            continue

        results = _run_all(
            frame_path,
            entry,
            prod_result=None,  # we only have prod's stored output, not its original result dict
            names=filter_names,
        )
        if not results:
            skipped_no_result += 1
            if args.verbose:
                print(
                    f"  [{i+1}/{len(candidates)}] {entry['timestamp']} "
                    f"no experiments applicable (model missing or bbox absent)",
                    file=sys.stderr,
                )
            continue

        processed += 1
        for name in results:
            per_experiment_counts[name] = per_experiment_counts.get(name, 0) + 1

        if not args.dry_run:
            # Merge into the entry's existing experiments dict — preserve
            # any other experiments that ran on this frame previously.
            existing = entry.get("experiments") or {}
            if not isinstance(existing, dict):
                existing = {}
            existing.update(results)
            db.update_entry(entry["timestamp"], {"experiments": existing})

        if args.verbose:
            summary = ", ".join(
                f"{name}={r.get('eyeState', '?')}/{round(r.get('eyeConfidence', 0) or 0, 2)}"
                for name, r in results.items()
            )
            print(
                f"  [{i+1}/{len(candidates)}] {entry['timestamp']} {summary}",
                file=sys.stderr,
            )

    elapsed = time.monotonic() - t0
    print(json.dumps({
        "ok": True,
        "processed": processed,
        "noResult": skipped_no_result,
        "perExperiment": per_experiment_counts,
        "elapsedSeconds": round(elapsed, 2),
        "dryRun": bool(args.dry_run),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
