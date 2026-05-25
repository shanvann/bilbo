# Shadow → Prod Playbook

This is the end-to-end workflow for taking a new eye-state model from
"I have an idea" to "it's deployed as prod with the old model running
as a rollback safety net." Every step has a single command and every
step is reversible.

The framework here is opinionated: **no new eye-state model ships to
prod without first running as a shadow long enough for the dashboard
to show a clean win on the correction subset.** The promotion script
refuses to run during a training job and always preserves a rollback
snapshot, so a bad deploy costs one command to undo, not a night of
hand-editing SQL and file copies.

---

## The full lifecycle

```
           ┌─────────────┐
           │  1. Train   │  python scripts/train_classifiers.py ...
           │ experiment  │    --model eye-state --eye-crop-size N
           │             │    --experiment-tag <tag>
           └──────┬──────┘
                  │ writes pipeline/models/experiments/<tag>/latest/
                  ▼
           ┌─────────────┐
           │ 2. Register │  edit scripts/lib/experiments.json
           │   shadow    │    (one new entry in "eye_state" list)
           └──────┬──────┘
                  │
                  ▼
           ┌─────────────┐
           │ 3. Backfill │  python scripts/experiments_backfill.py
           │   history   │    --name <experiment_name> --force
           └──────┬──────┘
                  │ populates entries[].data.experiments[<name>]
                  ▼
           ┌─────────────┐
           │ 4. Observe  │  Dashboard → Models tab → Shadow Experiments
           │  dashboard  │    (watch delta, agreement, per-class split)
           └──────┬──────┘
                  │
                  ▼
           ┌─────────────┐
           │ 5. Promote  │  python scripts/promote_experiment.py
           │    (flip)   │    --tag <experiment_tag>
           └──────┬──────┘
                  │ preserves old prod as rollback shadow
                  ▼
           ┌─────────────┐
           │ 6. Verify   │  Check Pipeline card latency; confirm
           │  post-flip  │    Shadow delta went negative (new prod wins)
           └──────┬──────┘
                  │
                  ▼
           ┌─────────────────────────────────┐
           │ 7. (optional) Rollback if       │
           │     something looks wrong:      │
           │                                 │
           │  python scripts/promote_        │
           │    experiment.py --tag          │
           │    eye_state_<old_size>_legacy  │
           └─────────────────────────────────┘
```

---

## Step 1 — Train an experimental variant

Pick a tag that encodes the key variable you're testing. The convention
`eye_state_<crop_size>` lets the promotion script auto-detect the crop
size from the tag name, but any filesystem-safe identifier works if you
pass `--crop-size` explicitly later.

```bash
cd skills/baby-monitor
source venv/bin/activate

python scripts/train_classifiers.py \
    --sleep-log data/sleep-log.jsonl \
    --frames data/frames \
    --output pipeline/models \
    --face-crops pipeline/output/bootstrap/face_crops \
    --corrections data/corrections.jsonl \
    --model eye-state \
    --eye-crop-size 336 \
    --experiment-tag eye_state_336
```

Important flags:

| Flag | Why it matters |
|---|---|
| `--model eye-state` | **Required** when `--experiment-tag` is set — enforced at argparse time. Experimental runs must not clobber the prod presence/face-detect classifiers. |
| `--eye-crop-size 336` | The classifier input resolution. Must match whatever the promotion script will later record in `pipeline/models/latest/meta.json`. |
| `--experiment-tag eye_state_336` | Routes the checkpoint to `pipeline/models/experiments/eye_state_336/v_<ts>/` with a parallel `latest` symlink. Never touches the prod path. |

Output lands at `pipeline/models/experiments/<tag>/latest/eye_state_classifier.pt` plus a `meta.json` sidecar with the exact crop size the training used (see step 5 for why that sidecar matters).

## Step 2 — Register as a shadow

Add one entry to `scripts/lib/experiments.json`:

```json
{
  "eye_state": [
    { "name": "eye_state_224_legacy",
      "description": "Previous prod model (224×224)",
      "crop_size": 224,
      "experiment_tag": "eye_state_224_legacy" },

    { "name": "eye_state_336_candidate",
      "description": "Candidate higher-res retrained model",
      "crop_size": 336,
      "experiment_tag": "eye_state_336" }
  ]
}
```

The `name` field is what shows up on the dashboard's Shadow Experiments card and what the DB stores under `entry.experiments[<name>]`. Keep it short and unique.

Multiple shadow experiments can run concurrently — the framework runs each on every capture tick and stores their results side by side. One frame can legitimately have 2–3 shadow entries.

## Step 3 — Backfill against history

Fresh shadows have no data on the dashboard until new captures accumulate, which is slow. Backfill re-runs the shadow against historical frames so the dashboard shows real numbers immediately:

```bash
python scripts/experiments_backfill.py --name eye_state_336_candidate --force
```

