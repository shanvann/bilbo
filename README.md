# BILBO — Baby Intelligent Lookout & Behavior Observer

![BILBO — Baby Intelligent Lookout & Behavior Observer](docs/bilbo.png)

A baby bassinet monitor that captures a frame every minute from an IP camera, classifies sleep state on-device with a 3-stage MobileNetV3 cascade (BIRDEYE), and falls back to a cloud API (GPT-4o) only when the local cascade can't see a face (~1% of non-empty frames). Includes a dashboard for frame review, label correction, model retraining, and performance tracking.

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Hardware Setup](#hardware-setup)
- [Software Setup](#software-setup)
- [Skills](#skills)
  - [Baby Monitor](#baby-monitor-skillsbaby-monitor)
  - [Baby Report](#baby-report-skillsbaby-report)
  - [AirGradient Logger](#airgradient-logger-skillsairgradient-logger)
- [Dashboard](#dashboard)
- [Continuous Improvement Loop](#continuous-improvement-loop)
- [Design Decisions](#design-decisions)
  - [Eye-state classifier input resolution — flipped to 448 on 2026-04-14](#eye-state-classifier-input-resolution--flipped-to-448-on-2026-04-14)
  - [Shadow vs Production (historical)](#shadow-vs-production-historical)
  - [Capture Interval](#capture-interval)
  - [Detection Pipeline Order](#detection-pipeline-order)
  - [Local vs Cloud Analysis](#local-vs-cloud-analysis)
  - [Wake Confirmation](#wake-confirmation)
  - [Capture Watchdog](#capture-watchdog)
  - [Temporal State Smoothing — added 2026-04-14](#temporal-state-smoothing--added-2026-04-14)
  - [Unknown → Awake absorption (added 2026-04-15)](#unknown--awake-absorption-added-2026-04-15)
  - [FallingAsleep (putdown-pattern absorption, added 2026-04-20)](#fallingasleep-putdown-pattern-absorption-added-2026-04-20)
  - [Frame Retention](#frame-retention)
  - [Storage: SQLite vs JSONL](#storage-sqlite-vs-jsonl)
- [Database Schema](#database-schema)
- [Workspace Files](#workspace-files)
- [Data (not in repo)](#data-not-in-repo)

## What It Does

- **Tracks sleep state** — Asleep, Awake, FallingAsleep, Unknown — via on-device **BIRDEYE** (a 3-stage MobileNetV3-Small cascade: presence → face detection → eye-state) running as production. A cloud API (GPT-4o) is called only when BIRDEYE can't find a face or has low confidence — ~1–2% of non-empty frames post-flip (2026-04-12). The `shadow` sub-dict and `shadow_birdeye_*` columns are now an immutable audit trail of what the model said per frame, separate from the user-correctable primary fields.
- **Detects wake-ups and sleep-onset** — confirms by checking last 3 entries (2/3 agreeing), then sends a Telegram alert. Wake alerts include feedback buttons; asleep alerts fire only on awake→asleep transitions (skipped on placed-already-asleep).
- **Capture watchdog** — runs as a background thread inside the capture container (every 2 min) and pings Telegram if no new frame has been written in `WATCHDOG_ALERT_AFTER_MIN` minutes. Catches RTSP outages, monitor crashes, container restarts; doesn't catch host-off (nothing runs at all).
- **Safety alerts** — immediate notification if baby is pressed against the bassinet side
- **Dashboard** — live camera feed, timeline, frame-by-frame review with eye state correction, block-level labeling, model performance metrics, training stats, retrain button
- **Continuous improvement** — corrections from dashboard feed back into retraining. Model versions are tracked with metrics, rollback support, and post-retrain re-inference
- **SQLite storage** — indexed queries replace JSONL scanning. JSONL kept as append-only backup

> **Note:** The camera only monitors the bassinet. Sleep elsewhere (stroller, arms, car seat) is not captured.

## Architecture

BIRDEYE-primary since 2026-04-12 (commit `7250067`). Before that, the cloud API was production and BIRDEYE ran as a shadow for validation; see [Shadow vs Production (historical)](#shadow-vs-production-historical) for the rationale behind the flip.

```
capture container (--loop, 60s) → capture frame (ffmpeg)
  → Pixel-diff: empty bassinet? → store state=not_present, skip BIRDEYE
  → BIRDEYE (3-stage cascade on CPU, ~130 ms):
       presence → face detector → eye-state
  → If BIRDEYE bails (no_face_detected / low_confidence / hard error):
       Cloud API fallback (GPT-4o) writes primary fields; BIRDEYE result
       is still stored as the `shadow` audit dict.
       If the cloud API itself fails (transport error, insufficient_quota,
       timeout): degrade to BIRDEYE primary, set `cloudUnavailable=true`,
       and on the `low_confidence` path promote the `eyeState` prediction
       to a real `Awake`/`Asleep` rawState rather than `Unknown` — the
       4-of-6 temporal smoother still gates the smoothed `state`.
  → Dual-write: SQLite (primary) + JSONL (append-only backup)
  → Temporal smoothing: 4-of-6 consecutive eyes_open/closed → Awake/Asleep
       (carry forward otherwise, Unknown as last resort)
  → Unknown → Awake absorption: on a confirmed Awake, rewrite any
       immediately-preceding contiguous Unknown+present run (<15 min)
       back to Awake.
  → Wake/Asleep alerts: 2/3 of last 3 entries match + prior opposite
       state + 30-min cooldown → Telegram alert.
  → If cloud API ran: save head position for BIRDEYE's next crop.

watchdog thread (every 2 min, in capture container)
  → newest DB timestamp older than WATCHDOG_ALERT_AFTER_MIN? → Telegram alert
  → recovered after outage? → Telegram "captures resumed" ping
```

```
Continuous improvement loop:
  Dashboard correction → corrections table ──┐
  Audit (--audit)      → audit table       ──┼→ train_classifiers.py → versioned models
  BIRDEYE + cloud-API  → entries table     ──┘        (label priority:
  labels as training                                   corrections >
  signal                                               audit > model
                                                      labels)
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

- Docker + Docker Compose
- An IP camera reachable over RTSP from the host
- (Optional, for host-dev) Python 3.12+ (PyTorch requires <=3.13) and ffmpeg

### Secrets

Copy `.env.example` to `.env` at the repo root and fill in:

```bash
RTSP_URL="rtsp://username:password@192.168.x.x/stream1"
OPENAI_API_KEY="sk-..."
TELEGRAM_BOT_TOKEN="123456:ABC..."
TELEGRAM_CHAT_ID="123456789"
```

### Start the stack

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

This brings up three always-on containers (`capture`, `control-api`, `dashboard`) and exposes the dashboard on `http://localhost:5555`. The training container is on-demand: control-api spawns it via the mounted Docker socket when the dashboard's "Retrain" button is clicked, and it auto-removes when done.

Operational commands:

```bash
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f capture
docker compose -f deploy/docker-compose.yml exec capture bilbo-monitor --status
docker compose -f deploy/docker-compose.yml down
```

For host-dev (running without Docker), `pip install -e ".[ml,control-api,capture]"` and run the console scripts directly — see CLAUDE.md for the full list.

## Skills

### Baby Monitor (`src/bilbo/`)

**Key files:**
| File | Purpose |
|---|---|
| `src/bilbo/monitor.py` | Main pipeline — capture → pixel-diff → BIRDEYE → cloud fallback → smooth → alert → dual-write. `--loop` runs forever (capture container); default runs one tick. |
| `src/bilbo/storage/db.py` | SQLite database — all read/write operations |
| `src/bilbo/pipeline/classifiers.py` | BIRDEYE — presence + eye state classifiers |
| `src/bilbo/pipeline/local_pipeline.py` | BIRDEYE orchestration + `maybe_reload_classifiers()` for symlink-flip hot reload |
| `src/bilbo/training_state.py` | Docker-SDK control plane for the training container (PID fallback for host-dev) |
| `src/bilbo/train_classifiers.py` | Train classifiers with corrections/audit data. `--eye-crop-size` and `--experiment-tag` let you train eye-state variants at different input resolutions without clobbering the prod model. |
| `src/bilbo/scripts/bbox_impact.py` | Measure eye-state accuracy on predicted vs corrected bboxes (manual-run, caches into the `state` table for the dashboard) |
| `src/bilbo/experiments.py` | Shadow pipeline framework — `Experiment` base class, `EyeStateShadowExperiment` generic class, manifest-driven registry. New shadows are added by editing `experiments.json`, not Python source. |
| `src/bilbo/experiments.json` | Data-driven shadow experiment manifest. Edited atomically by `promote_experiment.py` during a flip. |
| `src/bilbo/scripts/experiments_backfill.py` | Run registered shadow experiments against historical frames so the dashboard has immediate comparison data after a new experiment lands. |
| `src/bilbo/scripts/backfill_birdeye_primary.py` | Re-run BIRDEYE with the currently deployed weights over a time window and **write into the primary `eyeState` / `faceBbox` / `presenceConfidence` / `eyeConfidence` fields** (plus refresh the `shadow` audit dict). Skips `eye_state_edited=1` rows by default. Pair with `backfill_state.py` after. |
| `src/bilbo/scripts/backfill_state.py` | Re-smooth `state` + `rawState` over the primary `eyeState` signal for the whole DB. Cheap, re-runnable. Run after `backfill_birdeye_primary.py` or when smoothing thresholds change. |
| `src/bilbo/scripts/promote_experiment.py` | **One-command shadow → prod promotion.** Bundles the flip (snapshot, copy, meta update, metrics patch, stale-key cleanup, manifest edit, backfill, reinfer). Rollback is the same command pointed at the legacy snapshot tag. See `docs/shadow-to-prod-playbook.md`. |
| `src/bilbo/watchdog.py` | Capture-staleness watchdog. Runs as a background thread inside the capture container (every 2 min). Reads newest `entries.timestamp` via SQLite; if older than `WATCHDOG_ALERT_AFTER_MIN`, sends a Telegram alert and tracks state in `data/watchdog-state.json`. Sends a recovery ping when captures resume. |
| `src/bilbo/capture_service.py` | Capture container entry point — Flask :5557 with POST /infer + healthz, plus the monitor and watchdog as daemon threads so warm torch is reused for ad-hoc dashboard re-runs. |
| `src/bilbo/http/app.py` | control-api Flask app on :5556 — mounts the bilbo.api.* contract under /api/v1/*. |
| `src/bilbo/api/` | Internal Python contract (`entries`, `training`, `corrections`, `frames`, `stats`, `inference`, `system`, `air_quality`, `recap`, `models`) used by control-api and capture. New callers must go through here rather than `bilbo.storage.*` / `bilbo.pipeline.*` directly. |
| `dashboard/app.py` | ~120 lines: static + PWA + /api/* reverse proxy to control-api. No bilbo imports. |
| `references/prompt.md` | Cloud API prompt (includes head position) |
| `docs/shadow-to-prod-playbook.md` | Full lifecycle walkthrough: train → register → backfill → observe → promote → rollback. **Read this before shipping a new eye-state model.** |

**CLI modes:**
```bash
bilbo-monitor                                       # one tick (default); the capture container runs `bilbo-monitor --loop`
bilbo-monitor --dry-run                                # test without writing
bilbo-monitor --capture-only                           # grab one frame and exit
bilbo-monitor --analyze FRAME                          # re-run cloud API on an existing frame
bilbo-monitor --retrain                                # retrain with pending corrections (auto-runs post-retrain chain)
bilbo-monitor --retrain --force                        # retrain even if no new corrections
bilbo-monitor --retrain --skip-post-retrain            # retrain only (skip auto backfill + bbox_impact refresh)
bilbo-monitor --retrain --post-retrain-backfill-days 14 # widen auto primary-backfill window (default: 7 days)
bilbo-monitor --eval-corrections                       # re-eval the deployed model on corrections (no retrain)
bilbo-monitor --audit --sample 50                      # spot-check BIRDEYE vs cloud API disagreements
bilbo-monitor --list-models                            # show model versions + metrics
bilbo-monitor --rollback VERSION                       # revert to a previous model
bilbo-monitor --backtest --birdeye                     # BIRDEYE accuracy vs cloud API ground truth
bilbo-monitor --status                                 # system health (gaps, disk, recent stats)
bilbo-monitor --last 10                                # recent log entries
bilbo-monitor --backfill-shadow --hours 168 --only-stale  # re-run BIRDEYE on historical frames → SHADOW audit dict only

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

### Baby Report (`report/`)

```bash
report.py --range 24h                    # full report
report.py --section monitor              # model performance only
report.py --format json                  # structured output
```

Sections: `sleep`, `feeding`, `pumping`, `diapers`, `weight`, `monitor`

### AirGradient Logger (`airgradient-logger/`)

Standalone polling daemon for an AirGradient indoor air-quality monitor on the LAN. Runs as its own systemd unit / docker container (`POLL_SECONDS` = 60s) and writes one row per reading into a SQLite database at `data/airgradient.db`. The dashboard's Air Quality tab reads this DB read-only and pairs it with bassinet state transitions from the monitor DB to overlay state-change vlines on the time-series charts.

```bash
cd airgradient-logger
AIRGRADIENT_URL=http://192.168.1.50/measures/current \
DB_PATH=data/airgradient.db \
venv/bin/python airgradient_logger.py     # run manually (foreground)

systemctl --user status airgradient-logger    # or docker ps for the airgradient-logger container
tail -f logs/stderr.log                   # live log
```

Field mapping reads both camelCase (current firmware: `pm003Count`, `tvocRaw`, `pm02Compensated`, `atmpCompensated`, `rhumCompensated`) and snake_case for older firmware. Full payload is preserved as `raw_json` so any field the typed columns miss can be queried via `json_extract()` (this is how the dashboard pulls `tvocIndex` without a schema migration).

## Dashboard

Live at `http://localhost:5555`. The `dashboard` container is part of the compose stack.

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
5. **BIRDEYE Classifiers** (Models tab) — combined production + training view, selectable range (6h/12h/24h/7d):
   - Stacked rows per stage: Data & Training · Presence · Face Detection · Eye State (was 4 columns until 2026-04-18)
   - Per-stage headline: BIRDEYE Macro F1 + Accuracy vs reviewed/corrected ground truth. Cloud API comparisons removed 2026-04-18 — cloud runs on <2% of frames post-flip, samples too small to be meaningful.
   - Face Detection: Detection Rate + Frames + IoU vs dashboard-drawn corrections + bbox-impact A/B (eye-state accuracy on predicted vs corrected bbox, per-class).
   - Training Validation (collapsible): train/val/test split counts, val + test accuracy/macro-F1, epochs, val loss, per-class P/R/F1 with deltas.
   - Corrections pending/trained, run timing, data sources, model version, rollback badge, retrain button + abort.
6. **Pipeline** (Models tab) — selectable range (10m–1w): cloud cost, BIRDEYE latency, monitoring gaps (>10 min).
7. **Pipeline History** (Models tab, added 2026-04-18) — per-ET-day table of how each captured frame was decided: Captures · Pixel-diff · BIRDEYE · Cloud API (each shown as count + % of captures) · Cost (cloud × $0.01) · BIRDEYE model version(s) (% of BIRDEYE's slice, sums to ~100%). 7/14/30-day window selector. Makes the pre/post-flip cost delta visible at a glance.
8. **Eye-State Daily Metrics** (Models tab, added 2026-04-19) — three SVG line charts (Precision · Recall · F1) per ET day, with one line per class (eyes_open, eyes_closed). Truth = dashboard corrections + reviewed-as-correct frames; predictions = `shadow_birdeye_eye` (BIRDEYE's immutable audit trail), so dashboard re-labels don't contaminate the prediction side. Days with zero ground-truth support for a class render as a gap, not 0. 7/14/30-day window selector. Makes daily improvement (or regressions) after retraining visible at a glance.
9. **Daily Recap** (Events tab, added 2026-04-18) — stitches a day's frames into an MP4 time-lapse via ffmpeg. **In-bassinet frames only** (where `babyPresent=true`); out-of-bassinet stretches (empty crib, putdown blur) are skipped so the recap stays focused on the sleep narrative. Date picker + fps selector (15/30/60), server-side cache under `data/videos/recap_<date>_fps<N>.mp4` reused as long as the frame count matches. First generation ~8–30 s; cache hit is instant.
10. **Recent Events** (Events tab) — state transitions (placed/removed/fell asleep/woke up), selectable count.
11. **Air Quality** (Air Quality tab, added 2026-04-28) — rich dashboard over the AirGradient logger DB, not just charts. Stack of: Hero snapshot cards (Temperature, Humidity, CO₂, PM2.5, TVOC) with status pill (Good/Moderate/Poor/Critical) + dynamic interpretation + static "how this affects baby" line; Baby Comfort Score (0–100 weighted composite — PM2.5 30 %, CO₂ 25 %, Temp 20 %, Humidity 15 %, TVOC 10 %) with per-driver bars showing which metric is pulling the score down; Active Alerts (severity-coded, current threshold breaches + spike detection); Insights (rule-based pattern detection: overnight CO₂ climb, evening PM2.5 spike, humidity drift, overnight summary); What to do now (right-now recommendations tied to the snapshot); Trend charts for all five metrics (1h / 6h / 24h / 3d / 7d / 30d range selector) with bad-zone shading on the metric's "poor" band, plus bassinet state-change vlines (Asleep / FallingAsleep / Awake / Unknown / out-of-bassinet) so AQ excursions correlate against putdowns and wake-ups; Sensor Health footer (last reading, samples vs expected, missing %, OK/Gappy/Stale verdict). Analysis logic lives in `dashboard/aq_analysis.py` (pure functions, no Flask), driven by `/api/air-quality?hours=N`.

**APIs:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/training-status` | GET | Run status (PID-based), metrics, pending count |
| `/api/retrain` | POST | Start retraining (rejects if already running) |
| `/api/retrain/abort` | POST | Kill running training by PID |
| `/api/monitor-stats` | GET | Model performance (SQLite aggregation) |
| `/api/pipeline-history` | GET | `?days=N` (clamped 1–90). Per-ET-day detection-method breakdown + cost + BIRDEYE model versions. Powers the Pipeline History table. |
| `/api/eye-state-daily-metrics` | GET | `?days=N` (clamped 1–90). Per-ET-day BIRDEYE eye-state precision/recall/F1 for `eyes_open` and `eyes_closed` vs corrected/reviewed ground truth. Powers the Eye-State Daily Metrics charts. |
| `/api/recap/generate` | POST | `{date, fps}`. Stitches a day's frames into MP4 via ffmpeg, caches under `data/videos/`. Returns `{status, cached, video_url, frame_count, duration_sec, size_bytes}`. |
| `/api/recap/video` | GET | `?name=recap_<date>_fps<N>.mp4`. Range-capable MP4 delivery for the recap `<video>` element. |
| `/api/air-quality` | GET | `?hours=N` (clamped 1–720). Reads the AirGradient logger DB (path via `AIRGRADIENT_DB_PATH`, defaults to `airgradient-logger/data/airgradient.db`) read-only via SQLite URI mode. Returns bucketed time-series points + latest snapshot + computed analysis (comfort score, statuses, alerts, insights, recommendations, bad-zone spans, sensor health) + bassinet state transitions. Powers the Air Quality tab. |

## Continuous Improvement Loop

```
1. Monitor    — BIRDEYE decides every non-empty frame; cloud API fallback
                on ~1-2% of frames writes the shadow audit dict anyway
2. Review     — dashboard shows BIRDEYE vs corrected-ground-truth Macro F1,
                block-level review checkboxes build up trusted labels
3. Correct    — edit eye state per-frame or per-block in dashboard; also
                draw corrected face bboxes for IoU + bbox-impact analyses
4. Retrain    — manual only (daily cron is disabled); click dashboard
                button or run `bilbo-monitor --retrain`
5. Refresh    — after a successful retrain, the post-retrain chain runs
                automatically: backfill_birdeye_primary (7 days by default)
                → backfill_state → bbox_impact --force, so the dashboard
                Per-class / Bbox-impact numbers track the new model.
                Opt out with --skip-post-retrain.
6. Verify     — post-retrain re-inference on corrected frames
7. Track      — versioned model dirs with metrics, deltas, rollback
8. Shadow-experiment a new candidate — train at an alternate crop size
                / architecture, register in `experiments.json`, observe
                delta on the dashboard, flip with `promote_experiment.py`
```

**Label priority:** dashboard corrections > audit disagreements > cloud/BIRDEYE model labels.

**Model versioning:** timestamped directories (`pipeline/models/v_YYYYMMDD_HHMMSS/`), `latest` symlink, `training_runs` table for metrics, keeps last 20 versions.

**Training state:** PID-based, works across CLI/dashboard/cron. Auto-detects zombie processes.

**Retraining is manual-only.** There is no scheduled retrain — cloud-API labels aren't trusted training signal without manual review first. Retrain when a batch of user corrections is ready (dashboard button, `bilbo-monitor --retrain`, or `docker compose run --rm` against `bilbo:latest` with `bilbo-train`).

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

**Decision:** flipped. `EYE_STATE_INPUT_SIZE = 448` lives in `scripts/lib/config.py`. The prod path in `scripts/lib/local_pipeline.py` passes this to `EyeStateClassifier.classify()`. The old 224 weights are preserved at `pipeline/models/experiments/eye_state_224_legacy/latest/eye_state_classifier.pt` for rollback. The matching `eye_state_224_legacy` inverted shadow experiment (which ran the legacy model on every non-empty frame as a regression tripwire) was retired on **2026-04-18** once the 448 model had 6 days of clean production data — the registry entry was removed from `scripts/lib/experiments.json` but the weights and the historical shadow results in `entries.experiments` are preserved. Re-enable by restoring the JSON entry.

**Rollback** is a single command — literally the same promotion flow pointed at the legacy snapshot:

```bash
python scripts/promote_experiment.py --tag eye_state_224_legacy
```

No retraining, no data migration, no source-file edits. The promotion script handles every step of the flip (snapshot, copy, meta update, SQL patch, manifest edit, backfill, reinfer) and always preserves the current prod as a new rollback snapshot before overwriting, which means rollback-of-rollback is also a single command. See `docs/shadow-to-prod-playbook.md` for the full lifecycle.

**Under the hood** the pipeline's input resolution is read from `pipeline/models/latest/meta.json`, a sidecar written alongside the weights. `config.EYE_STATE_INPUT_SIZE` reads that file on import — a fallback default of 224 kicks in if the sidecar is missing. This means: (a) flipping is a file-write, not a source edit, (b) rollback via the `latest` symlink automatically reverts the crop size because the meta.json in the old version dir is self-contained, (c) `train_classifiers.py` must write meta.json into every version dir it creates — otherwise flipping the symlink would silently revert the runtime to 224. That last invariant is enforced in the training script now (was added 2026-04-14 after a retrain briefly regressed prod by stripping the sidecar).

### Shadow vs Production (historical)

How we safely deployed BIRDEYE without risking production quality. This is the rationale behind the **2026-04-12 flip** (commit `7250067`); BIRDEYE has been primary ever since, and cloud API usage collapsed from ~every non-empty frame to <2%.

| Approach | Risk | Data quality | Cost |
|----------|------|-------------|------|
| Direct deploy | High — bad model = missed wakes | No comparison data | Low |
| A/B split | Medium — some frames get bad model | Partial comparison | Medium |
| **Shadow mode (chosen until alignment ≥95%)** | **Zero — cloud API handles all decisions** | **Every frame compared** | **~$1.20/day at 4-min cadence** |
| **BIRDEYE-primary + cloud fallback (chosen post-flip)** | **Low — cloud catches low-confidence / no-face cases** | **Continuous via user corrections** | **~$0.24/day at 1-min cadence (~95% reduction vs pre-flip-at-1-min)** |

**Decision:** Shadow mode for the build-up phase — cloud API was production and BIRDEYE ran in parallel on every frame for months while we accumulated alignment data. Once alignment crossed 95% and the corrections-driven retraining loop was stable, the pipeline flipped to BIRDEYE-primary with cloud API as the fallback on BIRDEYE bails (`no_face_detected`, `low_confidence`, hard error). The `shadow` sub-dict in every entry is now an immutable record of what BIRDEYE said, kept separate from the user-correctable primary fields.

The flip day also bumped capture cadence from 4 min → 1 min (commit `0045243`, same day), because cloud cost was no longer the constraint. See the **Pipeline History** card on the Models tab for the per-day cost curve across the transition.

### Capture Interval

How often the camera grabs a frame determines how quickly we detect a wake-up and how much disk we burn storing images. This value changed over the life of the project as the cost picture changed.

| Interval | Frames/day | Disk/week | Wake detection delay | Cloud API cost (post-flip) |
|----------|-----------|-----------|---------------------|----------------------------|
| 4 min | 360 | ~1.5 GB | Up to 4 min | ~$0.005/day |
| 2 min | 720 | ~3.0 GB | Up to 2 min | ~$0.01/day |
| **1 min (chosen)** | **1,440** | **~6.0 GB** | **Up to 1 min** | **~$0.02/day** |

**Decision:** 1-minute intervals now that BIRDEYE runs as primary. When the cloud API was on every non-empty frame (~$0.01 each), 4-min intervals were the cost sweet spot — at 1-min they would have cost ~$14/day. Post BIRDEYE-primary flip, the cloud API runs on ~1% of frames as a fallback, so the cost of going to 1-min capture is basically zero and the wake detection latency drops by 4x. The capture interval change also makes the existing `BURST_AWAKE_THRESHOLD = 2 of last 3` wake rule fire ~3 min after a real wake event (was ~12 min at 4-min cadence).

### Detection Pipeline Order

Three systems can analyze a frame (BIRDEYE, pixel-diff, cloud API). The order they run in determines latency, cost, and resilience when one system is down.

| Order | Cost | Resilience | Notes |
|-------|------|------------|-------|
| pixel-diff → cloud + shadow BIRDEYE | ~$1.20/day at 4-min cadence | Cloud-dependent | Pre-flip; BIRDEYE ran every frame as shadow for validation |
| **pixel-diff → BIRDEYE → cloud fallback (chosen, post-2026-04-12)** | **~$0.24/day at 1-min cadence** | **Cloud only needed on BIRDEYE bails (~1-2% of non-empty frames)** | BIRDEYE 3-stage cascade (presence → face → eye-state) handles the hot path on-device; cloud catches `no_face_detected` / `low_confidence` / hard errors. |
| BIRDEYE only | $0 | Degrades when face is hidden; no recovery path | Would be ~98% accurate but the last 2% are the cases we care most about |

**Decision:** pixel-diff → BIRDEYE → cloud fallback. Pixel-diff cheaply gates out empty-bassinet frames before any model runs; BIRDEYE handles the vast majority of non-empty frames in ~130 ms on CPU; the cloud API is a correctness net for the hard cases BIRDEYE flags itself. The **Pipeline History** table on the dashboard is the running audit of this split.

### Local vs Cloud Analysis

The fundamental architecture question: run ML on-device, send frames to a cloud API, or both?

| Approach | Latency | Cost | Accuracy | Privacy |
|----------|---------|------|----------|---------|
| Cloud-primary + BIRDEYE shadow (pre-flip) | 2-5 s | ~$0.01/frame on every non-empty | High (GPT-4o) on every frame | Frames sent to OpenAI |
| **Local-primary + cloud fallback (chosen, post-2026-04-12)** | **~130 ms local, 2-5 s on ~1-2% fallbacks** | **~$0.24/day at 1-min cadence (~95% reduction)** | **~99% on reviewed/corrected ground truth; cloud backs up the hard cases** | **~99% on-device** |
| Local only | ~130 ms | $0 | Degrades silently on face-occluded frames | Full privacy |

**Decision:** Local-primary with cloud fallback. The shadow-mode phase built the alignment data needed to promote BIRDEYE safely; the corrections-driven retraining loop keeps it improving with every label review. Cloud API remains in the pipeline specifically for the frames BIRDEYE can't see clearly — retiring it entirely would mean silently missing the exact events we most want to catch.

### Wake Confirmation

A single "Awake" frame could be noise (classifier error, motion blur). We need a confirmation strategy that filters false alarms without delaying real alerts or blocking the pipeline.

| Approach | Detection delay | Blocking time | Complexity |
|----------|----------------|---------------|------------|
| Single frame | Instant | 0 | Low, but noisy (false alarms) |
| Burst capture (old) | +2 min | **2 min blocking** (sleeps between captures) | High (extra captures, API calls) |
| **Look-back (chosen)** | +3 min (at 1-min interval) | **0 (non-blocking)** | Low (check last 3 entries) |

**Decision:** Look-back confirmation. Requiring 2/3 entries to show "Awake" filters noise without blocking the pipeline. At 1-min intervals, confirmation takes ~3 minutes of consecutive captures (was ~12 min at the old 4-min cadence). The window is hardcoded at 3 frames in `alerts.check_wake_confirmation` as `[-3:]`; if you want to widen it for lower false-positive risk at the faster capture rate, parameterize that slice and the `BURST_AWAKE_THRESHOLD` config constant together.

**Asleep alert (mirror).** `alerts.should_alert_asleep` + `alerts.check_asleep_confirmation` are symmetric to the wake pair: 2/3 of last 3 frames `Asleep` confirms a sleep-onset transition, gated on a prior `Awake` in the `WAKE_WINDOW` lookback so the alert fires only on awake→asleep drift, not on a baby placed already-asleep (which the caretaker just did anyway). Independent 30-min cooldown via `lastAsleepAlert` in `alert-state.json`; both cooldowns reset together on `babyPresent=False` so the next placement session starts fresh.

### Capture Watchdog

Yesterday's monitoring outage (2026-04-16: 16h44m gap) was invisible until the next morning, because nothing alerts when the monitor itself stops working — wake/asleep alerts depend on the monitor running. The watchdog closes that loop.

| Approach | Detects | Doesn't detect | Cost |
|----------|---------|----------------|------|
| In-monitor self-check | Single capture failures | Monitor crashed entirely; container restart loop hides it | Free (already in pipeline) |
| **Capture-container background thread (chosen)** | RTSP outage, monitor crash, capture loop stall | Host off / Docker daemon down | Tiny (one SQL `MAX(timestamp)` query every 2 min) |
| Push-style cloud heartbeat | All of the above + laptop off | Cloud down | Higher (need a cloud endpoint) |

**Decision:** Watchdog thread (`bilbo.watchdog.run_loop`) inside the capture container, running every 2 min. It reads the newest `entries.timestamp` from SQLite, and if it's older than `WATCHDOG_ALERT_AFTER_MIN` (default 5 min), it sends a Telegram alert. State machine in `data/watchdog-state.json` tracks `outage_started_at` / `last_alert_at` so a multi-hour outage gets one initial ping, one reminder per `WATCHDOG_REMINDER_AFTER_MIN` (default 60 min), and one "captures resumed" ping on recovery — no spam, but no silent multi-hour gaps either.

The "laptop off" failure mode is left uncovered. The right fix for that is a push-style heartbeat to a cloud endpoint that alerts when it stops hearing from the monitor; out of scope for the current iteration. Mitigated separately by `pmset -a sleep 0 disksleep 0 autopoweroff 0 standby 0` so the laptop won't enter idle sleep while plugged in.

**Side-note on durability of `caffeinate`-based sleep prevention.** Until 2026-04-17 the only thing keeping the laptop awake was a long-running `caffeinate -dims` process — fragile (dies on reboot or session end). Switched to `pmset -a` settings so sleep is disabled at the system level regardless of process state.

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

**Upstream companion: primary-field inference backfill.** The state smoother reads `eyeState` from history. Pre-BIRDEYE-flip cloud-primary frames don't have an `eyeState` because the cloud API never emitted one, so smoothing those frames just carries forward Unknown. `scripts/backfill_birdeye_primary.py --start <ISO-ts>` re-runs BIRDEYE with the currently deployed weights over a time window and writes the new predictions into the **primary** `eyeState` / `faceBbox` / `presenceConfidence` / `eyeConfidence` fields (and also refreshes the `shadow` audit dict so it stays consistent). Corrected rows (`eye_state_edited = 1`) are skipped by default so user ground-truth labels are preserved. After running this, re-run `backfill_state.py` so the smoother re-fires over the refreshed eye-state signal. This is different from `bilbo-monitor --backfill-shadow` which writes into the shadow audit dict *only* and leaves the primary fields (and therefore the smoother's input) untouched.

**Interaction with the wake alert.** The 2-of-3 wake confirmation in `alerts.check_wake_confirmation` is now strictly weaker than the smoothing rule — a smoothed `Awake` already implies at least 4 consecutive `eyes_open` in the look-back window. The wake check is kept because it still enforces the prior-Asleep gate and the cooldown, but the quorum itself is trivially satisfied on any Asleep→Awake transition. Not a bug, just a note for anyone reading the alert path and wondering why it looks redundant.

**Why a write-time rule and not a render-time rule.** The dashboard, the report skill, the SQLite aggregation queries used by the Pipeline/Events panels, and any future consumer of `entries.state` would otherwise each have to re-derive the same rule. Centralizing at write time means there's exactly one definition of Awake/Asleep in the system, and it lives in one 60-line module.

**History lookup must go through SQLite (incident 2026-04-15).** The live smoother in `monitor.py` calls `db.get_recent_entries(n)` — an indexed `LIMIT` query — to fetch the previous `STATE_CONFIRM_WINDOW - 1` frames. For ~24 hours before this was discovered it was calling `lib.storage.get_recent_entries(n)` instead, which tails the JSONL file with a fixed `n * 600` byte budget. Real entries had grown to ~1,455 bytes (shadow dict + experiments dict + faceBbox), so asking for 5 history frames was returning only 2 — the 4-of-6 consecutive rule could never fire, every present frame fell through to carry-forward, and carry-forward cascaded into `Unknown` indefinitely. The timeline showed 498 consecutive Unknown blocks in what should have been a clear Asleep stretch. The fix was twofold: (1) switch the live smoother's history read to SQLite (matches the architectural rule `read paths must use SQLite via lib/db.py`, which is already in the dual-write notes), and (2) make `storage.get_recent_entries` adaptive — double the read window on underflow — so the other callers (`alerts.should_burst`, `alerts.check_wake_confirmation`) stop silently getting fewer rows than they asked for. The lesson is less about the byte budget and more about **silent undercounts are the worst kind of bug in a smoothing rule**: the consumer can't tell the difference between "history had no matching run" and "history was truncated before the rule could see the run." Anything downstream of a rolling-window rule should assert that the window it received is the size it requested. Same-day follow-up: all runtime JSONL readers migrated to SQLite — `alerts.should_burst` / `check_wake_confirmation`, `detect.detect_empty_bassinet`, the cloud-fallback position heuristic in `monitor.py`, and `dashboard/app.py::api_sleep_stats` (which was previously slurping the entire JSONL on every request). JSONL read paths now exist only in training (`train_classifiers.py`) and historical backtest/audit tools (`cli.py`), both of which intentionally want the append-only log as ground truth.

### Unknown → Awake absorption (added 2026-04-15)

The temporal smoother's 4-of-6 consecutive-eye-state rule produces a lot of tolerable `Unknown` frames between confirmed states — every time BIRDEYE's face detector briefly loses the baby (hand over eyes, crib shift, face pressed into mattress, eye-state confidence dip), the run breaks and subsequent frames carry forward the previous state. When the previous state was also Unknown (e.g., long periods of face_not_visible during fussy play), the timeline shows large Unknown stretches that are almost certainly awake time from a human observer's perspective.

The post-smoothing rule: **when a new `Awake` state is confirmed, any immediately-preceding contiguous run of `Unknown` + `babyPresent` frames whose total span is less than `UNKNOWN_ABSORB_MAX_MINUTES` (default 15) is retroactively reclassified as `Awake`.**

| Approach | Accuracy | Complexity | Downgrade risk |
|---|---|---|---|
| None (Unknown stays Unknown) | Dashboard under-reports awake time | Zero | None, but data is misleading |
| Symmetric (also Unknown → Asleep) | Over-applies to pre-wake ambiguity | Medium | Real "woke up briefly then back to sleep" gets laundered |
| **Asymmetric Unknown → Awake only (chosen)** | Matches the observable signal — the wake is strong, the pre-wake ambiguity is unreliable | Low | Only risk is under-absorbing, which preserves the raw signal |

**Where it runs.** The helper `unknown_prefix_to_absorb` in `lib/state.py` takes a candidate "current" entry and the recent history window, walks backward through contiguous Unknown+present frames, measures the span, and returns the rows to rewrite if the span is within budget. It's called from two places:

- `monitor.py` (live path): after the current entry is persisted, it fetches a history window larger than the absorption budget (`max(STATE_CONFIRM_WINDOW, UNKNOWN_ABSORB_MAX_MINUTES + 5)` rows ≈ 20 minutes), calls the helper, and rewrites each absorbed historical row via `db.update_entry`. The write is post-insert so a failure partway through leaves the DB consistent at the pre-absorption state.
- `scripts/backfill_state.py` (historical): after the pass-1 forward-smoothing sweep, a pass-2 single-pass walk accumulates contiguous Unknown runs and flushes them to Awake when a terminating Awake is within budget. A pass-3 diff against stored state builds the SQL update batch.

**Boundaries that break the run.** Asleep, Awake, not_present, or any non-Unknown state stops the backward walk. A baby-removal (`not_present`) in the middle of the window means only the post-removal Unknown frames can be absorbed, not anything before the removal. `eye_state_edited = 1` corrections are stored in the `eye_state` column but not read by the absorber — the absorber operates on the derived `state` field only, so user eye-state corrections already propagate through the smoother on re-smooth.

### FallingAsleep (putdown-pattern absorption, added 2026-04-20)

Companion rule for the mirror-image of Unknown→Awake, specifically targeting the *putdown-to-sleep* case. The pattern is narrow by design: `not_present → Unknown+babyPresent (run) → Asleep`. When the live smoother (or `backfill_state.py` pass-3) confirms `Asleep` AND the preceding contiguous `Unknown+babyPresent` run is bookended by `not_present`, the run is reclassified by span:

| Run span | New state | Reason |
|----------|-----------|--------|
| ≤ `FALLING_ASLEEP_MAX_MINUTES` (default 30) | `FallingAsleep` | Textbook putdown → settle → sleep. The ambiguous frames are the transition itself, and it's useful to see it as a distinct color on the timeline. |
| > 30 min | `Awake` | Baby was in the bassinet "crib-awake" for a long stretch before dozing off — the ambiguous frames were mostly awake time, not the sleep transition. |

**Why only the `not_present`-bookended pattern?** A pattern like `Awake → Unknown → Asleep` (no removal in between) is also a natural fall-asleep sequence, but the absorption there would conflict with the existing asymmetric-toward-Awake rationale (pre-sleep ambiguity is a weaker signal than the sleep confirmation itself). Limiting this to the putdown case avoids reclassifying pre-sleep ambiguity that wasn't preceded by a fresh placement.

**Why `FallingAsleep` is a first-class state value (not a flag on `Awake`).** The timeline, bassinet chart, and corrections-breakdown chip all want to distinguish this from true crib-awake time — same reason Unknown was elevated out of the Asleep bucket in 2026-04-14. The dashboard renders it as light green between Awake (yellow) and Asleep (green). Alerts are unaffected: `should_alert_asleep` requires `"Awake" in recent_present_states` to fire, and `FallingAsleep` doesn't match, so the putdown case stays silent (as it was before this rule).

**Pass ordering interaction (backfill).** `backfill_state.py` runs Pass 2 (Unknown→Awake) before Pass 3 (FallingAsleep putdown). If the same Unknown run is eligible for both — for instance, a wake-up that shortly preceded sleep — Pass 2 wins (flips to Awake), and Pass 3 sees no Unknown run to classify. This matches live-path behaviour and the design intuition: a wake-up that shortly precedes sleep is really a wake-up.

Helper: `lib.state.putdown_prefix_to_absorb`. Threshold: `FALLING_ASLEEP_MAX_MINUTES` in `lib/config.py`.

### Frame Retention

Captured frames are needed for retraining classifiers, backtesting detection changes, and reviewing alerts. More retention means more data but more disk.

| Retention | Disk budget | Use case |
|-----------|------------|----------|
| 1 day | ~1.5 GB | Debugging only |
| 3 days | ~4.5 GB | Short-term review |
| 7 days | ~10.5 GB | Weekly retraining, single-week backtests |
| **~17 days (chosen)** | **10 GB cap** | **Multi-week backtests + retraining on long history** |

**Decision:** 10 GB cap. At 1-min intervals and ~433 KB/frame this holds roughly 17 days of frames — down from ~67 days at the old 4-min cadence, which is the main tradeoff of the faster sampling rate. Still enough for multi-week backtests and for retraining on a meaningful history. Oldest-first pruning kicks in once the directory exceeds the cap. If you want more retention, either raise `MAX_FRAMES_KB` in `scripts/lib/config.py` or move frames to external storage on a nightly cron.

**Training-aware exception (issue #5).** `enforce_disk_limit()` skips pruning while a training run is active (`lib.training_state.is_running()`). Long trainings iterate `self.samples` populated at `__init__`; if retention deletes frames mid-run, `__getitem__` hits `None` images and recurse-resamples, which gets very slow once many adjacent samples go missing. Disk overshoot during a training run is bounded — at 1 frame/min × ~600 KB, a 6-hour run adds ~210 MB, well under the 10 GB cap.

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
| metrics | JSON | Per-classifier sub-dict. Val-set fields (`val_accuracy`, `best_macro_f1`, `best_val_loss`, `per_class`, `train_total`, `val_total`) are optimistically biased because val is used for best-epoch selection. Held-out test fields (`test_total`, `test_accuracy`, `test_macro_f1`, `test_per_class`) describe the saved best checkpoint on an unseen split and are the honest generalization numbers. Face detector carries `test_mean_iou` + `test_conf_accuracy` instead of `test_accuracy`. Train/val/test splits are deterministic via `time_block_split` with SEED=42 and 30-min blocks. See `CLAUDE.md` for the full schema. |
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
- `data/watchdog-state.json` — capture-watchdog outage/recovery state machine
- `*.log` — system and cron logs (rotating, 5MB x 3)
- `*.pt` — trained model weights
- `venv/` — Python 3.12 virtualenv
- `pipeline/models/` — versioned model checkpoints (last 20 kept)
- `pipeline/output/` — training data, validated face crops
- `airgradient-logger/data/airgradient.db` — AirGradient time-series readings (one row per minute)
- `airgradient-logger/logs/` — logger stdout/stderr
- `airgradient-logger/venv/` — Python virtualenv for the logger
