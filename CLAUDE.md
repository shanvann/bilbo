# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

BILBO — a baby bassinet monitor. A Python pipeline captures RTSP frames every minute, runs BIRDEYE (MobileNetV3 cascade) on-device as the primary decider, falls back to GPT-4o as a cloud backstop on ~1% of frames, dual-writes to Postgres + JSONL, and serves a Flask dashboard for review/correction/retraining.

`README.md` is the reference for the DB schema and the design-decision tradeoffs. Read it before touching the DB or revisiting a past tradeoff. This file is the fastest summary.

The repo is packaged as `pip install -e .` (see `pyproject.toml`) and runs as a 4-container Docker stack (see `deploy/`). The previous launchd setup is gone.

## Layout

```
bilbo/
├── pyproject.toml                  # bilbo + extras [ml] [control-api] [capture]
├── .env.example                    # secrets template (RTSP, OpenAI, Telegram)
├── src/bilbo/                      # the main package
│   ├── config.py                   # env-var-driven paths (BILBO_ROOT, BILBO_DATA_DIR, ...)
│   ├── monitor.py                  # per-tick pipeline (--loop runs forever)
│   ├── watchdog.py                 # capture-staleness Telegram alert (--loop)
│   ├── train_classifiers.py        # retraining
│   ├── capture_service.py          # Flask :5557 — POST /infer + monitor+watchdog threads
│   ├── training_state.py           # Docker-SDK control plane for the training container
│   ├── state.py, alerts.py, experiments.py, cli.py
│   ├── pipeline/                   # capture, classifiers, local_pipeline, vision, detect
│   ├── storage/                    # db.py (Postgres, single source of truth), files.py (JSONL)
│   ├── api/                        # the bilbo.api.* Python contract
│   │   ├── entries, training, corrections, frames, stats, recap,
│   │   ├── inference (POSTs to capture:5557/infer)
│   │   ├── system, air_quality, models
│   ├── http/app.py                 # control-api Flask on :5556 — mounts bilbo.api under /api/v1/*
│   └── scripts/                    # backfill_*, bbox_impact, promote_experiment, etc.
├── dashboard/                      # frontend container only
│   ├── app.py                      # ~120 lines: static + /api/* reverse proxy → control-api
│   ├── requirements.txt            # flask + httpx + gunicorn (no bilbo, no torch)
│   └── static/                     # index.html, app.js, style.css, sw.js, manifest, icons
├── deploy/
│   ├── Dockerfile                  # bilbo image (BILBO_EXTRAS build arg)
│   ├── Dockerfile.dashboard        # lightweight dashboard image
│   └── docker-compose.yml          # 4 always-on services (postgres + capture + control-api + dashboard)
├── report/                         # sibling skill, untouched by the refactor
├── airgradient-logger/             # sibling skill, untouched (has own Dockerfile)
├── references/                     # prompt.md + baby-profile.md (ships in the image)
├── docs/                           # bilbo.png hero + shadow-to-prod-playbook.md
├── data/                           # gitignored: JSONL, frames, alert state, logs (DB now lives in the postgres named volume)
└── pipeline/                       # gitignored: models/v_*/* + the `latest` symlink
```

The pre-refactor layout was `skills/baby-monitor/` etc.; everything that used to live under `skills/baby-monitor/scripts/lib/X.py` is now at `src/bilbo/X.py` (with `pipeline/`, `storage/`, `api/` subpackages). Imports are absolute (`from bilbo.storage.db import get_db`).

## Common commands

### Docker (production)

```bash
cp .env.example .env && $EDITOR .env             # fill in RTSP, OpenAI, Telegram
docker compose -f deploy/docker-compose.yml up -d --build    # bring stack up
docker compose -f deploy/docker-compose.yml logs -f capture  # tail capture
docker compose -f deploy/docker-compose.yml exec capture bilbo-monitor --status
docker compose -f deploy/docker-compose.yml down             # stop everything
```

Stack: `postgres` (the single-source-of-truth DB, named volume `pgdata`), `capture` (RTSP + BIRDEYE + watchdog + POST /infer), `control-api` (Flask :5556), `dashboard` (Flask :5555 → reverse proxy). The `training` container is on-demand: control-api spawns it via the mounted Docker socket when the dashboard's Retrain button is clicked — it joins the `bilbo-net` network and inherits `DATABASE_URL` so it can reach `postgres`.

### Local (host) development

After `pip install -e ".[ml,control-api,capture]"` (you probably want all three for dev):

