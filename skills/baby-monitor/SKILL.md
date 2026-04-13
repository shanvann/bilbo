---
name: baby-monitor
description: Intelligent baby bassinet monitoring via IP camera. Captures RTSP frames every minute, analyzes locally with BIRDEYE classifiers (MobileNetV3), falls back to cloud API (gpt-4o) when uncertain. Use when asked about baby status, monitoring logs, safety alerts, model performance, or to start/stop/query baby monitoring. Triggers on "baby monitor", "check on baby", "baby status", "is the baby sleeping", "monitor log", "bassinet check", "birdeye", "retrain".
---

# Baby Monitor

Automated baby bassinet monitoring: RTSP camera → local ML classifiers → cloud API fallback → Telegram alerts.

## Architecture

```
macOS launchd (every 1 min) → monitor.py
  → Stage 1: pixel-diff gate (~50ms, ffmpeg)
    → empty bassinet? → log as absent, done (no BIRDEYE, no API call)
    → changed? ↓
  → Stage 2: BIRDEYE (3-stage classifier cascade, ~40-100ms)
    → 2a: presence classifier (MobileNetV3-Small, bassinet crop)
        → not_present? → log, done
        → present? ↓
    → 2b: face detector (trainable MobileNetV3, YuNet ONNX fallback)
        → no face? → fall back to cloud API
        → face? ↓
    → 2c: eye-state classifier (MobileNetV3-Small, face crop)
        → low confidence? → fall back to cloud API
        → confident? → log entry, done (~99% of non-empty frames)
  → Stage 3 (fallback only): cloud API (gpt-4o, ~2-5s, ~1% of non-empty frames)
    → full analysis, returns head position → saved for next tick's adaptive crop
  → wake confirmation: 2/3 of last 3 entries Awake? → Telegram alert
  → edge detection: DISABLED post-flip — see github issue #3
```

**Important:** The monitoring pipeline runs via macOS launchd — NOT via OpenClaw cron. This avoids dependency on Anthropic or any LLM for monitoring.

### Single inference entry point

`lib/local_pipeline.py` exposes two helpers that ARE the public API for "run BIRDEYE on a frame":

- `run_birdeye_inference(frame_path)` — runs the 3-stage cascade and returns a fully-shaped entry dict (annotated with `shadowModelVersion`). Returns `None` only on hard error. The dict's `fallback` key is set to `no_face_detected` or `low_confidence` when BIRDEYE bailed; callers use it to decide whether to call the cloud API for the primary fields.
- `birdeye_result_to_shadow_blob(result)` — maps the result to the legacy `shadow` sub-dict shape that `db._derive_shadow_columns()` reads to populate the indexed `shadow_birdeye_*` columns.

Both `monitor.py` (live capture pipeline) and `run_single_inference.py` (dashboard re-run-inference button subprocess) call these helpers. **Don't call `try_local_analysis` directly from new callers** — it's an implementation detail. The shared helpers exist specifically so the live and re-run paths cannot drift on what model output looks like or how it maps to storage.

### BIRDEYE (Baby IR-aware Recognition & Detection of EYE-state)

Three classifiers in cascade, all MobileNetV3-Small except the legacy YuNet fallback:
- **Presence** — fixed bassinet-center crop → `present` / `not_present`
- **Face detector** — bassinet crop → face bbox or None. Trainable MobileNetV3 (`pipeline/models/face_detector.pt`) is the primary; YuNet ONNX fallback runs if the trainable detector misses. If both fail, falls back to a head-position crop seeded by the cloud API's last known head coordinates.
- **Eye state** — tight face crop (from the face detector's bbox) → `eyes_open` / `eyes_closed`

Head position is adaptive: when BIRDEYE falls back to the cloud API, the API returns the head's approximate location in `data/head-state.json`, which BIRDEYE uses to center its crop on the next tick.

| Metric | Value |
|---|---|
| Eye-state macro-F1 vs reviewed/corrected ground truth | 0.91 |
| Presence macro-F1 vs reviewed/corrected ground truth | 0.99 |
| Frames handled locally (no cloud API call) | ~99% of non-empty |
| Total inference latency | ~80-100ms on CPU |
| Daily cloud API cost (post-flip) | ~$0.01/day (was ~$1.17/day) |

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
| `run_single_inference.py <ts>` | Re-run BIRDEYE on the frame for one timestamp and update its `shadow` audit fields (does NOT touch user-facing primary fields or corrections). Thin wrapper around `lib.local_pipeline.run_birdeye_inference` and `birdeye_result_to_shadow_blob` — same shared helpers `monitor.py` uses, so the two paths cannot drift. Called by the dashboard's `/api/run-inference` button via subprocess. |

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