At ~10–15 frames/sec on CPU, 2000 frames ≈ 2–3 minutes. The `--force` flag re-computes even for frames that already have a cached result from an earlier run — needed when you've changed the experiment class or just want to re-baseline.

Other useful flags:

| Flag | Use |
|---|---|
| `--name <exp>` | Run one experiment by name (omit to run all registered) |
| `--hours 24` | Only frames from the last N hours — useful for quick iteration |
| `--limit 50 --verbose` | Smoke test on 50 frames with per-frame output |
| `--dry-run` | Compute but don't persist |

## Step 4 — Observe on the dashboard

Open `http://127.0.0.1:5555/#models` and scroll to the **Shadow Experiments** card. Each registered shadow shows:

- **Agreement with prod**: how often the shadow and prod produce the same eye state. A very high number (>98%) means the shadow barely disagrees — you learn little. A very low number (<80%) means you're looking at a wildly different model.
- **Accuracy vs GT (exp)**: shadow accuracy on the subset of frames you've corrected via the dashboard. This is the adversarial subset — the frames where prod was wrong enough that you intervened.
- **Δ vs prod (GT)**: `shadow_accuracy − prod_accuracy` on the correction subset. **Positive means shadow is winning**, negative means prod is winning. After a successful promotion, this number should be *negative* for the new rollback shadow (the new prod is winning over the old model).
- **Per-class breakdown**: the load-bearing view. The aggregate can hide a +15 on `eyes_open` with a −3 on `eyes_closed`. Always read the per-class split before making a decision.
- **Average latency**: how much slower the shadow is vs prod. Budget ~100ms total cascade — if the shadow alone is 50+ms, running it on every capture tick might push you over.

**What "ready to promote" looks like:**

- Δ vs prod is clearly positive (>5 pts) on the correction subset
- Both per-class deltas are non-negative (or the negative class is tiny)
- At least ~50 correction-subset frames of comparison data
- No surprising latency penalty (<100ms)

**What "NOT ready" looks like:**

- Delta is small, mixed per-class, or noisy
- Shadow wins on one class by double-digits but loses the other class by similar amounts
- Tiny comparison sample size

## Step 5 — Promote

Single command, takes ~5 minutes. Refuses to run during an active training job.

```bash
python scripts/promote_experiment.py --tag eye_state_336
```

What happens, in order:

| # | Step | Notes |
|---|---|---|
| 1 | **Preflight** | Verifies weights exist, manifest parses, training lock is free, crop size is known. Bails with a clear error if anything is off. |
| 2 | **Stop launchd capture** | Prevents a live tick from racing with the file swap. |
| 3 | **Snapshot current prod** | `pipeline/models/experiments/eye_state_<OLD_SIZE>_legacy/v_<ts>/eye_state_classifier.pt` + meta.json + `latest` symlink. This is the **rollback path** — specifically preserved so the promote command run against this tag reverts the flip. |
| 4 | **Copy experimental weights to prod** | `pipeline/models/latest/eye_state_classifier.pt` gets the new weights. Presence + face_detect untouched. |
| 5 | **Write new meta.json** | `pipeline/models/latest/meta.json` gets the new `eye_state_crop_size`. This is what `config.EYE_STATE_INPUT_SIZE` reads at import time — the atomic "the pipeline now runs at N" signal. |
| 6 | **Patch training_runs metrics** | SQLite row for the deployed prod version gets its `eye_state` sub-dict replaced with the experimental run's metrics so the dashboard Eye State column reflects what's actually running. |
| 7 | **Strip stale experiment keys** | The experiment being promoted had cached per-frame results in `entries.data.experiments[<name>]`. Those are now redundant (that IS prod). Strip them. |
| 8 | **Update manifest** | `scripts/lib/experiments.json`: remove the promoted entry, add the new rollback entry pointing at the snapshot from step 3. |
| 9 | **Backfill the rollback shadow** | Run `experiments_backfill.py --name eye_state_<OLD_SIZE>_legacy --force` so the dashboard has real rollback-comparison numbers immediately. |
| 10 | **Re-score history with new prod** | For every baby-present historical frame, run the new deployed model and update `shadow_birdeye_eye`. This is the slow step (~5 min for a week of captures). Skip with `--no-reinfer` if you want to move on faster and let new captures accumulate the comparison data. |
| 11 | **Reload launchd capture** | |
| 12 | **Print rollback command** | You get the exact command to undo the flip, copy-pasted to your scrollback. |

Common flags:

```bash
# Preview everything without touching state:
python scripts/promote_experiment.py --tag eye_state_336 --dry-run

# Faster — skip re-scoring history (OK if you can wait for new captures):
python scripts/promote_experiment.py --tag eye_state_336 --no-reinfer

# Custom rollback name (default: eye_state_<old_size>_legacy):
python scripts/promote_experiment.py --tag eye_state_336 \
    --legacy-name eye_state_224_legacy_before_336
```

## Step 6 — Verify post-flip

