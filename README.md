# BILBO — Baby Intelligent Lookout & Behavior Observer

An AI-powered baby monitor agent that watches over your newborn via an IP camera, tracks sleep patterns, detects wake events, and sends real-time alerts — all running locally on a Mac with [OpenClaw](https://github.com/openclaw/openclaw).

## What It Does

Bilbo captures a frame from your bassinet camera every 4 minutes, analyzes it with AI vision, and builds a detailed picture of your baby's sleep and behavior:

- **Tracks sleep state** — Asleep, Awake, Unknown — with position (Back, Side, Stomach) and location in bassinet
- **Detects wake-ups** — burst-confirms with 3 frames over 2 minutes to filter vision model noise, then sends a Telegram alert
- **Safety alerts** — immediate notification if baby is pressed against the bassinet side
- **Saves API costs** — pixel-diff detection skips vision API calls when the bassinet is empty (~31% savings)
- **Never goes down** — model fallback chain (gpt-4o-mini → gpt-4o → Claude Sonnet) handles API outages automatically
- **Generates reports** — daily/weekly activity reports combining camera data with manual tracking (feeds, pumps, diapers, weight)

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

# Vision API (primary)
OPENAI_API_KEY="sk-..."

# Vision API (fallback)
ANTHROPIC_API_KEY="sk-ant-..."

# Telegram alerts
TELEGRAM_BOT_TOKEN="123456:ABC..."    # Your Telegram bot token
TELEGRAM_CHAT_ID="123456789"          # Your Telegram user ID
```

### Start Monitoring

Install the launchd service (runs every 4 minutes, survives reboots):

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
launchd (every 4 min) → capture frame (ffmpeg)
  → pixel-diff: empty? → skip API, log as absent
  → not empty? → vision API (gpt-4o-mini → gpt-4o → claude-sonnet)
    → log to JSONL
    → wake detection: Awake after sleep?
      → burst: 2 more frames at 60s intervals
      → 2/3 Awake? → Telegram alert with feedback buttons
    → edge detection: pressed against side?
      → immediate Telegram alert
```

**Key files:**
| File | Purpose |
|---|---|
| `scripts/monitor.py` | Main pipeline — capture, detect, analyze, alert |
| `references/prompt.md` | Vision API prompt and JSON schema |
| `data/sleep-log.jsonl` | Append-only analysis log (gitignored) |
| `data/references/` | Empty bassinet reference frames for pixel-diff calibration |

**CLI modes:**
```bash
monitor.py                           # full pipeline (launchd runs this)
monitor.py --dry-run                 # test without writing to log
monitor.py --backtest --quick        # replay history against current detection logic
monitor.py --backtest --alerts       # test wake alert accuracy
monitor.py --alert-stats             # show alert precision from user feedback
monitor.py --status                  # system health overview
monitor.py --last 10                 # show recent log entries
```

### Baby Report (`skills/baby-report/`)

Generates activity reports from two data sources:
- **Sleep**: camera monitor JSONL (ground truth), CSV fallback for pre-camera days
- **Feeds, pumps, diapers, weight**: activity CSV from parent tracking app

```bash
report.py --range 7d                 # weekly report
report.py --range 24h                # last 24 hours
report.py --from 2026-03-25 --to 2026-03-31
report.py --section sleep            # single section
report.py --format json              # structured output
```

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
- `.venv/` — Python virtual environments
