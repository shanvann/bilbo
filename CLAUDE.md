# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

BILBO — a baby bassinet monitor. A Python pipeline captures RTSP frames every 4 minutes, classifies sleep state via GPT-4o (production), runs local MobileNetV3 classifiers ("BIRDEYE") in shadow mode, dual-writes to SQLite + JSONL, and serves a Flask dashboard for review/correction/retraining.

`README.md` is the source of truth for architecture, the SQLite schema, and the design-decision tradeoffs. Read it before making non-trivial changes — especially the **Architecture**, **Database Schema**, and **Design Decisions** sections.

> If `skills/baby-monitor/SKILL.md` and `README.md` disagree (e.g. capture interval, pipeline order), **`README.md` wins**. SKILL.md describes the older birdeye-first design; the current architecture is cloud-first with birdeye in shadow mode.

## Layout

- `skills/baby-monitor/` — the monitor pipeline, training, and dashboard. This is where almost all code changes happen.
  - `scripts/monitor.py` — main pipeline entry point (launchd runs this every 4 min)
  - `scripts/lib/db.py` — **all SQLite read/write goes through here**; do not open `data/monitor.db` directly elsewhere
  - `scripts/lib/classifiers.py` — BIRDEYE classifiers: `PresenceClassifier`, `EyeStateClassifier` (MobileNetV3-Small), `FaceDetector` (YuNet ONNX)
  - `scripts/lib/local_pipeline.py` — 3-stage BIRDEYE orchestration: presence → YuNet face detection → eye-state
  - `scripts/lib/training_state.py` — PID-based cross-process training state (CLI/dashboard/cron all coordinate through this)
  - `scripts/lib/config.py` — all constants, paths, thresholds, model chain config, logging setup
  - `scripts/lib/vision.py` — cloud API calls (OpenAI GPT-4o), prompt rendering, response parsing
  - `scripts/lib/detect.py` — pixel-diff empty-bassinet detection
  - `scripts/lib/capture.py` — ffmpeg RTSP frame capture
  - `scripts/lib/alerts.py` — Telegram wake/safety alerts with cooldown logic
  - `scripts/lib/storage.py` — frame retention (oldest-first pruning at 10 GB cap)
  - `scripts/train_classifiers.py` — retraining with corrections + audit data, writes versioned model dirs
  - `dashboard/app.py` — Flask app + training APIs
  - `references/prompt.md` — the GPT-4o vision prompt
  - `scripts/requirements.txt` — Python dependencies (torch, torchvision, opencv-python-headless, openai, scikit-learn, etc.)
- `skills/baby-report/` — read-only reporting on top of the monitor's data (`--section monitor` for model perf)
- `skills/classifieds-poster/` — unrelated text-generation skill
- `AGENTS.md`, `SOUL.md`, `USER.md`, `IDENTITY.md`, `HANDOFF.md`, `HEARTBEAT.md` — agent runtime files (personality, memory protocol, handoff). Ignore for code work; relevant only when running as the assistant.
- `memory/`, `MEMORY.md` — assistant session memory. Not code.

Each skill has its own Python venv (`skills/baby-monitor/venv`, `skills/baby-monitor/dashboard/venv`, `skills/baby-report/venv`). Activate the matching one before running scripts.

## Common commands

All commands assume `cd skills/baby-monitor` and the venv is active.

### Monitor pipeline (`scripts/monitor.py`)
```bash
python scripts/monitor.py                       # full pipeline tick (what launchd runs)
python scripts/monitor.py --dry-run             # run pipeline without writing to DB/JSONL
python scripts/monitor.py --capture-only        # grab one frame and exit
python scripts/monitor.py --analyze FRAME       # re-run cloud API on an existing frame
python scripts/monitor.py --status              # health: gaps, disk, recent stats
python scripts/monitor.py --last 20             # tail recent entries
python scripts/monitor.py --backtest --birdeye  # birdeye accuracy vs cloud ground truth
python scripts/monitor.py --audit --sample 50   # spot-check shadow vs prod disagreements
python scripts/monitor.py --list-models         # versioned model history + metrics
python scripts/monitor.py --rollback VERSION    # revert to a previous model
python scripts/monitor.py --retrain             # retrain with pending corrections
```

### Training (`scripts/train_classifiers.py`)
```bash
python scripts/train_classifiers.py \
  --sleep-log data/sleep-log.jsonl \
  --frames data/frames/ \
  --face-crops pipeline/output/bootstrap/face_crops/ \
  --corrections data/corrections.jsonl \
  --audit data/audit-log.jsonl
# Train just one classifier:
python scripts/train_classifiers.py ... --model presence
python scripts/train_classifiers.py ... --model eye-state
```
Label priority during training: dashboard corrections > audit disagreements > cloud API labels.

