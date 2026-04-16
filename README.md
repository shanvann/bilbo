# BILBO — Baby Intelligent Lookout & Behavior Observer

A baby bassinet monitor that captures a frame every minute from an IP camera, classifies sleep state on-device with a 3-stage MobileNetV3 cascade (BIRDEYE), and falls back to a cloud API (GPT-4o) only when the local cascade can't see a face (~1% of non-empty frames). Includes a dashboard for frame review, label correction, model retraining, and performance tracking.

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Hardware Setup](#hardware-setup)
- [Software Setup](#software-setup)
- [Skills](#skills)
  - [Baby Monitor](#baby-monitor-skillsbaby-monitor)
  - [Baby Report](#baby-report-skillsbaby-report)
- [Dashboard](#dashboard)
- [Continuous Improvement Loop](#continuous-improvement-loop)
- [Design Decisions](#design-decisions)
  - [Shadow vs Production](#shadow-vs-production)
  - [Capture Interval](#capture-interval)
  - [Detection Pipeline Order](#detection-pipeline-order)
  - [Local vs Cloud Analysis](#local-vs-cloud-analysis)
  - [Wake Confirmation](#wake-confirmation)
  - [Temporal State Smoothing — added 2026-04-14](#temporal-state-smoothing--added-2026-04-14)
  - [Frame Retention](#frame-retention)
  - [Storage: SQLite vs JSONL](#storage-sqlite-vs-jsonl)
- [Workspace Files](#workspace-files)
- [Data (not in repo)](#data-not-in-repo)

## What It Does

- **Tracks sleep state** — Asleep, Awake, Unknown — via cloud API (GPT-4o) as production, with BIRDEYE (two MobileNetV3-Small classifiers) running as shadow for validation
- **Shadow pipeline** — BIRDEYE runs on every frame in parallel with the cloud API. Results are logged and compared but not used for decisions until alignment reaches 95%
- **Detects wake-ups** — confirms by checking last 3 entries (2/3 must show Awake), then sends a Telegram alert with feedback buttons
- **Safety alerts** — immediate notification if baby is pressed against the bassinet side
- **Dashboard** — live camera feed, timeline, frame-by-frame review with eye state correction, block-level labeling, model performance metrics, training stats, retrain button
- **Continuous improvement** — corrections from dashboard feed back into retraining. Model versions are tracked with metrics, rollback support, and post-retrain re-inference
- **SQLite storage** — indexed queries replace JSONL scanning. JSONL kept as append-only backup

> **Note:** The camera only monitors the bassinet. Sleep elsewhere (stroller, arms, car seat) is not captured.

## Architecture

```
launchd (every 1 min) → capture frame (ffmpeg)
  → Shadow: BIRDEYE classifies frame (~40ms, logged but not used)
  → Production: pixel-diff gate → cloud API (GPT-4o)
  → Compare shadow vs prod → log alignment
  → Dual-write: SQLite (primary) + JSONL (backup)
  → Temporal smoothing: 4-of-6 consecutive eyes_open/closed → Awake/Asleep (carry forward otherwise)
  → Wake detection: 2/3 of last 3 entries Awake? → Telegram alert
  → Head position from cloud API → saved for birdeye's next crop
```

```
Continuous improvement loop:
  Dashboard correction → corrections table ──┐
  Audit (--audit)      → audit table       ──┼→ train_classifiers.py → versioned models
  Cloud API labels     → entries table     ──┘
                                                    ↓
                                            Post-retrain re-inference
                                            (verify corrected frames)
```

## Hardware Setup

**What you need:**
1. **IP security camera with RTSP support** — e.g., TP-Link Tapo C100/C200 (~$25-40)
2. **Gooseneck microphone stand with clamp** — clamp to bassinet frame (~$15-20)
3. **Mac** (or any machine with Python 3 + ffmpeg)

**Setup:** Mount camera on gooseneck → aim down at bassinet → connect to Wi-Fi → note RTSP URL

## Software Setup

### Prerequisites

- Python 3.12+ (PyTorch requires <=3.13)
- ffmpeg (`brew install ffmpeg`)
- OpenClaw installed and configured with Telegram

### API Keys

Create `.env.baby-monitor` in the workspace root:

```bash
RTSP_STREAM_URL="rtsp://username:password@192.168.x.x/stream1"
OPENAI_API_KEY="sk-..."
TELEGRAM_BOT_TOKEN="123456:ABC..."
TELEGRAM_CHAT_ID="123456789"
```

### Start Monitoring

```bash
launchctl load ~/Library/LaunchAgents/com.baby-monitor.plist        # monitor (every 1 min)
launchctl load ~/Library/LaunchAgents/com.baby-monitor-dashboard.plist  # dashboard (persistent)
launchctl load ~/Library/LaunchAgents/com.baby-monitor-retrain.plist    # daily retrain (12am ET)
```

## Skills

### Baby Monitor (`skills/baby-monitor/`)

**Key files:**
| File | Purpose |
|---|---|
| `scripts/monitor.py` | Main pipeline — capture, shadow, prod, compare, log |
| `scripts/lib/db.py` | SQLite database — all read/write operations |
| `scripts/lib/classifiers.py` | BIRDEYE — presence + eye state classifiers |
| `scripts/lib/local_pipeline.py` | BIRDEYE orchestration |
| `scripts/lib/training_state.py` | PID-based training state (cross-process) |
| `scripts/train_classifiers.py` | Train classifiers with corrections/audit data. `--eye-crop-size` and `--experiment-tag` let you train eye-state variants at different input resolutions without clobbering the prod model. |
| `scripts/bbox_impact.py` | Measure eye-state accuracy on predicted vs corrected bboxes (manual-run, caches into the `state` table for the dashboard) |
| `scripts/lib/experiments.py` | Shadow pipeline framework — `Experiment` base class, `EyeStateShadowExperiment` generic class, manifest-driven registry. New shadows are added by editing `experiments.json`, not Python source. |
| `scripts/lib/experiments.json` | Data-driven shadow experiment manifest. Edited atomically by `promote_experiment.py` during a flip. |
| `scripts/experiments_backfill.py` | Run registered shadow experiments against historical frames so the dashboard has immediate comparison data after a new experiment lands. |
| `scripts/promote_experiment.py` | **One-command shadow → prod promotion.** Bundles the 12-step flip (snapshot, copy, meta update, metrics patch, stale-key cleanup, manifest edit, backfill, reinfer, launchd reload). Rollback is the same command pointed at the legacy snapshot tag. See `docs/shadow-to-prod-playbook.md`. |
| `dashboard/app.py` | Flask dashboard with training APIs |
| `references/prompt.md` | Cloud API prompt (includes head position) |
| `docs/shadow-to-prod-playbook.md` | Full lifecycle walkthrough: train → register → backfill → observe → promote → rollback. **Read this before shipping a new eye-state model.** |

**CLI modes:**
```bash
monitor.py                           # full pipeline (cron runs this)
monitor.py --dry-run                 # test without writing
monitor.py --retrain                 # retrain with pending corrections
monitor.py --retrain --force         # retrain even if no new corrections (e.g. after code changes)
monitor.py --eval-corrections        # re-eval the deployed model on corrections (no retrain; useful after a rollback)
monitor.py --audit --sample 50       # spot-check birdeye vs cloud API
monitor.py --list-models             # show model versions + metrics
monitor.py --rollback VERSION        # revert to previous model
monitor.py --backtest --birdeye      # test birdeye accuracy
monitor.py --status                  # system health
monitor.py --last 10                 # recent log entries

bbox_impact.py                       # A/B eye-state on predicted vs corrected bbox, caches into `state`
bbox_impact.py --limit 20 --verbose  # iterate during development
bbox_impact.py --dry-run             # compute without persisting
bbox_impact.py --force               # re-run on frames already cached against the current model

# Shadow experiment framework
experiments_backfill.py                         # Run all registered shadow experiments against historical frames
experiments_backfill.py --name eye_state_hires_448_retrained --force  # Re-run a single experiment
experiments_backfill.py --hours 24 --limit 50 --verbose                # Iterate during dev
experiments_backfill.py --dry-run                                      # Compute without persisting
```

**Training an experimental eye-state variant (for the shadow framework):**
```bash
python scripts/train_classifiers.py \
  --sleep-log data/sleep-log.jsonl \
  --frames data/frames \
  --output pipeline/models \
  --face-crops pipeline/output/bootstrap/face_crops \
  --corrections data/corrections.jsonl \
  --model eye-state \
  --eye-crop-size 448 \
  --experiment-tag eye_state_448
# Writes to pipeline/models/experiments/eye_state_448/v_<ts>/ with a parallel
# `latest` symlink. Never touches the prod pipeline/models/latest path.
# The shadow Experiment class for this variant lives in scripts/lib/experiments.py
# and auto-loads from pipeline/models/experiments/eye_state_448/latest/ on first use.
```

**Training:**
```bash
python scripts/train_classifiers.py \
  --sleep-log data/sleep-log.jsonl \
  --frames data/frames/ \
  --face-crops pipeline/output/bootstrap/face_crops/ \
  --corrections data/corrections.jsonl \
  --audit data/audit-log.jsonl
```

### Baby Report (`skills/baby-report/`)

```bash
report.py --range 24h                    # full report
report.py --section monitor              # model performance only
report.py --format json                  # structured output
```

Sections: `sleep`, `feeding`, `pumping`, `diapers`, `weight`, `monitor`

## Dashboard

Live at `http://localhost:5555`. Runs as a persistent launchd service.

**Sections (top to bottom):**

1. **Status bar** — current state, duration, system health
2. **Live camera frame** — updates every capture interval, countdown timer to next frame
3. **Timeline** — 24h colored bar (in-bassinet / awake / out), date navigation, in/out time stats
4. **Block detail** — click any timeline block to review frames:
   - Prev/next block navigation
   - Frame-by-frame viewer with prev/next + arrow keys
   - Per-frame: detection method, model version, eye state label, retrain status badge
   - Block-level label override (apply to all frames at once)
   - Pending retrain count per block
5. **BIRDEYE Classifiers** — combined production + training view (24h/7d):
   - Three columns: Data & Training | Presence | Eye State
   - Per-classifier: production headlines (Macro F1, Accuracy from shadow)
   - Confusion matrices vs Cloud API, per-class P/R/F1
   - vs Corrections accuracy
   - Training validation: val accuracy, macro F1, epochs, val loss, error rates, per-class with deltas
   - Corrections tracking, training data sources, run timing
   - Model version, rollback badge, retrain button + abort
6. **Pipeline** — selectable time range (10m to 1 week):
   - Prod cost, shadow latency, monitoring gaps
   - Stacked bars: production pipeline + shadow alignment
7. **Recent Events** — state transitions (placed/removed/fell asleep/woke up), selectable count

**Training APIs:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/training-status` | GET | Run status (PID-based), metrics, pending count |
| `/api/retrain` | POST | Start retraining (rejects if already running) |
| `/api/retrain/abort` | POST | Kill running training by PID |
| `/api/monitor-stats` | GET | Model performance (SQLite aggregation) |

## Continuous Improvement Loop

```
1. Monitor    — shadow birdeye runs on every frame, compared to prod
2. Review     — dashboard shows alignment %, frames where birdeye disagrees
3. Correct    — edit eye state per-frame or per-block in dashboard
4. Retrain    — click dashboard button, CLI --retrain, or daily 12am cron
5. Verify     — post-retrain re-inference on corrected frames
6. Track      — model versions with metrics, deltas, rollback support
7. Promote    — when alignment ≥95%, switch birdeye from shadow to prod
```

**Label priority:** dashboard corrections > audit disagreements > cloud API labels

**Model versioning:** timestamped directories (`models/v_YYYYMMDD_HHMMSS/`), `latest` symlink, training-log.jsonl with full metrics, keeps last 20 versions.

**Training state:** PID-based, works across CLI/dashboard/cron. Auto-detects zombie processes.

## Design Decisions

Key tradeoffs and the reasoning behind them.

### Eye-state classifier input resolution — flipped to 448 on 2026-04-14

The eye-state classifier originally ran at 224×224 input (the torchvision MobileNetV3-Small default). The bbox-impact analysis on April 13 showed that the `eyes_open` class was the pipeline's accuracy bottleneck — 66-80% depending on the subset — while `eyes_closed` was at 88-99%. The shadow-experiment infra was built, and a separately-trained 448×448 model was run alongside prod for a day.

On the adversarial subset (82 frames where the user had to correct the label), the 448 model hit 86.6% vs prod's 52.4% — a **+34 point absolute improvement** on the cases that matter most. On the broader comparison (1206 frames in a 7-day window) the gap was +11.1 pts, heavily weighted toward `eyes_open`:

| Class | n | Prod 224 | Shadow 448 | Δ |
|---|---|---|---|---|
| `eyes_open` (needs iris/pupil resolution) | 144 | 80.6% | **95.8%** | **+15.2** |
| `eyes_closed` (eyelash line already resolvable at 224) | 55 | 90.9% | 90.9% | 0.0 |

The physics matched the hypothesis: `eyes_open` recognition needs enough pixels on the iris, `eyes_closed` is already above the resolution floor for the eyelash/lid features the model relies on. Latency went from ~15 ms to ~43 ms per inference — still well under budget given the 1-minute capture cadence.

**Decision:** flipped. `EYE_STATE_INPUT_SIZE = 448` lives in `scripts/lib/config.py`. The prod path in `scripts/lib/local_pipeline.py` passes this to `EyeStateClassifier.classify()`. The old 224 weights are preserved at `pipeline/models/experiments/eye_state_224_legacy/latest/eye_state_classifier.pt` and run as an **inverted shadow experiment** — the dashboard's Shadow Experiments card now reports "new 448 prod vs old 224 legacy" so any regression on future data would become visible before anyone notices a missed wake alert.

**Rollback** is a single command — literally the same promotion flow pointed at the legacy snapshot:

```bash
python scripts/promote_experiment.py --tag eye_state_224_legacy
```

No retraining, no data migration, no source-file edits. The promotion script handles every step of the flip (12 steps: snapshot, copy, meta update, SQL patch, manifest edit, backfill, reinfer, launchd reload) and always preserves the current prod as a new rollback snapshot before overwriting, which means rollback-of-rollback is also a single command. See `docs/shadow-to-prod-playbook.md` for the full lifecycle.

**Under the hood** the pipeline's input resolution is read from `pipeline/models/latest/meta.json`, a sidecar written alongside the weights. `config.EYE_STATE_INPUT_SIZE` reads that file on import — a fallback default of 224 kicks in if the sidecar is missing. This means: (a) flipping is a file-write, not a source edit, (b) rollback via the `latest` symlink automatically reverts the crop size because the meta.json in the old version dir is self-contained, (c) `train_classifiers.py` must write meta.json into every version dir it creates — otherwise flipping the symlink would silently revert the runtime to 224. That last invariant is enforced in the training script now (was added 2026-04-14 after a retrain briefly regressed prod by stripping the sidecar).

### Shadow vs Production

How to safely deploy a new ML model without risking production quality.

| Approach | Risk | Data quality | Cost |
|----------|------|-------------|------|
| Direct deploy | High — bad model = missed wakes | No comparison data | Low |
| A/B split | Medium — some frames get bad model | Partial comparison | Medium |
| **Shadow mode (chosen)** | **Zero — cloud API handles all decisions** | **Every frame compared** | **Higher (cloud API on every non-empty frame)** |

**Decision:** Shadow mode. Cloud API is production, birdeye runs in parallel on every frame. Zero risk to production quality while building alignment data. Cost is higher but temporary — once alignment hits 95%, birdeye takes over and cloud API drops to fallback-only.

- **Direct deploy** — fast but dangerous. A regression in the model means missed wake events.
- **A/B split** — reduces risk but some frames still get the untested model.
- **Shadow mode (chosen)** — every frame gets both pipelines. Full comparison data for retraining. Cloud API cost is the price of safety.

### Capture Interval

How often the camera grabs a frame determines how quickly we detect a wake-up and how much disk we burn storing images. This value changed over the life of the project as the cost picture changed.

| Interval | Frames/day | Disk/week | Wake detection delay | Cloud API cost (post-flip) |
|----------|-----------|-----------|---------------------|----------------------------|
| 4 min | 360 | ~1.5 GB | Up to 4 min | ~$0.005/day |
| 2 min | 720 | ~3.0 GB | Up to 2 min | ~$0.01/day |
| **1 min (chosen)** | **1,440** | **~6.0 GB** | **Up to 1 min** | **~$0.02/day** |

**Decision:** 1-minute intervals now that BIRDEYE runs as primary. When the cloud API was on every non-empty frame (~$0.01 each), 4-min intervals were the cost sweet spot — at 1-min they would have cost ~$14/day. Post BIRDEYE-primary flip, the cloud API runs on ~1% of frames as a fallback, so the cost of going to 1-min capture is basically zero and the wake detection latency drops by 4x. The capture interval change also makes the existing `BURST_AWAKE_THRESHOLD = 2 of last 3` wake rule fire ~3 min after a real wake event (was ~12 min at 4-min cadence).

### Detection Pipeline Order

Three systems can analyze a frame (birdeye, pixel-diff, cloud API). The order they run in determines latency, cost, and resilience when one system is down.

| Order | Production | Shadow |
|-------|-----------|--------|
| birdeye → pixel-diff → cloud | Birdeye handles 98%, cloud is last resort | N/A |
| **pixel-diff → cloud + shadow birdeye (chosen)** | **Cloud API is production, birdeye logs in parallel** | **Every frame compared** |

**Decision:** Cloud API as production with birdeye shadow. During shadow phase, the reliable pipeline (pixel-diff → cloud API) handles all decisions while birdeye runs in parallel for comparison.

- **Birdeye first** — the target architecture after promotion. Birdeye handles 98%, pixel-diff catches empties when birdeye fails, cloud API is last resort.
- **Cloud first + shadow (chosen)** — current architecture while building trust in birdeye. Will flip to birdeye-first once alignment exceeds 95%.

### Local vs Cloud Analysis

The fundamental architecture question: run ML on-device, send frames to a cloud API, or both?

| Approach | Latency | Cost | Accuracy | Privacy |
|----------|---------|------|----------|---------|
| **Cloud API + shadow (current)** | **2-5s** | **~$0.01/frame** | **High (GPT-4o)** | **Frames sent to OpenAI** |
| Local + cloud fallback (target) | 40ms local, 2-5s fallback | ~$0.003/frame avg | 92%+ local, 100% with fallback | 98% on-device |
| Local only | 40ms | $0 | 92% (face-down = unknown) | Full privacy |

**Decision:** Cloud API as production now, with a path to local-first. Shadow mode builds the alignment data needed to safely promote birdeye. The self-improving loop (corrections → retrain → re-infer → verify) means accuracy improves with every correction.

### Wake Confirmation

A single "Awake" frame could be noise (classifier error, motion blur). We need a confirmation strategy that filters false alarms without delaying real alerts or blocking the pipeline.

| Approach | Detection delay | Blocking time | Complexity |
|----------|----------------|---------------|------------|
| Single frame | Instant | 0 | Low, but noisy (false alarms) |
| Burst capture (old) | +2 min | **2 min blocking** (sleeps between captures) | High (extra captures, API calls) |
| **Look-back (chosen)** | +3 min (at 1-min interval) | **0 (non-blocking)** | Low (check last 3 entries) |

**Decision:** Look-back confirmation. Requiring 2/3 entries to show "Awake" filters noise without blocking the pipeline. At 1-min intervals, confirmation takes ~3 minutes of consecutive captures (was ~12 min at the old 4-min cadence). The window is hardcoded at 3 frames in `alerts.check_wake_confirmation` as `[-3:]`; if you want to widen it for lower false-positive risk at the faster capture rate, parameterize that slice and the `BURST_AWAKE_THRESHOLD` config constant together.

### Temporal State Smoothing — added 2026-04-14

The primary `state` field was originally derived per-frame from the eye-state classifier (`eyes_open → Awake`, `eyes_closed → Asleep`). That's brittle: one mis-classified frame or a one-second REM blink would flip the state and make the timeline look like dozens of tiny wake-ups between real sleep blocks. The wake alert had its own 2-of-3 look-back confirmation on top, but the stored `state` itself — the thing the dashboard Timeline, Events feed, and SQL aggregations read — was still raw per-frame.

| Approach | False flip rate | Implementation |
|---|---|---|
| Per-frame (old) | High — every noisy frame flips `state` | Single-frame `eyes_open → Awake` mapping at capture time |
| Smooth at read time (dashboard only) | Low — but every consumer needs to re-implement | Timeline code walks history each render |
| **Smooth at write time (chosen)** | **Low — one consistent definition across all readers** | `lib/state.py::smooth_state_temporal` runs before persistence |

**Decision:** smooth at write time, in `monitor.py` right before the entry hits SQLite / JSONL. The rule: within the last `STATE_CONFIRM_WINDOW = 6` baby-present frames (including the current one), a run of `STATE_CONFIRM_RUN = 4` consecutive `eyes_open` readings confirms `Awake`; same for `eyes_closed → Asleep`. Otherwise carry forward the previous smoothed state; degrade to `Unknown` only if there's no Awake/Asleep in history to carry. Non-present frames and intermediate classes (`face_not_visible`, `low_confidence`) break the run, as do cloud-API fallback frames that don't populate `eyeState`.

**Preserving the raw signal.** Each entry now carries a `rawState` field holding the per-frame (unsmoothed) state. Nothing in the live pipeline reads it — it exists solely so `scripts/backfill_state.py` can re-smooth historical entries when the thresholds change, without feeding already-smoothed `state` back into the smoother. The frame-level `eyeState` classifier label is never touched by the smoother and is still the thing the dashboard shows in the block-detail view and the thing the user corrects per-frame.

**One-time backfill.** `scripts/backfill_state.py` walks the DB in timestamp order and rewrites `state` + `rawState` on every entry using the same smoothing function. Non-destructive to `eyeState`, `eye_state_edited`, and all user corrections. Run once after deploying the smoothing change, or any time the window/run thresholds are adjusted.

**Upstream companion: primary-field inference backfill.** The state smoother reads `eyeState` from history. Pre-BIRDEYE-flip cloud-primary frames don't have an `eyeState` because the cloud API never emitted one, so smoothing those frames just carries forward Unknown. `scripts/backfill_birdeye_primary.py --start <ISO-ts>` re-runs BIRDEYE with the currently deployed weights over a time window and writes the new predictions into the **primary** `eyeState` / `faceBbox` / `presenceConfidence` / `eyeConfidence` fields (and also refreshes the `shadow` audit dict so it stays consistent). Corrected rows (`eye_state_edited = 1`) are skipped by default so user ground-truth labels are preserved. After running this, re-run `backfill_state.py` so the smoother re-fires over the refreshed eye-state signal. This is different from `monitor.py --backfill-shadow` which writes into the shadow audit dict *only* and leaves the primary fields (and therefore the smoother's input) untouched.

**Interaction with the wake alert.** The 2-of-3 wake confirmation in `alerts.check_wake_confirmation` is now strictly weaker than the smoothing rule — a smoothed `Awake` already implies at least 4 consecutive `eyes_open` in the look-back window. The wake check is kept because it still enforces the prior-Asleep gate and the cooldown, but the quorum itself is trivially satisfied on any Asleep→Awake transition. Not a bug, just a note for anyone reading the alert path and wondering why it looks redundant.

**Why a write-time rule and not a render-time rule.** The dashboard, the report skill, the SQLite aggregation queries used by the Pipeline/Events panels, and any future consumer of `entries.state` would otherwise each have to re-derive the same rule. Centralizing at write time means there's exactly one definition of Awake/Asleep in the system, and it lives in one 60-line module.

**History lookup must go through SQLite (incident 2026-04-15).** The live smoother in `monitor.py` calls `db.get_recent_entries(n)` — an indexed `LIMIT` query — to fetch the previous `STATE_CONFIRM_WINDOW - 1` frames. For ~24 hours before this was discovered it was calling `lib.storage.get_recent_entries(n)` instead, which tails the JSONL file with a fixed `n * 600` byte budget. Real entries had grown to ~1,455 bytes (shadow dict + experiments dict + faceBbox), so asking for 5 history frames was returning only 2 — the 4-of-6 consecutive rule could never fire, every present frame fell through to carry-forward, and carry-forward cascaded into `Unknown` indefinitely. The timeline showed 498 consecutive Unknown blocks in what should have been a clear Asleep stretch. The fix was twofold: (1) switch the live smoother's history read to SQLite (matches the architectural rule `read paths must use SQLite via lib/db.py`, which is already in the dual-write notes), and (2) make `storage.get_recent_entries` adaptive — double the read window on underflow — so the other callers (`alerts.should_burst`, `alerts.check_wake_confirmation`) stop silently getting fewer rows than they asked for. The lesson is less about the byte budget and more about **silent undercounts are the worst kind of bug in a smoothing rule**: the consumer can't tell the difference between "history had no matching run" and "history was truncated before the rule could see the run." Anything downstream of a rolling-window rule should assert that the window it received is the size it requested.

### Frame Retention

Captured frames are needed for retraining classifiers, backtesting detection changes, and reviewing alerts. More retention means more data but more disk.

| Retention | Disk budget | Use case |
|-----------|------------|----------|
| 1 day | ~1.5 GB | Debugging only |
| 3 days | ~4.5 GB | Short-term review |
| 7 days | ~10.5 GB | Weekly retraining, single-week backtests |
| **~17 days (chosen)** | **10 GB cap** | **Multi-week backtests + retraining on long history** |

**Decision:** 10 GB cap. At 1-min intervals and ~433 KB/frame this holds roughly 17 days of frames — down from ~67 days at the old 4-min cadence, which is the main tradeoff of the faster sampling rate. Still enough for multi-week backtests and for retraining on a meaningful history. Oldest-first pruning kicks in once the directory exceeds the cap. If you want more retention, either raise `MAX_FRAMES_KB` in `scripts/lib/config.py` or move frames to external storage on a nightly cron.

### Storage: SQLite vs JSONL

How to store and query monitoring data efficiently.

| Approach | Read speed (24h query) | Write safety | Query flexibility |
|----------|----------------------|-------------|-------------------|
| JSONL only (old) | ~50ms (scan 2800 lines) | Append-only, no corruption | grep/jq only |
| **SQLite + JSONL backup (chosen)** | **~6ms (indexed query)** | **Atomic writes, WAL mode** | **SQL aggregation** |

**Decision:** SQLite as primary read/write, JSONL as append-only backup. Dashboard APIs went from ~50ms to ~6ms. Corrections count went from ~30ms (full scan) to ~0.1ms (indexed).

- **JSONL only** — simple, grep-friendly, but O(n) for every query. At 1440 frames/day, the file grows fast.
- **SQLite + JSONL (chosen)** — indexed queries, atomic writes, SQL aggregation for dashboard stats. JSONL backup preserved for raw access and disaster recovery.

**Important subtlety — some fields live in SQLite only.** The JSONL file captures only the primary pipeline fields written by `monitor.py` at capture time. Secondary fields written by analysis scripts or the shadow-experiment framework (notably `bboxImpact`, `experiments`, and `faceBboxCorrected`) live in SQLite only, because they're derived after the fact and don't need append-only durability. Any code path that writes back to the `data` column **must merge with the existing SQLite blob**, not overwrite it — otherwise the SQLite-only fields are silently stripped. The retrain re-inference loop in `scripts/lib/cli.py::_reinfer_corrections_against_current_model` does this merge explicitly; treat that as the canonical pattern for any future SQLite-write path.

## Database Schema

Primary storage: `data/monitor.db` (SQLite, WAL mode)

### entries
Main table — one row per captured frame.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| timestamp | TEXT | ISO 8601 UTC (indexed) |
| frame | TEXT | Path to JPEG file |
| baby_present | INTEGER | 1=present, 0=absent |
| state | TEXT | Asleep, Awake, Unknown, not_present |
| eye_state | TEXT | eyes_open, eyes_closed, face_not_visible, not_in_bassinet |
| eye_state_edited | INTEGER | 1 if corrected via dashboard (indexed) |
| eye_state_corrected_at | TEXT | When the correction was made |
| detection_method | TEXT | birdeye, vision-api, pixel-diff, spot-check (indexed) |
| model_used | TEXT | openai/gpt-4o, local/mobilenet+mobilenet, n/a |
| shadow_model_version | TEXT | e.g., v_20260408_201030 |
| presence_confidence | REAL | Birdeye Classifier 1 confidence (0-1) |
| eye_confidence | REAL | Birdeye Classifier 2 confidence (0-1) |
| diff_score | REAL | Pixel-diff score |
| shadow_birdeye_state | TEXT | What birdeye predicted (shadow) |
| shadow_prod_state | TEXT | What prod pipeline returned |
| shadow_agreed | INTEGER | 1=aligned, 0=misaligned, NULL=no shadow |
| shadow_timings_total | REAL | Birdeye inference time in seconds |
| data | JSON | Full entry as JSON (all fields). Notable keys inside: `faceBbox` (BIRDEYE's predicted normalized bbox), `faceBboxCorrected` (user-drawn bbox from the dashboard, treated as ground truth), `bboxImpact` (cached output of `scripts/bbox_impact.py` — A/B eye-state result on both bboxes, present on frames that have been analyzed), `experiments` (map of registered shadow-experiment name → result dict, written by `scripts/lib/experiments.py` on every capture tick and by `scripts/experiments_backfill.py` for historical frames). |
| created_at | TEXT | Row creation timestamp |

### corrections
Dashboard and audit corrections — training signal.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| corrected_at | TEXT | When correction was made (indexed) |
| original_timestamp | TEXT | Entry timestamp being corrected |
| frame | TEXT | Path to frame |
| original_state | TEXT | State before correction |
| corrected_state | TEXT | New state (if sleep state changed) |
| original_eye_state | TEXT | Eye state before correction |
| corrected_eye_state | TEXT | New eye state (eyes_open, eyes_closed, etc.) |
| detection_method | TEXT | What produced the original label |
| source | TEXT | dashboard, audit |
| used_in_training | TEXT | Model version that consumed this correction |

### training_runs
One row per training run — model provenance and metrics.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| version | TEXT | e.g., v_20260408_201030 (indexed) |
| timestamp | TEXT | When training completed |
| entries_total | INTEGER | Total labeled frames used |
| label_sources | JSON | {"cloud-api": 530, "correction": 35, "audit": 10} |
| split | JSON | {"train": 435, "val": 99, "test": 41} |
| config | JSON | Hyperparameters (epochs, lr, batch_size, etc.) |
| metrics | JSON | Per-classifier sub-dict. Val-set fields (`val_accuracy`, `best_macro_f1`, `best_val_loss`, `per_class`, `train_total`, `val_total`) are optimistically biased because val is used for best-epoch selection. Held-out test fields (`test_total`, `test_accuracy`, `test_macro_f1`, `test_per_class`) describe the saved best checkpoint on an unseen split and are the honest generalization numbers. Face detector carries `test_mean_iou` + `test_conf_accuracy` instead of `test_accuracy`. Train/val/test splits are deterministic via `time_block_split` with SEED=42 and 30-min blocks. See `skills/baby-monitor/SKILL.md` for the full schema. |
| models_trained | TEXT | "all", "presence", "eye-state" |
| duration_seconds | REAL | Wall-clock training duration (NULL for runs before 2026-04-09) |
| started_at | TEXT | When training began (ISO 8601 UTC) |
| finished_at | TEXT | When training completed (ISO 8601 UTC) |

### state
Key-value store for runtime state.

| Key | Value | Description |
|-----|-------|-------------|
| head | {"x": 0.5, "y": 0.3, ...} | Last known head position for birdeye crop |
| alert | {"lastEdgeAlert": "..."} | Alert cooldown timestamps |
| training | {"status": "running", "pid": 12345, ...} | Training process state |
| bbox_impact | {"count": N, "accuracyOnPredicted": ..., "accuracyOnCorrected": ..., "delta": ..., "flipRate": ..., "perClass": {...}, "modelVersion": "..."} | Aggregate from `scripts/bbox_impact.py`. Read by the dashboard's Face Detection column to show whether corrected bboxes produce better eye-state predictions. Manual-refresh only — never touched by the live pipeline. |

## Workspace Files

| File | Purpose |
|---|---|
| `AGENTS.md` | Agent behavior rules and conventions |
| `SOUL.md` | Personality and tone |
| `USER.md` | User profile and preferences |
| `IDENTITY.md` | Name, emoji, avatar |
| `memory/` | Daily memory files for session continuity |
| `.claude/settings.json` | PostToolUse hook for doc sync reminders |

## Data (not in repo)

All data files are gitignored:
- `.env*` — API keys and credentials
- `data/monitor.db` — SQLite database (primary storage)
- `data/sleep-log.jsonl` — JSONL backup of all entries
- `data/corrections.jsonl` — JSONL backup of corrections
- `data/frames/` — captured camera frames (10 GB cap, ~17 days at 1-min intervals)
- `data/training-state.json` — PID-based training run state
- `data/head-state.json` — last known head position
- `*.log` — system and cron logs (rotating, 5MB x 3)
- `*.pt` — trained model weights
- `venv/` — Python 3.12 virtualenv
- `pipeline/models/` — versioned model checkpoints (last 20 kept)
- `pipeline/output/` — training data, validated face crops
