---
name: baby-monitor
description: Intelligent baby bassinet monitoring via IP camera. Captures RTSP frames every minute, analyzes locally with BIRDEYE classifiers (MobileNetV3), falls back to cloud API (gpt-4o) when uncertain. Use when asked about baby status, monitoring logs, safety alerts, model performance, or to start/stop/query baby monitoring. Triggers on "baby monitor", "check on baby", "baby status", "is the baby sleeping", "monitor log", "bassinet check", "birdeye", "retrain".
---

# Baby Monitor

Automated baby bassinet monitoring: RTSP camera → local ML classifiers → cloud API fallback → Telegram alerts.

## Architecture

```
macOS launchd (every 1 min) → monitor.py
  → Stage 1: BIRDEYE (two MobileNetV3-Small classifiers, ~40ms)
    → Classifier 1: fixed bassinet crop → baby present / not_present
    → Classifier 2: adaptive head crop → eyes_open / eyes_closed / face_not_visible
    → confident? → log to JSONL, done (~98% of frames)
    → uncertain/failed? ↓
  → Stage 2: pixel-diff gate (~10ms, safety net)
    → empty bassinet? → log as absent, done
    → changed? ↓
  → Stage 3: cloud API (gpt-4o, last resort, ~2-5s)
    → full analysis + returns head position → saved for next tick
  → log to JSONL
  → wake confirmation: 2/3 of last 3 entries Awake? → Telegram alert
  → edge detection: pressed against side? → immediate Telegram alert
```

**Important:** The monitoring pipeline runs via macOS launchd — NOT via OpenClaw cron. This avoids dependency on Anthropic or any LLM for monitoring.

### BIRDEYE (Baby IR-aware Recognition & Detection of EYE-state)

Two MobileNetV3-Small classifiers trained on the cloud API's own historical labels:
- **Classifier 1** — fixed bassinet-center crop → present / not_present
- **Classifier 2** — adaptive head-region crop → eyes_open / eyes_closed / face_not_visible

Head position is adaptive: when birdeye falls back to the cloud API, the API returns the head's approximate location in `data/head-state.json`, which birdeye uses to center its crop on the next tick.

| Metric | Value |
|---|---|
| Accuracy (vs cloud API ground truth) | 92.2% |
| Frames handled locally | ~98% |
| Inference latency | ~40ms on CPU |
| awake→asleep critical misses | 4% |

### Related skill
- `baby-report` — generates activity reports including monitor performance (`--section monitor`)

## Config

- RTSP stream URL + OpenAI key: `~/.openclaw/workspace/.env.baby-monitor`
- Vision prompt: `references/prompt.md`
- Launchd plist: `~/Library/LaunchAgents/com.baby-monitor.plist` (60s interval)
- Classifier models: `pipeline/models/presence_classifier.pt`, `pipeline/models/eye_state_classifier.pt`
- Head state: `data/head-state.json`

## Scheduling (launchd)

```bash
launchctl list | grep baby-monitor                                       # status (exit 0 = ok)
launchctl unload ~/Library/LaunchAgents/com.baby-monitor.plist  # stop
launchctl load ~/Library/LaunchAgents/com.baby-monitor.plist    # start
```

Logs: `data/system.log` (rotating, 5MB x 3), `data/cron-stdout.log`, `data/cron-stderr.log`

**Do NOT recreate an OpenClaw cron job for this.** The launchd approach is more reliable.

## Scripts

### monitor.py — main pipeline (launchd runs this)

| Command | What it does |
|---|---|
| `monitor.py` | Full pipeline: capture → birdeye → pixel-diff → cloud API → log |
| `monitor.py --capture-only` | Grab a frame, print path, exit |
| `monitor.py --analyze FILE` | Analyze an existing frame via cloud API, pretty-print results |
| `monitor.py --dry-run` | Full pipeline but skip JSONL write |
| `monitor.py --last N` | Show last N entries from the JSONL log |
| `monitor.py --status` | System health: log stats, recent gaps, disk usage |
| `monitor.py --backtest --birdeye` | Test BIRDEYE accuracy vs cloud API ground truth |
| `monitor.py --backtest --quick` | Replay history against pixel-diff logic |
| `monitor.py --backtest --alerts` | Test wake alert accuracy |
| `monitor.py --alert-stats` | Show alert precision from user feedback |
| `monitor.py --feedback ID yes\|no` | Record user feedback for an alert |
| `monitor.py --backfill-shadow --hours N [--only-stale] [--limit N] [--dry-run]` | Re-run BIRDEYE shadow inference on historical entries (use after deploying a new model). `--only-stale` skips entries already tagged with the deployed version. |
| `monitor.py --verbose` | Print all log messages to stderr |

### train_classifiers.py — retrain BIRDEYE models

