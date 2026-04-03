# Bilbo — OpenClaw Workspace

Personal AI assistant workspace running on [OpenClaw](https://github.com/openclaw/openclaw). Bilbo is a no-nonsense assistant that manages baby monitoring, activity reporting, and household tasks via Telegram.

## Skills

### Baby Monitor (`skills/baby-monitor/`)

Automated bassinet monitoring using an IP camera + AI vision.

**Architecture:**
```
macOS launchd (every 4 min) → monitor.py → pixel-diff gate → vision API → JSONL log
```

**Features:**
- Frame capture from RTSP camera via ffmpeg
- **Pixel-diff empty detection** — skips API calls when bassinet is empty (~31% savings)
- **Vision analysis** via model fallback chain: gpt-4o-mini → gpt-4o → claude-sonnet-4-6
- **Wake alerts** — burst confirmation (3 frames over 2 min) with Telegram notification + feedback buttons
- **Edge safety alerts** — immediate alert if baby is pressed against bassinet side
- **Backtest mode** — replay historical frames to test detection changes before deploying
- Sleep state tracking: Asleep, Awake, Unknown with position and posture
- All data logged to `data/sleep-log.jsonl`

**Dashboard** (`skills/baby-monitor/dashboard/`):
- Flask + Chart.js web dashboard at `http://localhost:5555`
- Live status bar with latest camera frame
- 24h timeline with date navigation (colored blocks for sleep/awake/absent)
- Click-to-drill-down block detail with editable state/position (human-in-the-loop)
- Sleep trends chart (total sleep + longest stretch + longest in-bassinet)
- Recent events table
- Dark theme, mobile-responsive, auto-refresh

### Baby Report (`skills/baby-report/`)

Generates activity reports from two data sources:
- **Sleep**: camera monitor JSONL (ground truth), CSV fallback for pre-camera days
- **Feeds, pumps, diapers, weight**: activity CSV from parent tracking app

```bash
python3 scripts/report.py --range 7d          # weekly report
python3 scripts/report.py --range 24h         # last 24 hours
python3 scripts/report.py --from 2026-03-25 --to 2026-03-31
python3 scripts/report.py --section sleep     # single section
python3 scripts/report.py --format json       # structured output
```

### Classifieds Poster (`skills/classifieds-poster/`)

Generates and posts classified ad listings on Park Slope Parents.

## Workspace Files

| File | Purpose |
|---|---|
| `AGENTS.md` | Agent behavior rules and conventions |
| `SOUL.md` | Personality and tone |
| `USER.md` | User profile and preferences |
| `IDENTITY.md` | Name, emoji, avatar |
| `HEARTBEAT.md` | Periodic check-in tasks |
| `TOOLS.md` | Local environment notes |
| `memory/` | Daily memory files for continuity |

## Other Agents

- **Neelix** — Separate Telegram bot for household food inventory tracking (workspace at `~/.openclaw/workspace-neelix/`)

## Data (not in repo)

All data files are gitignored:
- `.env*` — API keys and credentials
- `*.jsonl`, `*.csv` — monitor logs, activity data
- `data/frames/` — captured camera frames (~700MB+)
- `*.log` — system and cron logs
- `.venv/` — Python virtual environments