### Dashboard
```bash
cd dashboard && source venv/bin/activate && python app.py   # http://localhost:5555
```
Normally runs as a persistent launchd service — only start manually for development.

### Reports
```bash
cd skills/baby-report
python scripts/report.py --range 24h                   # full report
python scripts/report.py --section monitor             # model performance only
python scripts/report.py --range 1h --section monitor  # quick post-deploy check
```

### Scheduling (launchd, NOT OpenClaw cron)
```bash
launchctl list | grep baby-monitor                                                  # status (exit code 0 = ok)
launchctl load   ~/Library/LaunchAgents/com.openclaw.baby-monitor.plist             # capture (every 4 min)
launchctl load   ~/Library/LaunchAgents/com.openclaw.baby-monitor-dashboard.plist   # dashboard (persistent)
launchctl load   ~/Library/LaunchAgents/com.openclaw.baby-monitor-retrain.plist     # daily retrain (12am ET)
launchctl unload <plist>                                                            # to stop
```
Logs land in `skills/baby-monitor/data/system.log`, `cron-stdout.log`, `cron-stderr.log`.

**Do not** create an OpenClaw cron job for monitoring. Monitoring must not depend on Anthropic or any LLM being reachable — launchd is intentional.

## Architecture pointers (read README for the full story)

- **Shadow mode** — every non-empty frame is processed by both pipelines. Cloud API (GPT-4o) decides, BIRDEYE logs in parallel, alignment is recorded. Once alignment ≥ 95%, BIRDEYE is promoted to production. Don't accidentally make BIRDEYE authoritative in code paths until that flip is intentional.
- **BIRDEYE is 3-stage** — (1) presence classifier (MobileNetV3-Small, bassinet crop), (2) YuNet face detector (ONNX, finds face bbox), (3) eye-state classifier (MobileNetV3-Small, face crop from YuNet bbox). If YuNet fails to detect a face, falls back to head-position crop from the cloud API's last known coordinates. The YuNet model file lives at `pipeline/models/face_detection_yunet_2023mar.onnx`.
- **Storage** — `data/monitor.db` (SQLite, WAL mode) is primary; `data/sleep-log.jsonl` is an append-only backup. Read paths must use SQLite via `lib/db.py`. Writes are dual-write — keep them in sync.
- **Wake detection** — non-blocking look-back: 2-of-3 last entries `Awake` triggers a Telegram alert. Don't reintroduce burst-capture (it blocks the pipeline; see Design Decisions in README).
- **Head position** — when the cloud API runs, it returns head coordinates which are stored in `state` (key: `head`) and used by BIRDEYE's adaptive crop on the next tick.
- **Model versioning** — `pipeline/models/v_YYYYMMDD_HHMMSS/` with a `latest` symlink, last 20 kept. Each training run writes a `training_runs` row with full metrics; rollback flips the symlink.
- **Training state is PID-based** — CLI, dashboard, and cron all coordinate via `lib/training_state.py`. The dashboard's `/api/retrain` rejects starts when one is already running, and `/api/retrain/abort` kills by PID. Don't store training status in process-local globals.

## Conventions worth knowing

- **Don't commit data**: `.env*`, `data/`, `pipeline/models/`, `pipeline/output/`, `*.pt`, `*.log`, `venv/` are all gitignored. Never `git add -A` blindly.
- **Git push flow** (from `AGENTS.md`): before pushing, update `README.md` to reflect the latest state, then commit with a concise message. Always confirm with the user before pushing.
- **Sensitive file**: `.env.baby-monitor` holds the RTSP URL, OpenAI key, and Telegram bot token. Never echo or commit it.
- **Frame retention**: 7 days / 6 GB cap on `data/frames/`. Don't change retention logic without checking the disk-budget table in README's Design Decisions.
- **Python version**: 3.12+ but ≤ 3.13 (PyTorch constraint).
- **Trash, not rm**: prefer `trash` for deletes; data files may be the only copy of training signal.
- **No tests or linting**: there is no test suite, no linter config, no `pyproject.toml`. Don't waste time looking for them or proposing to add them unless asked.
- **Retraining is manual-only**: the daily retrain cron is disabled. Only retrain when the user explicitly asks — they don't trust cloud API labels as training signal without manual correction first.