```bash
bilbo-monitor                          # one tick (default)
bilbo-monitor --loop                   # persistent (Docker mode)
bilbo-monitor --dry-run                # tick without writing to DB/JSONL
bilbo-monitor --capture-only           # grab one frame and exit
bilbo-monitor --analyze FRAME          # re-run cloud API on an existing frame
bilbo-monitor --status                 # health: gaps, disk, recent stats
bilbo-monitor --last 20                # tail recent entries
bilbo-monitor --backtest --birdeye     # birdeye accuracy vs cloud ground truth
bilbo-monitor --audit --sample 50      # spot-check shadow vs prod disagreements
bilbo-monitor --list-models            # versioned model history + metrics
bilbo-monitor --rollback VERSION       # revert to a previous model
bilbo-monitor --retrain                # local in-process retrain (Docker uses bilbo-train)

bilbo-watchdog --loop --interval 120   # standalone watchdog (otherwise capture_service runs it as a thread)

bilbo-train --skip-face-detect         # what the docker training container runs
bilbo-control-api                      # dev mode for the REST API
bilbo-capture                          # dev mode for capture + watchdog threads + /infer

bilbo-inference 2026-05-25T15:00:00Z   # ad-hoc inference (also python -m bilbo.scripts.run_single_inference)
bilbo-backfill-state                   # re-smooth `state` over the whole DB
bilbo-backfill-primary --start 2026-04-02T00:00:00Z
bilbo-bbox-impact --force
bilbo-experiments-backfill --name eye_state_448 --force
bilbo-promote-experiment ...
```

### Reports
```bash
cd report
python scripts/report.py --range 24h                   # full report
python scripts/report.py --section monitor             # model performance only
python scripts/report.py --range 1h --section monitor  # quick post-deploy check
```

## Architecture pointers (read README for the full story)

- **BIRDEYE-primary pipeline** — `frame → pixel-diff → BIRDEYE → cloud API only on BIRDEYE fallback`. BIRDEYE handles ~99% of non-empty frames; the cloud API runs only on `no_face_detected`, `low_confidence`, or hard error. Cost is ~$0.01/day vs ~$1.17/day pre-flip. The legacy `shadow_birdeye_*` columns and the entry's `shadow` sub-dict are an immutable audit trail of what BIRDEYE said, kept separate from the user-facing primary fields which can be corrected via the dashboard.
- **BIRDEYE is 3-stage (+ a 2.5 retry)** — (1) presence classifier (MobileNetV3-Small, bassinet crop), (2) face detector (trainable MobileNetV3 primary, YuNet ONNX fallback), (3) eye-state classifier (MobileNetV3-Small, face crop from bbox). **Stage 2.5:** if both detectors miss on the full bassinet crop, retry them on a tighter `HEAD_CROP_SIZE` crop centered on the last-known head position (`crop_head_region_in_bassinet`), then translate the bbox back to bassinet-crop coords (`_translate_to_bassinet_coords`); only if that also misses does it fall through to `no_face_detected` → cloud. The head-position prior in `data/head-state.json` is now kept warm by BIRDEYE itself — every successful Stage-3 detection writes it back with `source="birdeye"` (the cloud API still writes it with `source="cloud-api"` on its ~1% of frames). Coord transforms: `full_frame_to_bassinet_coords` / `bassinet_to_full_frame_coords` in `pipeline/classifiers.py`.
- **Single inference entry point** — `bilbo.pipeline.local_pipeline.run_birdeye_inference(frame_path)` is the **only** function callers should use. Both `bilbo.monitor` (live capture) and `bilbo.scripts.run_single_inference` (dashboard re-run) go through it. `birdeye_result_to_shadow_blob()` is the matching helper for the audit-trail dict shape. Don't call `try_local_analysis` directly.
- **Hot reload** — `bilbo.pipeline.local_pipeline.maybe_reload_classifiers()` is called once per tick by `bilbo-monitor --loop`; it compares the cached `_loaded_model_version` (from `pipeline/models/latest`) against the current readlink and drops the singletons when the symlink flips. A retrain takes effect within the next minute without restarting the capture container.
- **Edge alert (`check_edge_alert`) is currently disabled** — it reads `entry["bassinetLocation"]` which only the cloud API populates. Post-flip the cloud API runs on ~1% of frames so the alert effectively dies. Tracked in github issue #3 — a trained `BassinetLocationClassifier` will restore it.
- **Storage** — **Postgres** (named via `DATABASE_URL`, the `postgres` compose service) is primary; `data/sleep-log.jsonl` is an append-only backup. Read paths must go through `bilbo.storage.db` (psycopg3, one autocommit thread-local connection with `dict_row`; placeholders are `%s`). Writes are dual-write. The previous SQLite-over-bind-mount DB corrupted twice under concurrent multi-container access — Postgres eliminates that failure mode. One-time SQLite→Postgres loader: `bilbo-migrate-sqlite-to-pg --sqlite <recovered.db>`. **Exception:** `bilbo.api.air_quality` still reads the *AirGradient logger's* separate SQLite DB read-only (`AIRGRADIENT_DB_PATH`) — that one is single-writer and stays SQLite.
- **Wake / asleep alerts** — the authoritative transition signal is the smoothed `state` column; the legacy 2-of-3-last-entries check in `alerts.py` is strictly weaker than the smoother and is kept only as a prior-Asleep gate + cooldown guard before firing. Wake alerts fire on Asleep→Awake (with feedback buttons); asleep alerts fire only on Awake→Asleep — placed-already-asleep (Unknown/not_present → Asleep) is intentionally skipped so putdowns don't ping.
- **Temporal state smoothing** — the primary `state` field is smoothed at write time in `monitor.py`. Within the last 6 baby-present frames, a run of 4 consecutive `eyes_open` → `Awake`, 4 consecutive `eyes_closed` → `Asleep`, otherwise carry forward (or `Unknown`). Thresholds in `bilbo.config.STATE_CONFIRM_WINDOW` / `STATE_CONFIRM_RUN`. The unsmoothed per-frame value is preserved in `rawState`. **Unknown → Awake absorption** (helper `bilbo.state.unknown_prefix_to_absorb`) and **putdown-pattern absorption → FallingAsleep** (helper `bilbo.state.putdown_prefix_to_absorb`) run in the live path and as pass-3 of `bilbo-backfill-state`.
- **Model versioning** — `pipeline/models/v_YYYYMMDD_HHMMSS/` with a `latest` symlink, last 20 kept. Each training run writes a `training_runs` row with full metrics; rollback flips the symlink. `run_birdeye_inference` reads the symlink target and tags every result with `shadowModelVersion`.
- **Training state is Docker-container-based** — `bilbo.training_state` queries Docker for a container named `bilbo-training` to determine if a run is in progress. The dashboard's `/api/retrain` (via control-api → `bilbo.api.training.retrain`) spawns it through the Docker socket. Legacy PID-based fallback exists for the host-dev path where someone runs `bilbo-monitor --retrain` directly.
- **Post-retrain chain (auto)** — after a successful `bilbo-train` or `bilbo-monitor --retrain`, `cmd_retrain` automatically runs: (1) `bilbo-backfill-primary --start=<7 days ago>` to refresh primary eyeState/face fields (skips `eye_state_edited=1` rows), (2) `bilbo-backfill-state` to re-smooth the derived `state` column, (3) `bilbo-bbox-impact --force` to regenerate Per-class / Bbox-impact numbers. Chain-step failures are logged but non-fatal. Opt out with `--skip-post-retrain`; widen the backfill window with `--post-retrain-backfill-days N`.
- **Shadow experiments** — alternative pipeline variants run alongside prod on every capture via `bilbo.experiments.run_all`. Results land in `entry["experiments"][<name>]` and are strictly read-only observers — a crashing experiment must never abort a tick (wrapped in try/except). Standard result schema: `state`, `eyeState`, `eyeConfidence`, `modelVersion`, `latencyMs`, `ranAt`. Flow: register in `_REGISTRY` → `bilbo-experiments-backfill` for history → review on dashboard → `bilbo-promote-experiment` to flip.

