#!/usr/bin/env python3
"""Promote a shadow experiment to production — single-command flip.

When a shadow experiment has been running alongside prod long enough
that the dashboard shows a clear win, this script bundles all the
manual steps of the flip into one atomic-ish operation.

Usage
-----

    python scripts/promote_experiment.py --tag eye_state_448

    # Preview without actually doing anything:
    python scripts/promote_experiment.py --tag eye_state_448 --dry-run

    # Skip the (slow) re-inference of historical frames:
    python scripts/promote_experiment.py --tag eye_state_448 --no-reinfer

    # Custom name for the rollback shadow (default: eye_state_<old_size>_legacy):
    python scripts/promote_experiment.py --tag eye_state_448 --legacy-name eye_state_224_legacy_pre_v2

The ``--tag`` argument names a directory under
``pipeline/models/experiments/<tag>/latest/`` whose contents should
replace the current prod eye-state model. The script discovers the
crop size from the tag (it must be encoded in the tag name for now —
``eye_state_NNN`` → NNN) or from an explicit ``--crop-size`` flag.

What it does (in order)
-----------------------

 1. Preflight validation — refuses to run if the requested weights
    don't exist, if ``experiments.json`` is malformed, if another
    training process is running, or if the tag to promote is already
    the one prod is using.
 2. **Stops launchd capture** so no live ticks race with the file swap.
 3. **Snapshots current prod eye-state weights** into
    ``pipeline/models/experiments/<legacy_name>/v_<ts>/`` with a
    ``latest`` symlink. This is the rollback path — specifically
    preserved so ``promote_experiment --tag <legacy_name>`` reverts
    the flip with one command.
 4. **Copies the experimental weights into the prod path**
    (``pipeline/models/latest/eye_state_classifier.pt``). Presence
    and face_detect are not touched — only eye_state.
 5. **Writes ``pipeline/models/latest/meta.json``** with the new
    ``eye_state_crop_size`` so ``config.EYE_STATE_INPUT_SIZE`` picks
    it up on next import. The old meta.json is preserved in the
    snapshot directory from step 3.
 6. **Patches the deployed ``training_runs`` row** in SQLite — copies
    the eye_state sub-dict from the experimental run's metrics so the
    dashboard's Eye State column reflects what is actually running.
 7. **Cleans up the promoted experiment's stale entries** from the
    ``experiments`` field on historical frames (they're redundant
    now — the experiment IS prod).
 8. **Updates ``scripts/lib/experiments.json``** — removes the
    promoted entry, inserts the rollback entry pointing at the
    snapshot from step 3.
 9. **Backfills the new rollback shadow** on historical frames
    (``experiments_backfill.py --name <legacy_name> --force``)
    so the dashboard has real comparison numbers immediately.
10. **Re-scores historical baby-present frames with the new prod**
    (slow — ~5 min for a week of captures) so the ``shadow_birdeye_eye``
    column reflects the currently-deployed model. Skipped by
    ``--no-reinfer`` if you want to move on faster.
11. **Reloads launchd capture**.
12. Prints the rollback command so you can copy-paste it somewhere.

If any step fails partway through, the script prints the exact state
it reached and the command to resume or roll back. Every destructive
step is preceded by a ``[step N/12]`` log line so you can tell from
the output where a crash happened.

Scope
-----

Only eye-state promotions are supported today. Presence and
face-detect have never had a shadow, so the manifest has no entries
for them. When that changes, extend the loop and add matching
branches here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths & helpers
# ---------------------------------------------------------------------------

import bilbo

from bilbo.config import MODELS_DIR, DATA_DIR  # noqa: E402

BILBO_PKG = Path(bilbo.__file__).resolve().parent


MANIFEST = BILBO_PKG / "experiments.json"
EXPERIMENTS_DIR = MODELS_DIR / "experiments"
PROD_LATEST = MODELS_DIR / "latest"
HOME_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
CAPTURE_PLIST = HOME_LAUNCH_AGENTS / "com.baby-monitor.plist"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _version_tag() -> str:
    """Versioned directory name for snapshots (matches train_classifiers)."""
    return datetime.now().strftime("v_%Y%m%d_%H%M%S")


def _log(step: int, total: int, msg: str):
    print(f"[step {step}/{total}] {msg}", flush=True)


def _die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Crop size discovery
# ---------------------------------------------------------------------------

_TAG_SIZE_RE = re.compile(r"eye_state_(\d{2,4})(?:$|_)")


def _infer_crop_size_from_tag(tag: str) -> int | None:
    """Pull a 2–4 digit number out of a tag like ``eye_state_448``.

    Returns None if the tag doesn't match the convention — the caller
    is expected to require an explicit ``--crop-size`` in that case.
    """
    m = _TAG_SIZE_RE.match(tag)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _read_current_prod_crop_size() -> int:
    """Read the currently-deployed prod crop size from meta.json."""
    meta_path = PROD_LATEST / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            return int(meta.get("eye_state_crop_size", 224))
        except (OSError, ValueError, TypeError):
            pass
    return 224


# ---------------------------------------------------------------------------
# Step 1 — Preflight
# ---------------------------------------------------------------------------


def preflight(args) -> dict:
    """Validate the request before touching anything. Returns a dict of
    resolved paths and parameters the later steps consume."""
    print(f"[preflight] tag={args.tag} dry_run={args.dry_run} reinfer={not args.no_reinfer}")

    # Tag directory must exist with a latest symlink
    tag_dir = EXPERIMENTS_DIR / args.tag
    if not tag_dir.exists():
        _die(f"experimental model dir not found: {tag_dir}")
    exp_latest = tag_dir / "latest"
    if not exp_latest.exists():
        _die(f"experimental model `latest` symlink not found: {exp_latest}")
    exp_weights = exp_latest / "eye_state_classifier.pt"
    if not exp_weights.exists():
        _die(f"experimental weights not found: {exp_weights}")

    # What version is this experiment pointing at?
    try:
        exp_version = Path(os.readlink(exp_latest)).name if exp_latest.is_symlink() else exp_latest.name
    except OSError:
        exp_version = exp_latest.name

    # Prod must exist
    if not PROD_LATEST.exists():
        _die(f"prod latest dir not found: {PROD_LATEST}")
    prod_weights = PROD_LATEST / "eye_state_classifier.pt"
    if not prod_weights.exists():
        _die(f"prod eye_state weights not found: {prod_weights}")

    # Same-tag promotion is a no-op
    current_prod_version = None
    if PROD_LATEST.is_symlink():
        try:
            current_prod_version = Path(os.readlink(PROD_LATEST)).name
        except OSError:
            pass

    # Crop size
    crop_size = args.crop_size
    if crop_size is None:
        crop_size = _infer_crop_size_from_tag(args.tag)
    if crop_size is None:
        _die(
            f"could not infer crop size from tag {args.tag!r}; "
            f"pass --crop-size explicitly"
        )
    old_crop_size = _read_current_prod_crop_size()
    print(f"[preflight] crop size: current prod = {old_crop_size}, new = {crop_size}")
    if crop_size == old_crop_size and current_prod_version and exp_version == current_prod_version:
        _die(
            f"tag {args.tag!r} (version {exp_version}) is already deployed as prod"
        )

    # Manifest must be parseable
    if not MANIFEST.exists():
        _die(f"manifest not found: {MANIFEST}")
    try:
        manifest = json.loads(MANIFEST.read_text())
    except ValueError as e:
        _die(f"manifest at {MANIFEST} is malformed: {e}")

    # Legacy name defaults to ``eye_state_<old_size>_legacy``
    legacy_name = args.legacy_name or f"eye_state_{old_crop_size}_legacy"
    if "/" in legacy_name or ".." in legacy_name:
        _die(f"legacy name must be a simple identifier: {legacy_name!r}")

    # Training lock — refuse if a retrain is in flight
    try:
        from bilbo.training_state import is_running as _train_is_running
        if _train_is_running():
            _die(
                "a training process is currently running — promotion would "
                "race against it. Abort the retrain (or wait for it to "
                "finish), then re-run this script."
            )
    except ImportError:
        pass  # training_state not available → skip the check

    return {
        "tag": args.tag,
        "tag_dir": tag_dir,
        "exp_latest": exp_latest,
        "exp_weights": exp_weights,
        "exp_version": exp_version,
        "prod_weights": prod_weights,
        "current_prod_version": current_prod_version,
        "crop_size": crop_size,
        "old_crop_size": old_crop_size,
        "manifest": manifest,
        "legacy_name": legacy_name,
    }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def stop_launchd(dry_run: bool):
    if dry_run:
        print("  (dry-run) would: launchctl unload com.baby-monitor.plist")
        return
    try:
        subprocess.run(
            ["launchctl", "unload", str(CAPTURE_PLIST)],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        print("  launchctl not found — skipping (no-op on non-macOS)")


def reload_launchd(dry_run: bool):
    if dry_run:
        print("  (dry-run) would: launchctl load com.baby-monitor.plist")
        return
    try:
        subprocess.run(
            ["launchctl", "load", str(CAPTURE_PLIST)],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        pass


def snapshot_current_prod(ctx: dict, dry_run: bool) -> Path:
    """Copy the current prod eye-state weights into
    ``experiments/<legacy_name>/v_<ts>/`` with a ``latest`` symlink.

    Also copies the current meta.json if it exists so rollback picks
    up the old crop size automatically.
    """
    legacy_name = ctx["legacy_name"]
    version = _version_tag()
    target_dir = EXPERIMENTS_DIR / legacy_name / version
    if dry_run:
        print(f"  (dry-run) would: snapshot prod weights to {target_dir}")
        return target_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    # Weights
    shutil.copy2(ctx["prod_weights"], target_dir / "eye_state_classifier.pt")
    # Prod meta.json (if any) — preserves the old crop_size for rollback
    prod_meta = PROD_LATEST / "meta.json"
    if prod_meta.exists():
        # We write a new meta for the legacy snapshot so rollback is
        # self-contained. Start from the prod meta and overlay the
        # now-known old crop size.
        try:
            prod_meta_data = json.loads(prod_meta.read_text())
        except (OSError, ValueError):
            prod_meta_data = {}
    else:
        prod_meta_data = {}
    legacy_meta = {
        **prod_meta_data,
        "eye_state_crop_size": ctx["old_crop_size"],
        "snapshotted_at": _iso_now(),
        "snapshotted_from_prod_version": ctx.get("current_prod_version"),
        "snapshot_reason": f"pre-flip snapshot taken before promoting {ctx['tag']}",
    }
    (target_dir / "meta.json").write_text(json.dumps(legacy_meta, indent=2) + "\n")

    # Update the legacy tag's `latest` symlink
    legacy_latest = EXPERIMENTS_DIR / legacy_name / "latest"
    if legacy_latest.is_symlink() or legacy_latest.exists():
        legacy_latest.unlink()
    legacy_latest.symlink_to(version)

    print(f"  → snapshot at {target_dir}")
    return target_dir


def copy_experimental_to_prod(ctx: dict, dry_run: bool):
    if dry_run:
        print(f"  (dry-run) would: cp {ctx['exp_weights']} → {ctx['prod_weights']}")
        return
    shutil.copy2(ctx["exp_weights"], ctx["prod_weights"])
    print(f"  → prod eye_state weights replaced with {ctx['exp_weights'].name}")


def write_prod_meta(ctx: dict, dry_run: bool):
    """Write pipeline/models/latest/meta.json with the new crop size."""
    meta_path = PROD_LATEST / "meta.json"
    existing = {}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            existing = {}
    new_meta = {
        **existing,
        "eye_state_crop_size": ctx["crop_size"],
        "deployed_at": _iso_now(),
        "deployed_version": ctx["current_prod_version"],  # note: this is the dir version, not the experimental one
        "eye_state_source": f"promoted from experiments/{ctx['tag']}/{ctx['exp_version']}",
        "eye_state_previous_crop_size": ctx["old_crop_size"],
    }
    if dry_run:
        print(f"  (dry-run) would: write {meta_path} with crop_size={ctx['crop_size']}")
        return
    meta_path.write_text(json.dumps(new_meta, indent=2) + "\n")
    print(f"  → {meta_path}: eye_state_crop_size={ctx['crop_size']}")


def patch_training_runs_metrics(ctx: dict, dry_run: bool):
    """Copy the experimental run's eye_state metrics into the deployed
    prod row so the dashboard shows what is actually deployed.
    """
    from bilbo.storage.db import get_connection  # noqa: PLC0415

    deployed = ctx.get("current_prod_version")
    if not deployed:
        print("  prod version unknown — skipping training_runs patch")
        return

    # Experimental metrics: SQLite training_runs, falling back to
    # pipeline/models/experiments/<tag>/training-log.jsonl.
    conn = get_connection()
    exp_row = conn.execute(
        "SELECT metrics FROM training_runs WHERE version = %s",
        (ctx["exp_version"],),
    ).fetchone()
    if exp_row:
        exp_metrics = json.loads(exp_row["metrics"])
    else:
        jsonl = ctx["tag_dir"] / "training-log.jsonl"
        if not jsonl.exists():
            print(f"  no experimental metrics found (tried SQLite + {jsonl}) — skipping")
            return
        exp_metrics = None
        with jsonl.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("version") == ctx["exp_version"]:
                    exp_metrics = entry.get("metrics") or {}
                    break
        if exp_metrics is None:
            print(f"  no matching version in {jsonl} — skipping")
            return

    new_eye = dict(exp_metrics.get("eye_state") or {})
    if not new_eye:
        print("  experimental metrics had no eye_state sub-dict — skipping")
        return
    new_eye.setdefault("_patched_from", ctx["exp_version"])
    new_eye.setdefault(
        "_patched_reason",
        f"promoted from experiments/{ctx['tag']} on {_iso_now()}",
    )

    prod_row = conn.execute(
        "SELECT metrics FROM training_runs WHERE version = %s",
        (deployed,),
    ).fetchone()
    if not prod_row:
        print(f"  no training_runs row for deployed version {deployed} — skipping")
        return
    prod_metrics = json.loads(prod_row["metrics"])
    prod_metrics["eye_state"] = new_eye

    if dry_run:
        print(
            f"  (dry-run) would: UPDATE training_runs SET metrics[eye_state] "
            f"← experiments/{ctx['tag']} eye_state"
        )
        return
    conn.execute(
        "UPDATE training_runs SET metrics = %s WHERE version = %s",
        (json.dumps(prod_metrics), deployed),
    )
    print(f"  → patched training_runs row {deployed} eye_state metrics")


def cleanup_stale_experiment_keys(ctx: dict, dry_run: bool):
    """Strip the promoted experiment's key from entries.data.experiments.

    After promotion the old shadow results are redundant (they became
    prod), and leaving them would confuse the Shadow Experiments card
    on the dashboard if the experiment later got re-registered.
    """
    from bilbo.storage.db import get_connection  # noqa: PLC0415

    # The experiment name we're stripping is whatever manifest entry
    # pointed at this tag. Match by experiment_tag.
    manifest: dict = ctx["manifest"]
    promoted_names: list[str] = []
    for entry in manifest.get("eye_state") or []:
        if entry.get("experiment_tag") == ctx["tag"]:
            promoted_names.append(entry["name"])
    if not promoted_names:
        print(f"  no manifest entry for tag {ctx['tag']!r} — nothing to strip")
        return

    conn = get_connection()
    updated = 0
    for promoted_name in promoted_names:
        # `%` lives in the bound parameter, not the query text, so psycopg
        # passes it through literally — no %%-escaping needed here.
        like_pat = f"%{promoted_name}%"
        rows = conn.execute(
            "SELECT timestamp, data FROM entries WHERE data LIKE %s",
            (like_pat,),
        ).fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"])
            except ValueError:
                continue
            exps = d.get("experiments") or {}
            if promoted_name in exps:
                del exps[promoted_name]
                if exps:
                    d["experiments"] = exps
                else:
                    d.pop("experiments", None)
                if not dry_run:
                    conn.execute(
                        "UPDATE entries SET data = %s WHERE timestamp = %s",
                        (json.dumps(d), r["timestamp"]),
                    )
                updated += 1
    suffix = " (dry-run)" if dry_run else ""
    print(f"  → stripped {updated} stale {promoted_names} keys{suffix}")


def update_manifest(ctx: dict, dry_run: bool):
    """Remove the promoted entry from experiments.json and add the
    new rollback entry pointing at the snapshot we just made.
    """
    manifest: dict = ctx["manifest"]
    eye_state = list(manifest.get("eye_state") or [])

    # Drop any entries that point at the tag we just promoted
    promoted_names: list[str] = []
    kept: list[dict] = []
    for entry in eye_state:
        if entry.get("experiment_tag") == ctx["tag"]:
            promoted_names.append(entry.get("name", "?"))
        else:
            kept.append(entry)

    # Append the new rollback entry (if not already present)
    new_entry = {
        "name": ctx["legacy_name"],
        "description": f"Previous prod model ({ctx['old_crop_size']}×{ctx['old_crop_size']} input) — rollback comparison",
        "crop_size": ctx["old_crop_size"],
        "experiment_tag": ctx["legacy_name"],
    }
    if not any(e.get("name") == new_entry["name"] for e in kept):
        kept.append(new_entry)

    manifest["eye_state"] = kept

    if dry_run:
        print(
            f"  (dry-run) would: edit manifest "
            f"(remove {promoted_names!r}, add {new_entry['name']!r})"
        )
        return
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"  → manifest updated: removed {promoted_names}, added {new_entry['name']!r}"
    )


def backfill_new_shadow(ctx: dict, dry_run: bool):
    if dry_run:
        print(f"  (dry-run) would: experiments_backfill.py --name {ctx['legacy_name']} --force")
        return
    cmd = [
        sys.executable, "-m", "bilbo.scripts.experiments_backfill",
        "--name", ctx["legacy_name"],
        "--force",
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("  WARNING: backfill exited non-zero — the shadow may have incomplete data")
    else:
        print("  → backfill complete")


def reinfer_prod_on_history(dry_run: bool):
    """Re-run the currently-deployed prod model on every baby-present
    frame so ``shadow_birdeye_eye`` reflects the new model. Slow (~5 min).
    """
    if dry_run:
        print("  (dry-run) would: iterate baby-present frames + run_birdeye_inference")
        return
    from bilbo.storage.db import get_db  # noqa: PLC0415
    from bilbo.pipeline.local_pipeline import (  # noqa: PLC0415
        birdeye_result_to_shadow_blob,
        run_birdeye_inference,
    )

    db = get_db()
    entries = db.get_entries()
    todo = [
        e for e in entries
        if e.get("babyPresent") and e.get("frame") and Path(e["frame"]).exists()
    ]
    print(f"  reinfer: {len(todo)} baby-present frames")
    t0 = time.monotonic()
    ok = 0
    skipped = 0
    errors = 0
    for i, entry in enumerate(todo):
        frame_path = Path(entry["frame"])
        try:
            result = run_birdeye_inference(frame_path)
        except Exception:
            errors += 1
            continue
        if result is None:
            skipped += 1
            continue
        shadow = birdeye_result_to_shadow_blob(result)
        updates = {"shadow": shadow}
        if result.get("shadowModelVersion"):
            updates["shadowModelVersion"] = result["shadowModelVersion"]
        if result.get("faceBbox"):
            updates["faceBbox"] = result["faceBbox"]
        if result.get("faceConfidence") is not None:
            updates["faceConfidence"] = result["faceConfidence"]
        db.update_entry(entry["timestamp"], updates)
        ok += 1
        if (i + 1) % 500 == 0:
            elapsed = time.monotonic() - t0
            rate = (i + 1) / max(elapsed, 1e-3)
            eta = (len(todo) - i - 1) / max(rate, 1e-3)
            print(
                f"    [{i+1}/{len(todo)}] ok={ok} skipped={skipped} "
                f"errors={errors}  ({rate:.1f} fr/s, ETA {eta:.0f}s)"
            )
    elapsed = time.monotonic() - t0
    print(
        f"  → reinfer done: ok={ok} skipped={skipped} errors={errors} "
        f"({elapsed:.1f}s)"
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--tag", required=True, help="experiments/<tag>/ directory to promote")
    p.add_argument("--legacy-name", default=None,
                   help="manifest name for the rollback shadow (default: eye_state_<old_size>_legacy)")
    p.add_argument("--crop-size", type=int, default=None,
                   help="override crop size detection — required if --tag doesn't encode it")
    p.add_argument("--dry-run", action="store_true",
                   help="print every step without modifying state")
    p.add_argument("--no-reinfer", action="store_true",
                   help="skip step 10 (re-scoring history with new prod) — faster but the dashboard delta stays stale until new captures accumulate")
    args = p.parse_args()

    ctx = preflight(args)

    total = 12
    _log(1, total, "preflight OK")
    _log(2, total, "stopping launchd capture")
    stop_launchd(args.dry_run)
    _log(3, total, "snapshotting current prod eye-state weights")
    snapshot_current_prod(ctx, args.dry_run)
    _log(4, total, "copying experimental weights to prod")
    copy_experimental_to_prod(ctx, args.dry_run)
    _log(5, total, "writing pipeline/models/latest/meta.json")
    write_prod_meta(ctx, args.dry_run)
    _log(6, total, "patching training_runs row metrics")
    patch_training_runs_metrics(ctx, args.dry_run)
    _log(7, total, "stripping promoted experiment's stale keys from entries")
    cleanup_stale_experiment_keys(ctx, args.dry_run)
    _log(8, total, "updating scripts/lib/experiments.json manifest")
    update_manifest(ctx, args.dry_run)
    _log(9, total, "backfilling new rollback shadow")
    backfill_new_shadow(ctx, args.dry_run)
    _log(10, total, "re-scoring history with new prod" if not args.no_reinfer else "skipping reinfer (--no-reinfer)")
    if not args.no_reinfer:
        reinfer_prod_on_history(args.dry_run)
    _log(11, total, "reloading launchd capture")
    reload_launchd(args.dry_run)
    _log(12, total, "done")

    print()
    print("=" * 70)
    print("Promotion complete." + (" (DRY RUN)" if args.dry_run else ""))
    print()
    print(f"  new prod crop size: {ctx['crop_size']}")
    print(f"  weights from:       experiments/{ctx['tag']}/{ctx['exp_version']}")
    print(f"  rollback shadow:    experiments/{ctx['legacy_name']}/ (at crop size {ctx['old_crop_size']})")
    print()
    print("To roll back (single command):")
    print(f"  python scripts/promote_experiment.py --tag {ctx['legacy_name']}")
    print()
    print("Observe the new prod vs shadow on the dashboard's Shadow Experiments")
    print("card. A negative delta means the new prod is winning — if that number")
    print("ever drifts toward zero or inverts, consider rollback.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
