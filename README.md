# BILBO — Baby Intelligent Lookout & Behavior Observer

An AI-powered baby monitor agent that watches over your newborn via an IP camera, tracks sleep patterns, detects wake events, and sends real-time alerts — all running locally on a Mac with [OpenClaw](https://github.com/openclaw/openclaw).

## What It Does

Bilbo captures a frame from your bassinet camera every minute, analyzes it locally with two on-device classifiers, and builds a detailed picture of your baby's sleep and behavior:

- **Tracks sleep state** — Asleep, Awake, Unknown — with position (Back, Side, Stomach) and location in bassinet
- **Runs locally first** — BIRDEYE (two MobileNetV3-Small classifiers) handles ~98% of frames in ~40ms, falling back to cloud API only when uncertain
- **Detects wake-ups** — confirms by checking last 3 entries (2/3 must show Awake), then sends a Telegram alert with feedback buttons to track accuracy
- **Safety alerts** — immediate notification if baby is pressed against the bassinet side
- **Saves API costs** — BIRDEYE handles ~98% of frames locally. Pixel-diff catches empty bassinets when birdeye is down. Cloud API (gpt-4o) called on ~2% of frames as last resort
- **Adaptive head tracking** — when cloud API is called, it returns the baby's head position, which BIRDEYE uses to center its crop on the next tick
- **Generates reports** — daily/weekly activity reports combining camera data with manual tracking (feeds, pumps, diapers, weight) and monitor performance metrics
- **Human-in-the-loop** — dashboard lets you correct vision model mistakes (edit state/position per frame)

> **Note:** The camera only monitors the bassinet. Sleep that happens elsewhere (stroller, being held, car seat) is not captured — sleep totals from camera data are a lower bound.

## Hardware Setup

Bilbo uses a simple DIY camera rig — no proprietary baby monitor hardware needed.

**What you need:**
1. **IP security camera with RTSP support** — any camera that exposes an RTSP video stream (e.g., TP-Link Tapo C100/C200). Budget: ~$25-40.
2. **Gooseneck microphone stand with clamp** — clamp it to the bassinet so the camera moves with the bassinet. Flexible positioning, no wall mounting needed. Budget: ~$15-20.
3. **Mac** (or any machine that runs Python 3 + ffmpeg) — captures frames and runs the analysis pipeline.

**Setup:**
- Mount the camera on the gooseneck, clamp to the bassinet frame, aim down at the sleep surface
- Connect the camera to your Wi-Fi and note the RTSP stream URL (check your camera's app/docs)
- Ensure the Mac and camera are on the same network

## Software Setup

### Prerequisites

- Python 3.10+
- ffmpeg (`brew install ffmpeg`)
- OpenClaw installed and configured with Telegram
- cloudflared (optional, for remote dashboard access)

### API Keys

Create `.env.baby-monitor` in the workspace root:

```bash
# Camera
RTSP_STREAM_URL="rtsp://username:password@192.168.x.x/stream1"

# Cloud API (backup to local classifiers — only called on ~2% of frames)
OPENAI_API_KEY="sk-..."

# Telegram alerts
TELEGRAM_BOT_TOKEN="123456:ABC..."    # Your Telegram bot token
TELEGRAM_CHAT_ID="123456789"          # Your Telegram user ID
```

### Start Monitoring

Install the launchd service (runs every minute, survives reboots):

```bash
# Copy the plist (edit paths if your workspace differs)
cp com.openclaw.baby-monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.openclaw.baby-monitor.plist

# Verify it's running
launchctl list | grep baby-monitor
```

## Skills

### Baby Monitor (`skills/baby-monitor/`)

The core monitoring pipeline.

**Architecture:**
```
launchd (every 1 min) → capture frame (ffmpeg)
  → Stage 1: BIRDEYE (local classifiers, ~40ms)
    → Classifier 1: bassinet crop → baby present?
    → Classifier 2: head-region crop → eyes_open / eyes_closed / face_not_visible
    → confident? → log, done (handles ~98% of frames)
    → uncertain/failed? ↓
  → Stage 2: pixel-diff gate (cheap safety net, ~10ms)
    → empty bassinet? → log as absent, done
    → changed? ↓
  → Stage 3: cloud vision API (last resort, ~2-5s, $0.01/call)
    → full analysis (gpt-4o)
    → also returns head position → saved for next tick
  → log to JSONL
  → wake detection: Awake after sleep?
    → check last 3 entries: 2/3 Awake? → Telegram alert with feedback buttons
  → edge detection: pressed against side?
    → immediate Telegram alert
```

**BIRDEYE** (Baby IR-aware Recognition & Detection of EYE-state) uses two MobileNetV3-Small classifiers trained on the cloud API's own historical labels. When the baby turns face-down or the model is uncertain, pixel-diff checks if the bassinet is simply empty before falling back to the cloud API. This keeps the cloud API as a true last resort — only called when both local inference and pixel-diff can't resolve the frame. The cloud API also returns the head's approximate position, which BIRDEYE uses to center its head crop on the next tick.

| Metric | Value |
|---|---|
| Local accuracy | 92.2% (vs cloud API ground truth) |
| Frames handled locally | 98% |
| Inference latency | ~40ms on CPU |
| awake→asleep critical misses | 4% |

**Key files:**
| File | Purpose |
|---|---|
| `scripts/monitor.py` | Main pipeline — capture, detect, analyze, alert |
| `scripts/lib/classifiers.py` | BIRDEYE — presence + eye state classifiers |
| `scripts/lib/local_pipeline.py` | BIRDEYE orchestration and fallback logic |
| `scripts/train_classifiers.py` | Train classifiers from sleep-log labels |
| `references/prompt.md` | Cloud API prompt and JSON schema |
| `data/sleep-log.jsonl` | Append-only analysis log (gitignored) |
| `data/head-state.json` | Last known head position (gitignored) |

**CLI modes:**
```bash
monitor.py                           # full pipeline (launchd runs this)
monitor.py --dry-run                 # test without writing to log
monitor.py --backtest --birdeye      # test BIRDEYE accuracy vs cloud API labels
monitor.py --backtest --quick        # replay history against pixel-diff logic
monitor.py --backtest --alerts       # test wake alert accuracy
monitor.py --alert-stats             # show alert precision from user feedback
monitor.py --status                  # system health overview
monitor.py --last 10                 # show recent log entries
```

**Training:**
```bash
# Train both classifiers (~10 min on CPU, labels from sleep-log.jsonl)
python scripts/train_classifiers.py \
  --sleep-log data/sleep-log.jsonl \
  --frames data/frames/ \
  --face-crops pipeline/output/bootstrap/face_crops/
```

### Baby Report (`skills/baby-report/`)

Generates activity reports from camera data, manual tracking CSV, and monitor performance metrics.

```bash
report.py --range 7d                 # weekly report (all sections)
report.py --range 24h                # last 24 hours
report.py --from 2026-03-25 --to 2026-03-31
report.py --section sleep            # single section
report.py --section monitor          # BIRDEYE performance metrics
report.py --format json              # structured output
```

Sections: `sleep`, `feeding`, `pumping`, `diapers`, `weight`, `monitor`. The `monitor` section reports BIRDEYE vs cloud API usage, confidence stats, inference timing, API cost savings, coverage gaps, and state transitions.

## Dashboard

A companion web dashboard provides real-time visibility into baby monitoring data.

**Run it:**
```bash
cd skills/baby-monitor/dashboard
pip install flask                    # one-time setup
python3 app.py                       # accessible at http://localhost:5555
```

**What's on the dashboard:**
- **Live status bar** — current state (Asleep/Awake/Out), duration, latest camera frame thumbnail
- **24-hour timeline** — colored blocks for in-bassinet (blue), awake (light blue), out (orange) with date navigation
- **Click-to-drill-down** — click any timeline block for a detailed table with editable state/position and frame links (human-in-the-loop data correction)
- **Sleep trends chart** — daily total sleep (bars) + longest sleep stretch + longest in-bassinet stretch (lines)
- **Recent events** — last 20 state transitions with timestamps

**Remote access:** Expose via Cloudflare Tunnel with Zero Trust auth for secure access from anywhere.

## Design Decisions

Key tradeoffs and the reasoning behind them.

### Capture Interval

| Interval | Frames/day | Disk/week | Wake detection delay | Cloud API cost (2% fallback) |
|----------|-----------|-----------|---------------------|------------------------------|
| 4 min | 360 | ~1.5 GB | Up to 4 min | ~$0.07/day |
| 2 min | 720 | ~3.0 GB | Up to 2 min | ~$0.14/day |
| **1 min (chosen)** | **1,440** | **~6.0 GB** | **Up to 1 min** | **~$0.29/day** |

**Decision:** 1-minute intervals. With birdeye running locally in 40ms, the cost per frame is effectively zero. The tradeoff is disk — 6GB/week for 7-day frame retention. This also eliminates the need for special burst capture logic (see below).

### Detection Pipeline Order

| Order | When birdeye is healthy | When birdeye is down |
|-------|------------------------|---------------------|
| pixel-diff → birdeye → cloud | Pixel-diff runs on every frame (wasted on baby-present frames) | Pixel-diff catches empties, cloud handles rest |
| **birdeye → pixel-diff → cloud (chosen)** | **Birdeye handles everything, pixel-diff never runs** | **Pixel-diff catches empties before cloud API** |
| birdeye → cloud (no pixel-diff) | Same | Every frame hits cloud API ($0.01 each) |

**Decision:** Birdeye first, pixel-diff as safety net. Pixel-diff only runs on the ~2% of frames where birdeye fails — and even then, it saves a cloud API call when the bassinet is simply empty. Costs nothing when birdeye is healthy, saves money when it's not.

### Local vs Cloud Analysis

| Approach | Latency | Cost | Accuracy | Privacy |
|----------|---------|------|----------|---------|
| Cloud API only | 2-5s | ~$0.01/frame | High (GPT-4o) | Frames sent to OpenAI |
| **Local + cloud fallback (chosen)** | **40ms local, 2-5s fallback** | **~$0.003/frame avg** | **92% local, 100% with fallback** | **98% of frames stay on-device** |
| Local only (no fallback) | 40ms | $0 | 92% (face-down = unknown) | Full privacy |

**Decision:** Local-first with cloud fallback. BIRDEYE handles 98% of frames on-device. The 2% fallback (mostly face-not-visible) gets the cloud API's full analysis AND returns the head position to improve birdeye's next crop. Self-improving loop.

### Wake Confirmation

| Approach | Detection delay | Blocking time | Complexity |
|----------|----------------|---------------|------------|
| Single frame | Instant | 0 | Low, but noisy (false alarms) |
| Burst capture (old) | +2 min | **2 min blocking** (sleeps between captures) | High (extra captures, API calls) |
| **Look-back (chosen)** | +2 min | **0 (non-blocking)** | Low (check last 3 entries) |

**Decision:** Look-back confirmation. With 1-minute intervals, 3 recent entries naturally span ~3 minutes. Requiring 2/3 entries to show "Awake" filters noise without blocking the pipeline or triggering extra captures. Same confirmation quality, zero added latency per run.

### Frame Retention

| Retention | Disk budget | Use case |
|-----------|------------|----------|
| 1 day | ~1 GB | Debugging only |
| 3 days | ~4 GB | Short-term review |
| **7 days (chosen)** | **~6 GB** | **Retraining, backtest, weekly reports** |
| 30 days | ~25 GB | Full history (not worth the disk) |

**Decision:** 7-day retention with 6GB cap. Keeps enough frames to retrain classifiers on recent data, run weekly backtests, and review any alerts from the past week. Oldest frames auto-deleted when cap is reached.

## Workspace Files

| File | Purpose |
|---|---|
| `AGENTS.md` | Agent behavior rules and conventions |
| `SOUL.md` | Personality and tone |
| `USER.md` | User profile and preferences |
| `IDENTITY.md` | Name, emoji, avatar |
| `memory/` | Daily memory files for session continuity |

## Data (not in repo)

All data files are gitignored:
- `.env*` — API keys and credentials
- `*.jsonl`, `*.csv` — monitor logs, activity data
- `data/frames/` — captured camera frames
- `*.log` — system and cron logs
- `*.pt`, `*.onnx` — trained model weights
- `venv/`, `.venv/` — Python virtual environments
- `pipeline/output/` — training output, bootstrap data
- `pipeline/models/` — trained model checkpoints
