# BILBO — Baby Intelligent Lookout & Behavior Observer

A baby bassinet monitor that captures a frame every 4 minutes from an IP camera, classifies sleep state with a cloud API (GPT-4o), and trains a local ML model (BIRDEYE) in shadow mode to eventually replace the cloud API. Includes a dashboard for frame review, label correction, model retraining, and performance tracking.

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
launchd (every 4 min) → capture frame (ffmpeg)
  → Shadow: BIRDEYE classifies frame (~40ms, logged but not used)
  → Production: pixel-diff gate → cloud API (GPT-4o)
  → Compare shadow vs prod → log alignment
  → Dual-write: SQLite (primary) + JSONL (backup)
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
launchctl load ~/Library/LaunchAgents/com.openclaw.baby-monitor.plist        # monitor (every 4 min)
launchctl load ~/Library/LaunchAgents/com.openclaw.baby-monitor-dashboard.plist  # dashboard (persistent)
launchctl load ~/Library/LaunchAgents/com.openclaw.baby-monitor-retrain.plist    # daily retrain (12am ET)
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
| `scripts/train_classifiers.py` | Train classifiers with corrections/audit data |
| `dashboard/app.py` | Flask dashboard with training APIs |
| `references/prompt.md` | Cloud API prompt (includes head position) |

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
5. **Model Performance** — selectable time range (10m to 1 week):
   - Alignment rate (shadow birdeye vs ground truth)
   - Misaligned count, prod cost, shadow latency, eye confidence
   - Pending corrections count, monitoring gaps
   - Stacked bars: production pipeline + shadow alignment
6. **Training Stats** — last training run details:
   - Live alignment, pending/previous corrections, training data sources
   - Per-classifier: train accuracy, val loss, epochs, per-class P/R/F1
   - Presence: in-labeled-as-out, out-labeled-as-in, class split
   - Eye state: awake→asleep miss rate, asleep→awake false alarms
   - Deltas from previous model (green/red)
   - Retrain button + abort, training duration, status
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

How often the camera grabs a frame determines how quickly we detect a wake-up and how much disk we burn storing images.

| Interval | Frames/day | Disk/week | Wake detection delay | Cloud API cost |
|----------|-----------|-----------|---------------------|----------------|
| **4 min (chosen)** | **360** | **~1.5 GB** | **Up to 4 min** | **~$3.60/day** |
| 2 min | 720 | ~3.0 GB | Up to 2 min | ~$7.20/day |
| 1 min | 1,440 | ~6.0 GB | Up to 1 min | ~$14.40/day |

**Decision:** 4-minute intervals during shadow mode. Every non-empty frame hits the cloud API (~$0.01 each), so lower frequency saves cost while building alignment data. Once birdeye is promoted to production, can increase to 1-min (birdeye is free).

- **4 min (chosen)** — balances cloud API cost with acceptable wake detection delay during shadow phase.
- **1 min** — ideal for production with local-only inference, but too expensive at $14/day during shadow mode.

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
| **Look-back (chosen)** | +8 min (at 4-min interval) | **0 (non-blocking)** | Low (check last 3 entries) |

**Decision:** Look-back confirmation. Requiring 2/3 entries to show "Awake" filters noise without blocking the pipeline. At 4-min intervals, confirmation takes ~8 minutes — acceptable tradeoff for zero blocking.

### Frame Retention

Captured frames are needed for retraining classifiers, backtesting detection changes, and reviewing alerts. More retention means more data but more disk.

| Retention | Disk budget | Use case |
|-----------|------------|----------|
| 1 day | ~0.4 GB | Debugging only |
| 3 days | ~1.1 GB | Short-term review |
| 7 days | ~1.1 GB | Weekly retraining, single-week backtests |
| **~67 days (chosen)** | **10 GB cap** | **Multi-week backtests + retraining on long history** |

**Decision:** 10 GB cap (raised from 6 GB on 2026-04-09). At 4-min intervals and ~433 KB/frame this holds roughly 67 days of frames, which is enough for multi-week backtest comparisons and for retraining on a long-tail of historical samples. Oldest-first pruning kicks in once the directory exceeds the cap.

### Storage: SQLite vs JSONL

How to store and query monitoring data efficiently.

| Approach | Read speed (24h query) | Write safety | Query flexibility |
|----------|----------------------|-------------|-------------------|
| JSONL only (old) | ~50ms (scan 2800 lines) | Append-only, no corruption | grep/jq only |
| **SQLite + JSONL backup (chosen)** | **~6ms (indexed query)** | **Atomic writes, WAL mode** | **SQL aggregation** |

**Decision:** SQLite as primary read/write, JSONL as append-only backup. Dashboard APIs went from ~50ms to ~6ms. Corrections count went from ~30ms (full scan) to ~0.1ms (indexed).

- **JSONL only** — simple, grep-friendly, but O(n) for every query. At 1440 frames/day, the file grows fast.
- **SQLite + JSONL (chosen)** — indexed queries, atomic writes, SQL aggregation for dashboard stats. JSONL backup preserved for raw access and disaster recovery.

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
| data | JSON | Full entry as JSON (all fields) |
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
| metrics | JSON | Per-classifier: accuracy, loss, F1, miss rates |
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
- `data/frames/` — captured camera frames (10 GB cap, ~67 days at 4-min intervals)
- `data/training-state.json` — PID-based training run state
- `data/head-state.json` — last known head position
- `*.log` — system and cron logs (rotating, 5MB x 3)
- `*.pt` — trained model weights
- `venv/` — Python 3.12 virtualenv
- `pipeline/models/` — versioned model checkpoints (last 20 kept)
- `pipeline/output/` — training data, validated face crops