## Layered API contract

```
┌────────────────────────────────────────────────┐
│  HTTP API (control-api → :5556/api/v1/*)        │  ← dashboard talks to this
├────────────────────────────────────────────────┤
│  bilbo.api.*  (Python module)                   │  ← control-api, capture
│  entries, training, corrections, frames,        │     internally
│  models, stats, inference, system, recap,       │
│  air_quality                                    │
├────────────────────────────────────────────────┤
│  bilbo.storage.*, bilbo.pipeline.*, ...         │  ← strictly internal
└────────────────────────────────────────────────┘
```

Rules:
1. The dashboard imports nothing from bilbo — pure proxy.
2. control-api, capture, and training import only from `bilbo.api.*` for cross-domain operations. They may import `bilbo.pipeline.*` for things that are their direct job (capture does inference, training trains).
3. `bilbo.storage.*` and `bilbo.pipeline.*` are private. New callers must go through `bilbo.api`.
4. The HTTP API mirrors `bilbo.api.*` 1:1.

## Conventions worth knowing

- **Don't commit data**: `.env*` (except `.env.example`), `data/`, `pipeline/models/`, `pipeline/output/`, `*.pt`, `*.onnx`, `*.log`, `venv/`, `.venv/` are all gitignored. Never `git add -A` blindly.
- **Git push flow**: before pushing, update `README.md` to reflect the latest state, then commit with a concise message. Always confirm with the user before pushing.
- **Sensitive file**: `.env` holds the RTSP URL, OpenAI key, and Telegram bot token. Never echo or commit it. Copy from `.env.example`.
- **Frame retention**: 10 GB cap on `data/frames/` (~17 days at 1-min intervals; oldest-first pruning). Don't change retention logic without checking the disk-budget table in README's Design Decisions.
- **Python version**: 3.12+ but ≤ 3.13 (PyTorch constraint, encoded in pyproject.toml).
- **Trash, not rm**: prefer `trash` for deletes; data files may be the only copy of training signal.
- **No tests or linting**: there is no test suite, no linter config. Don't waste time looking or proposing to add unless asked.
- **Retraining is manual-only**: there is no scheduled retrain. Only retrain when the user explicitly asks — they don't trust cloud API labels as training signal without manual correction first.
