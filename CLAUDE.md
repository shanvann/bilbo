# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

BILBO — a baby bassinet monitor. A Python pipeline captures RTSP frames every minute, runs BIRDEYE (MobileNetV3 cascade) on-device as the primary decider, falls back to GPT-4o as a cloud backstop on ~1% of frames, dual-writes to SQLite + JSONL, and serves a Flask dashboard for review/correction/retraining.

`README.md` is the reference for the SQLite schema and the design-decision tradeoffs. Read it before touching the DB or revisiting a past tradeoff. This file is the fastest summary; README and `skills/baby-monitor/SKILL.md` describe the same post-flip BIRDEYE-primary pipeline (commit `7250067`) in more depth.

## Layout

- `skills/baby-monitor/` — the monitor pipeline, training, and dashboard. This is where almost all code changes happen.
  - `scripts/monitor.py` — main pipeline entry point (launchd runs this every 1 min)
  - `scripts/run_single_inference.py` — runs BIRDEYE on one frame by timestamp; invoked as a subprocess by the dashboard's `/api/run-inference` (the dashboard venv intentionally has no torch/cv2)
  - `scripts/lib/db.py` — **all SQLite read/write goes through here**; do not open `data/monitor.db` directly elsewhere
  - `scripts/lib/cli.py` — argparse wiring and `cmd_last`/`cmd_backtest`/`cmd_status` handlers for `monitor.py`
  - `scripts/lib/state.py` — temporal state-smoothing rule (`STATE_CONFIRM_WINDOW`/`RUN`) and `unknown_prefix_to_absorb` for Unknown→Awake absorption
  - `scripts/lib/classifiers.py` — BIRDEYE classifiers: `BabyPresenceClassifier`, `EyeStateClassifier` (MobileNetV3-Small), `TrainableFaceDetector` (MobileNetV3-Small, primary), `FaceDetector` (YuNet ONNX, fallback)
  - `scripts/lib/local_pipeline.py` — 3-stage BIRDEYE orchestration: presence → face detection → eye-state
  - `scripts/lib/training_state.py` — PID-based cross-process training state (CLI/dashboard/cron all coordinate through this)
  - `scripts/lib/config.py` — all constants, paths, thresholds, model chain config, logging setup
  - `scripts/lib/vision.py` — cloud API calls (OpenAI GPT-4o), prompt rendering, response parsing
  - `scripts/lib/detect.py` — pixel-diff empty-bassinet detection
  - `scripts/lib/capture.py` — ffmpeg RTSP frame capture
  - `scripts/lib/alerts.py` — Telegram wake/safety alerts with cooldown logic
  - `scripts/lib/storage.py` — frame retention (oldest-first pruning at 10 GB cap)
  - `scripts/lib/experiments.py` — shadow-experiment framework; every capture tick calls `run_all()` and stores results under `entry["experiments"][<name>]` without touching primary fields
  - `scripts/train_classifiers.py` — retraining with corrections + audit data, writes versioned model dirs
  - `scripts/experiments_backfill.py` — run registered experiments against historical frames so the dashboard has immediate comparison data
  - `scripts/backfill_birdeye_primary.py` — re-run BIRDEYE over a time window and write into the **primary** `eyeState`/face fields (skips `eye_state_edited=1` rows); pair with `backfill_state.py` afterwards so the smoother re-fires
  - `scripts/backfill_state.py` — cheap, re-runnable pass-2 sweep that re-smooths the `state` column over the primary eyeState signal and applies Unknown→Awake absorption for the whole DB
  - `scripts/promote_experiment.py` — atomic flip of a winning shadow experiment to prod (rename old model as `*_legacy` shadow, swap in the new one, optional re-inference)
  - `scripts/bbox_impact.py` — A/B-measures whether dashboard-corrected face bboxes improve eye-state predictions vs the classifier's own crop
  - `scripts/watchdog.py` — independent capture-staleness checker; runs via its own launchd job every 2 min, fires Telegram alert when the newest DB entry is older than `WATCHDOG_ALERT_AFTER_MIN`. State in `data/watchdog-state.json`.
  - `dashboard/app.py` — Flask app; primary surface for frame review, label correction, retraining, model rollback, experiment review, recap video, pipeline history, and per-day eye-state P/R/F1 trend (not just a viewer). The dashboard venv intentionally has no torch/cv2 — any on-device inference goes out through `run_single_inference.py` as a subprocess.
  - `dashboard/system_usage.py` — pure-stdlib snapshot (load avg, memory, disk, baby-monitor process breakdown) backing the dashboard's System Load panel via `/api/system-usage`; also runnable as a CLI (`python dashboard/system_usage.py [--json]`)
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
python scripts/monitor.py --retrain             # retrain with pending corrections (auto-runs post-retrain chain below)
python scripts/monitor.py --retrain --skip-post-retrain               # retrain only; skip auto-backfill/bbox_impact chain
python scripts/monitor.py --retrain --post-retrain-backfill-days 14   # widen the auto primary-backfill window (default: 7 days)
python scripts/monitor.py --backfill-shadow --hours 168 --only-stale  # re-run BIRDEYE on historical frames after deploying a new model and write into the SHADOW AUDIT DICT ONLY (--only-stale skips entries already tagged with the deployed version; supports --limit, --dry-run)
python scripts/backfill_birdeye_primary.py --start 2026-04-02T00:00:00Z  # re-run BIRDEYE and write into the PRIMARY eyeState/face fields for a time window — use this one if you want the temporal state smoother to re-fire over refreshed signal (pair with backfill_state.py afterwards). Skips eye_state_edited=1 rows by default.
python scripts/backfill_state.py  # re-smooth `state` over the primary eyeState signal for the whole DB (cheap, re-runnable)
```

### Training (`scripts/train_classifiers.py`)
```bash
python scripts/train_classifiers.py \
  --sleep-log data/sleep-log.jsonl \
  --frames data/frames/ \
  --face-crops pipeline/output/bootstrap/face_crops/ \
  --corrections data/corrections.jsonl \
  --audit data/audit-log.jsonl
