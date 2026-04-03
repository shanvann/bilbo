---
name: baby-monitor
description: Intelligent baby bassinet monitoring via IP camera. Captures RTSP frames every 4 minutes, analyzes them with OpenAI vision for sleep state, position, and environment conditions. Use when asked about baby status, monitoring logs, safety alerts, or to start/stop/query baby monitoring. Triggers on "baby monitor", "check on baby", "baby status", "is the baby sleeping", "monitor log", "bassinet check".
---

# Baby Monitor

Automated baby bassinet monitoring using RTSP camera + OpenAI vision (gpt-4o).

## Architecture

```
macOS launchd (every 4 min) → monitor.py (ffmpeg capture + OpenAI vision + JSONL log)
```

**Important:** The monitoring pipeline runs independently via macOS launchd — NOT via OpenClaw cron. This was changed on 2026-04-01 because Anthropic API outages caused the OpenClaw cron agent to fail, creating data gaps. The current setup has zero dependency on Anthropic or any LLM for monitoring.

### What runs where
- **Frame capture + vision analysis + logging**: `monitor.py` via launchd (uses OpenAI API directly, no agent wrapper)
- **Querying/reporting**: Agent reads `data/sleep-log.jsonl` and `data/activity-log.csv` on demand
- **Alerts**: Currently disabled (vision model misclassifies positions). If re-enabled, monitor.py writes alerts to stdout log; agent can check periodically

### Related skill
- `baby-report` — generates activity reports from sleep-log.jsonl (sleep) + activity-log.csv (feeds, pumps, diapers, weight)

## Config

- RTSP stream URL + OpenAI key: `~/.openclaw/workspace/.env.baby-monitor`
- Vision prompt: `references/prompt.md`
- Launchd plist: `~/Library/LaunchAgents/com.openclaw.baby-monitor.plist`

## Scheduling (launchd)

```bash
launchctl list | grep baby-monitor                                       # status (exit 0 = ok)
launchctl unload ~/Library/LaunchAgents/com.openclaw.baby-monitor.plist  # stop
launchctl load ~/Library/LaunchAgents/com.openclaw.baby-monitor.plist    # start
```

Stdout/stderr logs: `data/cron-stdout.log`, `data/cron-stderr.log`

**Do NOT recreate an OpenClaw cron job for this.** The launchd approach is more reliable.

## Scripts

`scripts/monitor.py` — single self-contained script. Modes:

| Command | What it does |
|---|---|
| `monitor.py` | Full pipeline: capture → detect → analyze → log |
| `monitor.py --capture-only` | Grab a frame, print path, exit |
| `monitor.py --analyze FILE` | Analyze an existing frame, pretty-print results |
| `monitor.py --dry-run` | Full pipeline but skip JSONL write |
| `monitor.py --last N` | Show last N entries from the JSONL log |
| `monitor.py --status` | System health: log stats, recent gaps, disk usage |
| `monitor.py --backtest --quick` | Replay all historical frames against pixel-diff logic |
| `monitor.py --backtest --count 100` | Backtest last 100 entries only |
| `monitor.py --backtest --from-date 2026-03-31` | Backtest from a specific date |
| `monitor.py --verbose` | Print all log messages to stderr |

## Log Format

`data/sleep-log.jsonl` — one flat JSON object per line:

```json
{
  "timestamp": "2026-03-29T03:49:39Z",
  "frame": "/path/to/frame.jpg",
  "captureMode": "NightVision",
  "cameraTimestamp": "2026-03-28T23:49:31",
  "babyPresent": true,
  "sleepPosition": "Back",
  "objectsInBassinet": "Pacifier",
  "swaddle": "Full",
  "headCovering": "No hat",
  "lighting": "Dark",
  "state": "Asleep",
  "bodyPosture": "Relaxed",
  "pacifierEngaged": false,
  "bassinetCondition": "Clean",
  "hazards": "None",
  "alerts": []
}
```

### Field reference

| Field | Values | Notes |
|---|---|---|
| `babyPresent` | `true` / `false` | |
| `sleepPosition` | Back, Side, Stomach, Unknown | |
| `state` | Asleep, Drowsy, Awake, Unknown | |
| `objectsInBassinet` | None, Pacifier, Blanket, Pillow, Toys, Mixed, Unknown | |
| `swaddle` | None, Partial, Full, Unknown | |
| `headCovering` | Hat, No hat, Unknown | |
| `lighting` | Dark, Dim, Moderate, Bright, Unknown | |
| `bodyPosture` | Relaxed, Tense, Startle reflex, Unknown | |
| `pacifierEngaged` | `true` / `false` / Unknown | In mouth? |
| `bassinetCondition` | Clean, Wrinkled, Loose, Unknown | |
| `hazards` | None, Loose items, Cords nearby, Unknown | Inside bassinet |
| `captureMode` | Normal, NightVision, Unknown | |
| `alerts` | list of strings | Pre-computed safety alerts (currently disabled) |