Two quick checks on the dashboard:

1. **Pipeline card → Prod Latency**: confirm the new number is reasonable. A bigger model means bigger latency; as long as the full cascade is under ~150ms you're fine on a 1-minute capture cadence.
2. **Shadow Experiments card**: the new entry should be `eye_state_<OLD_SIZE>_legacy` (or whatever `--legacy-name` you chose), and its **delta should be negative** (meaning the new prod beats the old one on the correction subset). If the delta is ~0 or positive, something is off — **roll back**.

You can also run a single live inference to confirm the crop size is what you expect:

```bash
# Force a capture and check the latest entry
python scripts/monitor.py
python -c "
import sqlite3, json
r = sqlite3.connect('data/monitor.db').execute(
    'SELECT data FROM entries ORDER BY timestamp DESC LIMIT 1'
).fetchone()
d = json.loads(r[0])
print('latency:', (d.get('shadow') or {}).get('birdeyeTimings', {}).get('eye_state'))
"
```

The latency should match your expectation for the new crop size (~15ms for 224, ~45ms for 448, etc.).

## Step 7 — Rollback (if needed)

**Single command**:

```bash
python scripts/promote_experiment.py --tag eye_state_<OLD_SIZE>_legacy
```

The rollback is literally the promotion flow pointed at the legacy snapshot. It preserves the current (buggy) prod as a new rollback shadow, which means if the rollback itself turns out to be wrong, you can re-promote forward to the model you just rolled back from. Every promotion is reversible.

---

## Manifest file reference

`scripts/lib/experiments.json`:

```json
{
  "_comment": "See scripts/lib/experiments.py for the schema. The promotion script edits this file atomically — avoid hand-editing while a promote is in progress.",
  "eye_state": [
    {
      "name": "<dashboard identifier — short, unique>",
      "description": "<one-line human hint shown in the card>",
      "crop_size": 224,
      "experiment_tag": "<directory name under pipeline/models/experiments/>"
    }
  ]
}
```

The `eye_state` key is a list so you can register multiple shadows concurrently. Other classifier types (presence, face_detect) will be added as new top-level keys when they grow shadow support.

## meta.json sidecar reference

`pipeline/models/latest/meta.json`:

```json
{
  "eye_state_crop_size": 448,
  "deployed_at": "2026-04-14T16:08:18Z",
  "deployed_version": "v_20260413_175422",
  "trained_by": "train_classifiers.py",
  "models_trained": "eye-state"
}
```

This is the **source of truth** for `EYE_STATE_INPUT_SIZE` at runtime. `scripts/lib/config.py` reads it on import; if it's missing the runtime falls back to `224`. Both `train_classifiers.py` and `promote_experiment.py` are responsible for writing it — if you add a third code path that creates a new version directory, make sure it writes meta.json too or the runtime will silently revert to 224.

## Files in the framework

| File | Purpose |
|---|---|
| `scripts/lib/experiments.py` | Experiment base class, registry loader, `run_all()` called from `monitor.py` on every capture tick |
| `scripts/lib/experiments.json` | Manifest — the data-driven registry of registered shadows |
| `scripts/experiments_backfill.py` | Run shadows against historical frames |
| `scripts/promote_experiment.py` | The one-command promotion + automatic rollback inversion |
| `scripts/train_classifiers.py` | Training, `--eye-crop-size` + `--experiment-tag` flags, writes `meta.json` in every version dir |
| `scripts/lib/config.py` | Reads `pipeline/models/latest/meta.json` to determine `EYE_STATE_INPUT_SIZE` |
| `scripts/lib/db.py::get_experiment_stats` | Per-shadow aggregate computed on demand, folded into `get_safety_stats` |
| `dashboard/static/app.js::renderExperiments` | Shadow Experiments card on the Models tab |

## Things that went wrong before (and how the script avoids them)

- **Retrain runs in parallel with a flip** → weights race, the symlink ends up pointing at a half-copied state. **Preflight refuses to run if `training_state.is_running()` is true.**
- **New model trained at 448 but runtime stays at 224** → silent degradation, no alert. **meta.json sidecar is the single source of truth, written by both train and promote.**
- **Flip succeeds but history still compares against at-capture predictions from the old model** → dashboard delta is meaningless. **Step 10 (reinfer) re-scores history with the new deployed model; `shadow_birdeye_eye` reflects current prod.**
- **Rollback requires hand-editing source code** → fragile, stressful at 3am. **Rollback is the same command pointed at the legacy tag.** No code edits.
- **The SQLite `data` JSON gets clobbered by a re-inference loop** → SQLite-only fields silently stripped. **Fixed in `_reinfer_corrections_against_current_model` which merges with existing SQLite data, and the promotion script follows the same pattern.**
- **Aggregate metrics look fine but a per-class regression hides in them** → you ship a model that's worse on one class. **The dashboard card shows per-class splits directly; the playbook says to always read them before promoting.**