# Train just one classifier (or skip the slow face detector):
python scripts/train_classifiers.py ... --model presence
python scripts/train_classifiers.py ... --model eye-state
python scripts/train_classifiers.py ... --model face-detect
python scripts/train_classifiers.py ... --model all-no-face  # skips face detector (~60 min)
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
launchctl load   ~/Library/LaunchAgents/com.baby-monitor.plist             # capture (every 1 min)
launchctl load   ~/Library/LaunchAgents/com.baby-monitor-dashboard.plist   # dashboard (persistent)
launchctl load   ~/Library/LaunchAgents/com.baby-monitor-watchdog.plist    # capture-staleness watchdog (every 2 min)
launchctl unload <plist>                                                            # to stop
```
Logs land in `skills/baby-monitor/data/system.log`, `cron-stdout.log`, `cron-stderr.log`.

**Do not** create an OpenClaw cron job for monitoring. Monitoring must not depend on Anthropic or any LLM being reachable — launchd is intentional.

**`com.baby-monitor-retrain.plist` exists but is intentionally not loaded.** Retraining is manual-only (see Conventions) — don't `launchctl load` it unless the user explicitly asks to re-enable daily retraining.

## Architecture pointers (read README for the full story)

- **BIRDEYE-primary pipeline** — `frame → pixel-diff → BIRDEYE → cloud API only on BIRDEYE fallback`. BIRDEYE handles ~99% of non-empty frames; the cloud API runs only on `no_face_detected`, `low_confidence`, or hard error. Cost is ~$0.01/day vs ~$1.17/day pre-flip. The flip happened — there is no longer a "shadow mode" where the cloud API is authoritative. The legacy `shadow_birdeye_*` columns and the entry's `shadow` sub-dict are now an immutable model audit trail (what BIRDEYE said for each frame), kept separate from the user-facing primary fields which can be corrected via the dashboard.
- **BIRDEYE is 3-stage** — (1) presence classifier (MobileNetV3-Small, bassinet crop), (2) face detector (finds face bbox), (3) eye-state classifier (MobileNetV3-Small, face crop from bbox). The face detector is a trainable MobileNetV3 (`pipeline/models/face_detector.pt`) loaded as primary, with YuNet ONNX (`pipeline/models/face_detection_yunet_2023mar.onnx`) loaded as a fallback. If both fail, falls back to a head-position crop from the cloud API's last known coordinates.
- **Single inference entry point** — `lib.local_pipeline.run_birdeye_inference(frame_path)` is the **only** function callers should use to run BIRDEYE on a frame. Both `monitor.py` (live capture) and `run_single_inference.py` (dashboard re-run button) go through it, so the two paths cannot drift on what model output looks like or how it maps to storage. `birdeye_result_to_shadow_blob()` is the matching helper for the audit-trail dict shape. Don't call `try_local_analysis` directly from new callers.
- **Edge alert (`check_edge_alert`) is currently disabled** — it reads `entry["bassinetLocation"]` which only the cloud API populates. Post-flip the cloud API runs on ~1% of frames (BIRDEYE fallback path) so the alert effectively dies. Tracked in github issue #3 — a trained `BassinetLocationClassifier` will restore it.
- **Storage** — `data/monitor.db` (SQLite, WAL mode) is primary; `data/sleep-log.jsonl` is an append-only backup. Read paths must use SQLite via `lib/db.py`. Writes are dual-write — keep them in sync.
- **Wake / asleep alerts** — the authoritative transition signal is the smoothed `state` column (see Temporal state smoothing below); the legacy 2-of-3-last-entries check in `alerts.py` is now strictly weaker than the smoother and is kept only as a prior-Asleep gate + cooldown guard before firing. Wake alerts fire on Asleep→Awake and carry feedback buttons; **asleep alerts** (added 2026-04-18, commit `9ad419c`) fire only on Awake→Asleep transitions — placed-already-asleep (Unknown/not_present → Asleep) is intentionally skipped so putdowns don't ping. Don't reintroduce burst-capture (it blocks the pipeline; see Design Decisions in README).
- **Temporal state smoothing (2026-04-14, refined 2026-04-15)** — the primary `state` field is smoothed *at write time* in `monitor.py`, not at read time. Rule in `lib/state.py`: within the last 6 baby-present frames (including the current one), a run of 4 consecutive `eyes_open` → `Awake`, 4 consecutive `eyes_closed` → `Asleep`, otherwise carry forward the previous smoothed state (or `Unknown`). Non-present frames and intermediate eyeState classes (`face_not_visible`, `low_confidence`, cloud-API frames with no `eyeState`) break the run. Thresholds: `STATE_CONFIRM_WINDOW = 6`, `STATE_CONFIRM_RUN = 4` in `lib/config.py`. The unsmoothed per-frame value is preserved in `rawState`. **Unknown → Awake absorption**: after smoothing, if the new state is `Awake`, any immediately-preceding contiguous run of `Unknown`+`babyPresent` frames whose total span is less than `UNKNOWN_ABSORB_MAX_MINUTES` (default 15) is retroactively rewritten to `Awake`. Asymmetric by design — the Unknown → Asleep direction is NOT applied. Helper: `lib/state.py::unknown_prefix_to_absorb`. Live path rewrites historical rows via `db.update_entry` after the current entry is persisted; `scripts/backfill_state.py` applies it as a pass-2 sweep. User `eyeState` corrections still win. The 2-of-3 wake alert check is now strictly weaker than the smoothing rule and is kept only for the prior-Asleep gate + cooldown. **Runtime read paths go through SQLite (`lib/db.py`)**, never `lib.storage.get_recent_entries` / `get_last_entry`. The JSONL reader had a fixed `n*600` byte-budget bug that silently under-returned once entries grew past ~600 bytes, stranding ~24h of in-bassinet time as `Unknown` (incident 2026-04-15). All runtime read callers were migrated to SQLite the same day: `alerts.should_burst` / `check_wake_confirmation`, `detect.detect_empty_bassinet`, the cloud-fallback position heuristic in `monitor.py`, and `dashboard/app.py::api_sleep_stats`. JSONL reads remain only in `train_classifiers.py` and `cli.py` historical tools, where the append-only log is intentional ground truth.
- **Head position** — when the cloud API runs (BIRDEYE fallback path), it returns head coordinates which are stored in `state` (key: `head`) and used by BIRDEYE's adaptive crop on the next tick. Now rare since cloud API runs on ~1% of frames.
- **Model versioning** — `pipeline/models/v_YYYYMMDD_HHMMSS/` with a `latest` symlink, last 20 kept. Each training run writes a `training_runs` row with full metrics; rollback flips the symlink. `run_birdeye_inference` reads the symlink target and tags every result with `shadowModelVersion`.
- **Training state is PID-based** — CLI, dashboard, and cron all coordinate via `lib/training_state.py`. The dashboard's `/api/retrain` rejects starts when one is already running, and `/api/retrain/abort` kills by PID. Don't store training status in process-local globals.
- **Post-retrain chain (auto, since 2026-04-19)** — after a successful `--retrain` (CLI or dashboard), `cmd_retrain` automatically runs: (1) `backfill_birdeye_primary.py --start=<7 days ago>` to refresh primary eyeState/face fields on recent frames (skips `eye_state_edited=1` rows), (2) `backfill_state.py` to re-smooth the derived `state` column, (3) `bbox_impact.py --force` to regenerate Per-class / Bbox-impact numbers against the newly-deployed model. Chain-step failures are logged but non-fatal — the model is already persisted. Opt out with `--skip-post-retrain`; widen the backfill window with `--post-retrain-backfill-days N`. The "manual-only retraining" policy still holds — this chain runs *only* after the user explicitly retrains; nothing here is scheduled.
- **Shadow experiments** — alternative pipeline variants (e.g. larger eye-state crops, alternate thresholds) run alongside prod on every capture via `lib/experiments.py::run_all`. Results land in `entry["experiments"][<name>]` and are strictly read-only observers — a crashing experiment must never abort a tick (wrapped in try/except in `run_all`). Follow the standard result schema (`state`, `eyeState`, `eyeConfidence`, `modelVersion`, `latencyMs`, `ranAt`) so `db.get_experiment_stats` and the dashboard can render any experiment uniformly. Flow: register in `_REGISTRY` → `experiments_backfill.py` for historical coverage → review on dashboard → `promote_experiment.py` to flip.

## Conventions worth knowing

- **Don't commit data**: `.env*`, `data/`, `pipeline/models/`, `pipeline/output/`, `*.pt`, `*.log`, `venv/` are all gitignored. Never `git add -A` blindly.
- **Git push flow** (from `AGENTS.md`): before pushing, update `README.md` to reflect the latest state, then commit with a concise message. Always confirm with the user before pushing.
- **Sensitive file**: `.env.baby-monitor` holds the RTSP URL, OpenAI key, and Telegram bot token. Never echo or commit it.
- **Frame retention**: 7 days / 6 GB cap on `data/frames/`. Don't change retention logic without checking the disk-budget table in README's Design Decisions.
- **Python version**: 3.12+ but ≤ 3.13 (PyTorch constraint).
- **Trash, not rm**: prefer `trash` for deletes; data files may be the only copy of training signal.
- **No tests or linting**: there is no test suite, no linter config, no `pyproject.toml`. Don't waste time looking for them or proposing to add them unless asked.
- **Retraining is manual-only**: the daily retrain cron is disabled. Only retrain when the user explicitly asks — they don't trust cloud API labels as training signal without manual correction first.