```bash
# Train both classifiers (~10 min on CPU)
python scripts/train_classifiers.py \
  --sleep-log data/sleep-log.jsonl \
  --frames data/frames/ \
  --face-crops pipeline/output/bootstrap/face_crops/

# Train only one
python scripts/train_classifiers.py --sleep-log ... --frames ... --model presence
python scripts/train_classifiers.py --sleep-log ... --frames ... --model eye-state
```

Labels come from the cloud API's annotations in sleep-log.jsonl (no manual labeling needed). `--face-crops` adds manually validated face images to improve awake→asleep accuracy.

## Directory structure

```
skills/baby-monitor/
├── scripts/                        # All source code
│   ├── monitor.py                  # Main pipeline (cron entry point)
│   ├── train_classifiers.py        # Training script
│   ├── requirements.txt            # All Python deps
│   └── lib/
│       ├── config.py               # Paths, constants, classifier config
│       ├── classifiers.py          # BIRDEYE — presence + eye state classifiers
│       ├── local_pipeline.py       # BIRDEYE orchestration and fallback logic
│       ├── vision.py               # Cloud API (gpt-4o) fallback
│       ├── capture.py              # ffmpeg RTSP capture
│       ├── detect.py               # Pixel-diff empty detection
│       ├── alerts.py               # Wake confirmation, edge alerts, Telegram
│       ├── storage.py              # JSONL read/write
│       └── cli.py                  # CLI modes, backtest
├── pipeline/                       # Data only (gitignored)
│   ├── models/                     # Trained .pt weights
│   └── output/                     # Training data, validated face crops
├── dashboard/                      # Flask web UI
├── data/                           # Runtime data (gitignored)
│   ├── sleep-log.jsonl             # Primary log
│   ├── head-state.json             # Last known head position
│   ├── frames/                     # Captured JPEG frames (6GB cap, ~7 days)
│   ├── system.log                  # Pipeline log (rotating)
│   └── alert-state.json            # Wake alert cooldown
├── references/
│   ├── prompt.md                   # Cloud API vision prompt
│   └── baby-profile.md            # Baby profile
└── venv/                           # Python 3.12 virtualenv (gitignored)
```

## Log format

`data/sleep-log.jsonl` — one flat JSON object per line. Key fields:

| Field | Values | Notes |
|---|---|---|
| `detectionMethod` | `birdeye`, `pixel-diff`, `vision-api` | Which stage resolved the frame |
| `modelUsed` | `local/mobilenet+mobilenet`, `openai/gpt-4o`, `n/a` | Model that produced the result |
| `babyPresent` | `true` / `false` | |
| `state` | Asleep, Awake, Unknown, not_present | |
| `presenceConfidence` | 0.0–1.0 | Birdeye Classifier 1 confidence |
| `eyeConfidence` | 0.0–1.0 | Birdeye Classifier 2 confidence |
| `eyeState` | eyes_open, eyes_closed, face_not_visible | Birdeye raw eye classification |
| `headPosition` | `{"x": 0.35, "y": 0.22, "visible": true}` | From cloud API (when called) |
| `birdeyeTimings` | `{"presence": 0.02, "eye_state": 0.02, "total": 0.05}` | Per-stage timing |
| `sleepPosition` | Back, Side, Stomach, Unknown | Cloud API only |
| `alerts` | list of strings | Safety alerts triggered |

### Active wake alerts

When the baby is detected as "Awake" after being "Asleep", the pipeline confirms by checking the last 3 entries. If 2+ show Awake → sends Telegram alert with inline feedback buttons. 30-min cooldown between alerts, reset when baby is removed.

```bash
python3 scripts/monitor.py --feedback <alert_id> yes|no   # record feedback
python3 scripts/monitor.py --alert-stats                   # check accuracy
```

### Workflow for changing detection logic

1. Make changes to source files
2. Run backtest: `python3 scripts/monitor.py --backtest --birdeye --count 500`
3. Check accuracy, confusion matrix, critical miss rate
4. Reload launchd: `launchctl unload .../plist && launchctl load .../plist`
5. Monitor `data/cron-stderr.log` for import errors
6. Check performance: `python3 ../baby-report/scripts/report.py --range 1h --section monitor`

### Known limitations
- **Bassinet only** — camera doesn't see sleep in stroller, arms, car seat. Sleep totals are a lower bound.
- BIRDEYE returns "Unknown" fields (position, swaddle, hazards) when it handles the frame locally — only cloud API provides full attribute set
- Vision model frequently misclassifies Side vs Stomach position
- Alert rules (stomach position, objects, hazards) are disabled due to false positives

## Activity Log

`data/activity-log.csv` — manual tracking from parents (feeds, pumps, diapers, sleep, weight). Updated when user sends new CSV exports. Column mapping has quirks for Diaper rows (see baby-report skill).

## Querying Logs

To answer questions about the baby:
1. Read `data/sleep-log.jsonl` (each line is a flat JSON object)
2. Filter on fields (e.g. `babyPresent == true`, `state == "Asleep"`)
3. Check `detectionMethod` to know which stage resolved each frame
4. For activity reports, use `baby-report` skill (`--section monitor` for model performance)
