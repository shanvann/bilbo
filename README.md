# Baby Monitor

Automated baby bassinet monitoring using an IP camera and Claude's vision AI.

## How It Works

A cron job runs every 5 minutes and performs the following:

1. **Captures a frame** from an RTSP IP camera using ffmpeg (`scripts/capture.sh`)
2. **Analyzes the frame** with Claude's vision API using a structured prompt (`references/prompt.md`)
3. **Logs the results** as a JSON line to `data/baby-monitor-log.jsonl`
4. **Sends a Telegram alert** if any safety concerns are detected (e.g., stomach sleeping, loose items near baby)

```
Cron (every 5 min) → capture frame (ffmpeg) → Claude vision analysis → log + alerts
```

## What It Monitors

| Category | Attributes |
|---|---|
| **Sleep Safety** | Baby present, sleep position (back/side/stomach), objects in bassinet, swaddle presence |
| **Comfort** | Head covering, lighting level |
| **Baby State** | Awake vs asleep, body posture, pacifier engaged |
| **Environment** | Bassinet condition, external hazards |

Each analysis returns a structured JSON object following a strict schema (v1.1). The vision model only reports high-confidence observations — anything uncertain is marked as `"Unknown"`.

## Project Structure

```
skills/baby-monitor/
├── SKILL.md              # Skill metadata and description
├── references/
│   └── prompt.md         # Vision analysis prompt and JSON schema
├── scripts/
│   ├── capture.sh        # Captures a single JPEG frame from the RTSP stream
│   ├── analyze.py        # Utility wrapper for manual frame capture
│   └── test.sh           # Quick test script
└── data/
    ├── frames/           # Captured JPEG frames (auto-cleaned at 1GB)
    └── baby-monitor-log.jsonl  # Append-only analysis log
```

## Configuration

Set the following in `.env.baby-monitor` at the workspace root:

- `RTSP_STREAM_URL` — Full RTSP connection string to your IP camera

## Querying the Log

The log file (`data/baby-monitor-log.jsonl`) contains one JSON object per line:

```json
{"timestamp": "2026-03-28T22:15:00Z", "frame": "/path/to/frame.jpg", "analysis": { ... }}
```

Common queries:

- **Current status** — Read the last log entry
- **Sleep history** — Filter entries where `isBabyPresent = "Yes"` and check `Awake vs asleep`
- **Safety events** — Filter for `Sleep Position = "Stomach"` or hazardous objects detected

## Safety Alerts

Telegram alerts are triggered when the analysis detects:

- Baby sleeping on stomach
- Loose items or cords in the bassinet
- Other hazardous conditions