### Pixel-diff empty detection

Before calling OpenAI, the pipeline compares the current frame against the previous frame using ffmpeg pixel-difference on the center 60% crop. If the previous entry was empty and the diff score is below threshold (5), the bassinet is still empty → API is skipped (~33% savings).

Fields added: `detectionMethod` ("pixel-diff" or "openai-vision"), `diffScore` (float).

Reference empty frames stored in `data/references/` (empty_night.jpg, empty_day.jpg — used for calibration, not runtime).

### Active wake alerts

When the baby is detected as "Awake" after being "Asleep", the pipeline triggers a **burst confirmation**:
1. Normal tick detects Awake → triggers burst
2. Captures + analyzes 2 more frames at 60s intervals
3. If 2+ of 3 frames show Awake → confirmed wake → sends Telegram alert
4. If <2 Awake → suppressed as noise
5. 30-min cooldown between alerts, reset when baby is removed

Alert state stored in `data/alert-state.json`. Telegram credentials in `.env.baby-monitor` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

Each alert includes inline feedback buttons (✅ Yes / ❌ No). When the user taps a button, the callback comes through as a message. Record it with:
```bash
python3 scripts/monitor.py --feedback <alert_id> yes|no
```
The alert_id is in the callback_data: `wake_yes:<id>` or `wake_no:<id>`.

Check accuracy stats:
```bash
python3 scripts/monitor.py --alert-stats
```

Feedback is logged in `data/alert-feedback.jsonl`.

Burst frames are logged to JSONL with `burstFrame: true`.

**Workflow for changing detection logic:**
1. Make changes to `monitor.py` (threshold, diff algorithm, crop region, etc.)
2. Run backtest: `python3 scripts/monitor.py --backtest --quick`
3. Verify exit code 0 (no false skips) and review savings %
4. If false skips > 0, inspect the flagged frames — may be OpenAI mislabels (check visually)
5. Optionally test a date range: `--backtest --quick --from-date YYYY-MM-DD`
6. Only deploy (let launchd pick up changes) after backtest passes
7. Monitor `data/cron-stderr.log` for errors after deploy

### Known limitations
- Vision model frequently misclassifies Side vs Stomach position — treat position data as approximate
- `state: "Unknown"` is common; apply heuristic: if baby is present and position matches previous frame, infer "Asleep"
- Alert rules (stomach position, objects, hazards) are disabled due to false positives

## Activity Log

`data/activity-log.csv` — manual tracking from parents (feeds, pumps, diapers, sleep, weight). Updated when user sends new CSV exports. Column mapping has quirks for Diaper rows (see baby-report skill).

## Baby Profile

See `references/baby-profile.md` for feeding schedule and habits. Key points:
- Fed every 3 hours: 11 PM, 2 AM, 5 AM, 8 AM, 11 AM, 2 PM, 5 PM, 8 PM
- Removed from bassinet for feeds (~30-60 min)
- Usually needs to be woken up to feed

## Querying Logs

To answer questions about the baby:
1. Read `data/sleep-log.jsonl`
2. Parse JSONL — each line is a flat JSON object, no nesting
3. Filter directly on fields (e.g. `babyPresent == true`, `state == "Asleep"`)
4. Apply sleep heuristic: if `state == "Unknown"` and `babyPresent == true` and `sleepPosition` matches previous entry (and isn't "Unknown"), infer "Asleep"
5. Label out-of-bassinet gaps: if `babyPresent == false` and the gap overlaps a feeding time (±45 min), label it "Feeding"

Feeding times (daily, every 3h): 23:00, 02:00, 05:00, 08:00, 11:00, 14:00, 17:00, 20:00

Common queries:
- **Current status**: Last entry in JSONL
- **Sleep history**: Filter `babyPresent == true`, check `state` (apply heuristic)
- **Nap duration**: Consecutive entries where `babyPresent == true`
- **Feeding gaps**: Out-of-bassinet blocks near scheduled feed times
- **Activity reports**: Use `baby-report` skill (`scripts/report.py`)
