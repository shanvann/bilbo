# BILBO Refactor — Worklog

Single-session refactor on `refactor/docker-package-layout` (2026-05-25).
Source of truth for what changed, why, and what's still pending. Pair
with `CLAUDE.md` (new layout summary) and the project board
[refactor bilbo](https://github.com/users/shanvann/projects/4) (status of
each step).

## Goals

Reshape the repo from an OpenClaw skill (`skills/baby-monitor/...`,
launchd plists, host venvs) into a standalone Python package with a
Docker-Compose runtime. Pre-refactor, the live monitor ran from
`/Users/shanit/.openclaw/workspace/skills/baby-monitor/` via three
launchd jobs. The refactor branch is independent of that tree — none
of the work touched the live system.

Targets, decided up front:

1. Delete OpenClaw / agent artifacts.
2. Move source out of `skills/` into a conventional Python layout
   (`src/bilbo/`).
3. Define a proper API contract between the dashboard and bilbo
   (previously the dashboard reached into bilbo via `sys.path.insert`,
   raw subprocess calls, and direct SQLite/filesystem reads).
4. Replace launchd with 4 Docker services (3 always-on + 1 on-demand
   training container).
5. Keep the dashboard frontend (HTML/JS) byte-identical.

## Strategy (decided with user before any edits)

- **Cutover plan**: refactor on a branch, only cut over when Docker is
  verified. Minimizes downtime to the cutover window itself.
- **Session scope**: all 15 refactor steps end-to-end.
- **Branch**: `refactor/docker-package-layout` off main.
- **Live system**: untouched until the user runs the cutover (still TODO).

## Project board setup

Created 15 issues (`#8`–`#22`) in the
[refactor bilbo](https://github.com/users/shanvann/projects/4) project,
one per refactor step, with Size estimates and Backlog status. Each
commit moves its issue Backlog → In progress → Done on the board.

The board's pre-existing items (#3 / #4 / #6 / #7) are unrelated bug
trackers, kept in Backlog.

## Step-by-step log

Each step is one commit on the branch.

### Step 1 — Delete OpenClaw / agent artifacts (commit `64f694b`, issue #8)

Removed: `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `HEARTBEAT.md`,
`USER.md`, `HANDOFF.md`, `TOOLS.md`, `skills/baby-monitor/SKILL.md`,
`skills/baby-report/SKILL.md`, `skills/baby-monitor/PLAN-face-detection.md`.

`.gitignore`: dropped OpenClaw-only entries (`.openclaw/`, `runs/`,
`context/`, `skills/classifieds-poster/`). Path-rename entries
(`skills/baby-monitor/data/` → `data/`, etc.) were intentionally
left to step 2's commit so the live data path stayed protected at
all times.

### Step 2 — Move tree to `src/bilbo/` (commit `98721d1`, issue #9)

Mass `git mv` from the OpenClaw skill layout to a proper src/ package:

| From | To |
|---|---|
| `skills/baby-monitor/scripts/monitor.py` | `src/bilbo/monitor.py` |
| `skills/baby-monitor/scripts/watchdog.py` | `src/bilbo/watchdog.py` |
| `skills/baby-monitor/scripts/train_classifiers.py` | `src/bilbo/train_classifiers.py` |
| `skills/baby-monitor/scripts/lib/{config,cli,state,alerts,experiments,experiments.json,training_state}.py` | `src/bilbo/<same>.py` |
| `skills/baby-monitor/scripts/lib/{classifiers,local_pipeline,vision,detect,capture}.py` | `src/bilbo/pipeline/<same>.py` |
| `skills/baby-monitor/scripts/lib/db.py` | `src/bilbo/storage/db.py` |
| `skills/baby-monitor/scripts/lib/storage.py` | `src/bilbo/storage/files.py` |
| `skills/baby-monitor/scripts/{backfill_*,bbox_impact,promote_experiment,experiments_backfill,run_single_inference}.py` | `src/bilbo/scripts/<same>.py` |
| `skills/baby-monitor/dashboard/{app.py,static/}` | `dashboard/{app.py,static/}` |
| `skills/baby-monitor/dashboard/{system_usage,aq_analysis}.py` | `src/bilbo/api/{system,air_quality}.py` |
| `skills/baby-monitor/references/` | `references/` |
| `skills/baby-monitor/docs/shadow-to-prod-playbook.md` | `docs/shadow-to-prod-playbook.md` |
| `skills/baby-report/` | `report/` |
| `skills/airgradient-logger/` | `airgradient-logger/` |

Added empty `__init__.py` in each new bilbo subpackage. Deleted empty
`skills/` directory.

All imports rewritten to absolute `bilbo.X` style — `from lib.db import
...` → `from bilbo.storage.db import ...`, etc. The mass rewrite was a
Python regex script over 12 files; first attempt had an f-string bug
(`\.{1,2}` interpreted as a format spec rather than a regex quantifier),
fixed by escaping to `\.{{1,2}}`. Dead `sys.path.insert()` hacks (10
files) removed.

`.gitignore` paths updated atomically in this commit so the live data
directory stayed protected.

### Step 3 — env-var-driven `config.py` (commit `e85a5e5`, issue #10)

Replaced hardcoded `/Users/shanit/.openclaw/workspace/.env.baby-monitor`
and the `Path(__file__).parent.parent.parent` walk-up with env-var-driven
paths:

```
BILBO_ROOT            (default: /app in Docker, repo root on host)
BILBO_DATA_DIR        ($ROOT/data)
BILBO_MODELS_DIR      ($ROOT/pipeline/models)
BILBO_ENV_FILE        ($ROOT/.env)
BILBO_REFERENCES_DIR  ($ROOT/references)
```

`SKILL_DIR` removed from config.py; cli.py + api/system.py updated to use
`BILBO_ROOT`. `RotatingFileHandler` made lazy + best-effort so
`import bilbo.config` doesn't crash when DATA_DIR doesn't exist yet.

Added `.env.example` at repo root (whitelisted through the `.env*`
gitignore pattern with `!.env.example`).

### Step 4 — `bilbo.api.*` Python contract (commit `31a034f`, issue #11)

Extracted business logic out of dashboard/app.py (1881 lines) and into
a Python contract under `src/bilbo/api/`. dashboard/app.py shrank to
339 lines: every route is now a parse-args-and-dispatch shell that
calls `bilbo.api.<group>.<func>` and `jsonify`s the result.

New modules:
- `bilbo.api.entries` — timeline, update_entry, mark_reviewed, run_inference
- `bilbo.api.training` — training_status, retrain, retrain_abort
- `bilbo.api.corrections` — pending_corrections, resolve, discard
- `bilbo.api.stats` — status, sleep_stats, bassinet_daily, sleep_trend,
  feeds, diapers, events, safety_stats, monitor_stats,
  eye_state_daily_metrics, pipeline_history, classification_rate,
  pipeline_health
- `bilbo.api.frames` — get_frame_path (raises FrameForbidden / FrameNotFound)
- `bilbo.api.recap` — recap_generate, recap_video (Path | None)
- `bilbo.api.inference` — run_single (subprocess-then-HTTP wrapper)
- `bilbo.api.air_quality` — api_air_quality aggregator
- `bilbo.api.models` — list_versions, latest_version, rollback (for
  control-api's use, no dashboard route yet)
- `bilbo.api.system` — gather (moved in step 2)

Design conventions used:
- Functions take primitive args (`limit: int`, `since: datetime|None`),
  return primitive dicts/lists. Flask is invisible from inside
  `bilbo.api.*`.
- Non-200 responses set a `_status` key; the dashboard's `_respond`
  helper unpacks it. Lets the api stay Flask-free while still supporting
  404/422/etc.
- `entries.update_entry` uses Ellipsis as the `face_bbox` sentinel to
  preserve the "field omitted vs explicit null" semantic.

Response shapes preserved exactly — `dashboard/static/app.js` is
unchanged.

Delegated the bulk extraction to a subagent with a precise brief
(module mapping + design rules + verification command) and verified
the result; 27 dashboard routes preserved (31 routes including PWA).

### Step 5 — control-api Flask app (commit `be39831`, issue #12)

`src/bilbo/http/app.py` — standalone Flask app on :5556 mirroring
every dashboard data endpoint under `/api/v1/*` by dispatching through
`bilbo.api.*`. 29 routes (28 dashboard data + `/healthz`).

Binary endpoints (`/api/v1/frame`, `/api/v1/recap/video`) use
`send_file` so the reverse proxy passes the streaming response
through unchanged.

Console script `bilbo-control-api` invokes `main()` for local dev;
production runs under gunicorn.

### Step 6 — dashboard reverse proxy (commit `d245b3b`, issue #13)

`dashboard/app.py` collapsed 339 → 118 lines. Serves only static
(`/`, `/sw.js`, `/manifest.webmanifest`, `/static/*`) and
reverse-proxies `/api/*` → `http://control-api:5556/api/v1/*`. No
bilbo imports, no business logic.

Proxy details:
- Hop-by-hop headers (RFC 7230) stripped both directions.
- `/api/frame` and `/api/recap/video` streamed via
  `httpx.Client.stream()` so 206 Partial Content from the `<video>`
  Range requests round-trips correctly and memory stays flat.
- `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` read from env
  (compose env_file) instead of the previous hardcoded
  `/Users/shanit/.openclaw/workspace/.env.dashboard` file. The service
  worker patching behavior is preserved so the installed PWA keeps
  working when the Cloudflare Access SSO cookie expires.

`dashboard/requirements.txt`: flask + httpx + gunicorn only.

### Step 7 — `--loop` mode (commit `56099cb`, issue #14)

monitor was a one-shot the launchd plist kicked every minute. For
Docker the capture container needs to stay alive. Added `--loop`:

- Extracted the existing pipeline body into
  `_run_pipeline_tick(args, env, rtsp_url, api_key, anthropic_key, *, t_start)`.
- New `--loop` flag (argparse in cli.py) wraps it in `while True:`
  with a 60-second cadence (sleeps to the next minute boundary,
  swallows per-tick errors).
- After each tick calls `bilbo.pipeline.local_pipeline.maybe_reload_classifiers()`,
  which compares the cached `_loaded_model_version` against the
  `latest` symlink and drops the classifier singletons if they differ.
  Retrains take effect within the next minute without a container
  restart.

Same pattern for watchdog (`bilbo-watchdog --loop --interval 120`).
`watchdog.run_loop` is importable so the capture service can run it
as a background thread.

### Step 8 — `capture_service.py` (commit `40876bd`, issue #15)

`bilbo.capture_service` — the capture container's main entry point.
Flask on :5557 that:

1. Runs `bilbo.monitor.main()` with `--loop` in a daemon thread.
2. Runs `bilbo.watchdog.run_loop()` in another daemon thread.
3. Exposes `POST /infer` for the dashboard's ad-hoc inference button
   (reuses the warm torch model instead of cold-loading in a subprocess).
4. Exposes `GET /healthz`.

Refactored `bilbo.scripts.run_single_inference` to expose a
`run(timestamp) -> dict` callable in addition to its CLI entry, so
both the capture-service handler and the `python -m
bilbo.scripts.run_single_inference <ts>` path go through the same
code.

`bilbo.api.inference.run_single()` switched from subprocess to
`httpx.post(BILBO_CAPTURE_URL/infer)`. The :5557 listener is
internal to the compose network; only control-api talks to it.

### Step 9 — Docker-based training state (commit `8a1f78a`, issue #16)

Replaced `bilbo.training_state`'s PID coordination with a Docker-SDK
control plane:

```
start(args, trigger)  → docker.containers.run("bilbo:latest", ...)
abort()               → container.stop()
is_running()          → "bilbo-training" container in status=running
get_status()          → container.attrs while alive, persisted summary after
```

The `bilbo-training` container is on-demand: control-api spawns it via
the mounted Docker socket when the dashboard's Retrain button is
clicked. auto_remove cleans it up on exit. The new versioned model
dir + the `latest` symlink flip happen on the shared
`pipeline/models/` volume; capture picks it up on the next tick via
`maybe_reload_classifiers()`.

Legacy `mark_started` / `mark_subprocess_started` / `mark_completed`
helpers kept for the host-dev path where someone shells out
`bilbo-monitor --retrain` without Docker. `is_running` / `get_status`
check both signals (container OR PID) so the dashboard panel renders
consistently.

`bilbo.api.training.retrain()` collapsed to a single
`training_state.start()` call. Added `train_main()` in cli.py as the
`bilbo-train` console-script entry point — argparse wrapper around
the existing `cmd_retrain`, which is what the training container runs
as its command.

### Step 10 — pyproject.toml (commit `52ebd7e`, issue #17)

Replaced flat `requirements.txt` with `pyproject.toml` using the
hatchling backend and src/ layout. Extras keep each Docker image
slim:

- `bilbo` — requests, python-dotenv, certifi (always)
- `bilbo[ml]` — torch, torchvision, opencv, sklearn, openai, Pillow,
  pyyaml, ultralytics (capture + training)
- `bilbo[control-api]` — flask, gunicorn, docker, httpx (no torch)
- `bilbo[capture]` — ml + flask + gunicorn + httpx

Console scripts wired:
- `bilbo-monitor` → `bilbo.monitor:main`
- `bilbo-watchdog` → `bilbo.watchdog:main`
- `bilbo-train` → `bilbo.cli:train_main`
- `bilbo-control-api` → `bilbo.http.app:main`
- `bilbo-capture` → `bilbo.capture_service:main`
- `bilbo-inference` → `bilbo.scripts.run_single_inference:main`
- `bilbo-backfill-state`, `bilbo-backfill-primary`, `bilbo-bbox-impact`,
  `bilbo-promote-experiment`, `bilbo-experiments-backfill`

torch / torchvision pinned to 2.1–2.7 so linux/arm64 wheels resolve
cleanly under Apple Silicon and amd64 builds without falling back to
a CPU-only sdist build.

### Step 11 — Dockerfiles (commit `72de8ff`, issue #18)

Two images:

**`deploy/Dockerfile`** — python:3.12-slim + apt ffmpeg, libgl1,
libglib2.0-0 (opencv runtime). Takes a `BILBO_EXTRAS` build arg so
compose can build one variant per service.

**`deploy/Dockerfile.dashboard`** — minimal frontend image (flask,
httpx, gunicorn from `dashboard/requirements.txt`). No bilbo install,
no torch.

`PYTHONUNBUFFERED=1` so `docker logs` sees prints immediately.
`ENV BILBO_ROOT=/app` feeds into config.py's path defaults.

Added `dashboard/__init__.py` so the package can be invoked as
`python -m dashboard.app` (smoke-test default; compose uses gunicorn).

### Step 12 — docker-compose.yml (commit `c40d409`, issue #19)

Three services, one image variant each:

- **capture** — host networking for RTSP, runs `bilbo-capture`
  (monitor loop + watchdog thread + POST /infer on :5557 internal)
- **control-api** — gunicorn on :5556, mounts `/var/run/docker.sock`
  so it can spawn the training container, reads data via shared
  volume, models read-only
- **dashboard** — lightweight image, gunicorn on :5555,
  reverse-proxies `/api/*` to control-api

The **training** service is intentionally not in compose — control-api
spawns it on demand via Docker socket using `bilbo:latest` +
auto_remove. New model versions land on the shared `pipeline/models/`
volume; capture picks them up via `maybe_reload_classifiers()`.

Networking subtleties baked in:
- capture is on `network_mode: host` (RTSP-friendly), so control-api
  can't reach it as `capture:5557`. `BILBO_CAPTURE_URL` points at
  `host.docker.internal:5557`; `extra_hosts` gives Linux hosts the
  same resolution Docker Desktop provides natively.
- `BILBO_HOST_DATA` / `BILBO_HOST_MODELS` use `${PWD}` so the training
  container (which the host's docker daemon starts, not control-api's
  filesystem) bind-mounts the right host paths.

### Step 14 — Docs (commit `41492de`, issue #21)

(Step 13, the launchd cutover, is intentionally deferred — destructive
on the live monitor; user-triggered after smoke-test passed.)

- `CLAUDE.md` — rewritten Layout section, Common commands split into
  Docker (production) and host-dev (`pip install -e .[...]`),
  architecture pointers updated for Docker-container training state,
  `--loop` mode, `maybe_reload_classifiers()`, and the layered
  bilbo.api / control-api / capture-service architecture.
- `README.md` — Software Setup is now Docker-Compose-driven.
  Architecture diagram updated. "Baby Monitor (`src/bilbo/`)" key-files
  table rewritten against new module paths. Capture Watchdog Decision
  table now says "Capture-container background thread (chosen)".
  Airgradient logger + Baby Report sections re-pathed.
- `.claude/settings.json` — hook matcher checks `src/bilbo/`,
  `references/`, `dashboard/` instead of `skills/baby-monitor/scripts/`.

### Step 15 — Smoke test (commit `a448069`, issue #22)

Built and brought up the 3-container stack on shifted ports
(5560/5561; capture stays on host-net :5557) alongside the live
launchd monitor. Six bugs caught:

1. **Dockerfile** — `pip install -e .` failed because slim image's
   hatchling is too old for `prepare_metadata_for_build_editable`.
   Bumped pip + hatchling, dropped `-e` (containers bake source).
2. **Dockerfile** — `OSError: Readme file does not exist: README.md`
   because pyproject declares `readme = "README.md"` but the
   Dockerfile didn't COPY it.
3. **monitor.py** — `NameError: name 'sys' is not defined`. Step 2's
   sys.path cleanup over-removed the import; sys.version + sys.exit
   are still used.
4. **watchdog.py** — `sqlite3.OperationalError: no such table: entries`
   on a fresh data volume. The bare `get_last_entry()` skips schema
   init; forced `get_db()` first.
5. **config.load_env()** — now falls back to `os.environ` when
   `ENV_FILE` is missing, with a fixed list of known keys. Containers
   that get secrets via compose `env_file:` (no bind-mounted `.env`)
   load cleanly.
6. **.env.example** — variable names matched live env file
   (`RTSP_STREAM_URL`, not `RTSP_URL`; added `ANTHROPIC_API_KEY` +
   `CF_ACCESS_*`).

Added `deploy/docker-compose.smoke.yml` for parallel topology, with a
note on the macOS Docker Desktop quirk: capture's `network_mode: host`
binds inside the Linux VM (192.168.65.3), not the macOS host.
`host.docker.internal` resolves to a different gateway alias
(192.168.65.254) on macOS. The smoke override uses the VM IP; Linux
production keeps `host.docker.internal` + `extra_hosts: host-gateway`
which works correctly there.

Validated end-to-end:

- ✅ 3 images built (bilbo 7.0 GB, control-api 600 MB, dashboard 139 MB)
- ✅ All 3 services come up healthy
- ✅ Dashboard serves static (HTTP 200) and reverse-proxies `/api/*`
- ✅ control-api answers `/healthz`, `/api/v1/status`,
  `/api/v1/training-status`
- ✅ Capture's `--loop` mode runs; ffmpeg grabbed an RTSP frame in 3 s
- ✅ Pipeline runs pixel-diff → BIRDEYE → cloud fallback. BIRDEYE
  bailed because no model weights in the fresh data volume (expected);
  cloud API returned 429 (your known OpenAI-quota issue #6, expected).
- ✅ Watchdog handles empty DB gracefully
- ✅ End-to-end dashboard → control-api → capture `/infer` round-trips
  (404 because the timestamp doesn't exist in the empty DB)
- ✅ Live launchd monitor undisturbed throughout

Untested (would need real data):
- BIRDEYE with real model weights
- Retrain container spawn flow (needs corrections)
- Telegram alert delivery

Smoke stack torn down after validation; live monitor still running.

## Outstanding work

### Step 13 — Cutover (issue [#20](https://github.com/shanvann/bilbo/issues/20), Ready)

Destructive. User-triggered. Sequence:

1. `launchctl list | grep baby` — confirm the 3 live services are still
   running. Today: `com.baby-monitor` (1-min capture),
   `com.baby-monitor-dashboard` (persistent), `com.baby-monitor-watchdog`
   (2-min).
2. Copy live data + models from the OpenClaw workspace:
   ```bash
   cp -R /Users/shanit/.openclaw/workspace/skills/baby-monitor/data       ./data
   cp -R /Users/shanit/.openclaw/workspace/skills/baby-monitor/pipeline   ./pipeline
   ```
   (Or symlink if you want to keep the OpenClaw paths as the canonical
   on-disk location during transition.)
3. `cp .env.example .env && $EDITOR .env` if not already done.
4. `docker compose -f deploy/docker-compose.yml up -d --build`.
5. Verify a fresh entry within 60 s:
   ```bash
   sleep 70 && docker compose -f deploy/docker-compose.yml exec capture \
       sqlite3 /app/data/monitor.db "SELECT MAX(timestamp) FROM entries"
   ```
6. Verify dashboard renders + Telegram alert flow.
7. Stop the live launchd services:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.baby-monitor.plist
   launchctl unload ~/Library/LaunchAgents/com.baby-monitor-dashboard.plist
   launchctl unload ~/Library/LaunchAgents/com.baby-monitor-watchdog.plist
   rm ~/Library/LaunchAgents/com.baby-monitor*.plist
   ```

### Security — rotate exposed credentials

During the smoke test, `docker compose config` rendered the env file's
contents (plaintext) into a tool-output buffer in this session. Rotate
these post-cutover:

- **OpenAI API key** (was exposed)
- **Telegram bot token** (was exposed)
- **Cloudflare Access service token** (was exposed)
- ANTHROPIC_API_KEY (was exposed but probably unused — the cloud
  chain is openai-only today)

The RTSP URL contains an embedded camera password — rotate the camera
credential too.

## Commit history (oldest → newest)

```
64f694b  refactor(step 1):  delete OpenClaw / agent runtime artifacts
98721d1  refactor(step 2):  move source tree to src/bilbo + top-level layout
e85a5e5  refactor(step 3):  env-var-driven config paths + .env.example
31a034f  refactor(step 4):  extract dashboard route logic into bilbo.api.*
be39831  refactor(step 5):  control-api Flask app under bilbo.http.app
d245b3b  refactor(step 6):  strip dashboard/app.py to static + reverse proxy
56099cb  refactor(step 7):  --loop mode for monitor + watchdog, hot reload
40876bd  refactor(step 8):  bilbo.capture_service — POST /infer + healthz
8a1f78a  refactor(step 9):  training_state uses Docker container as source of truth
52ebd7e  refactor(step 10): pyproject.toml with extras + console scripts
72de8ff  refactor(step 11): deploy/Dockerfile + deploy/Dockerfile.dashboard
c40d409  refactor(step 12): deploy/docker-compose.yml — 3 always-on services
41492de  refactor(step 14): rewrite CLAUDE.md, update README + .claude/settings.json
a448069  refactor(step 15): smoke-test fixes — six bugs caught by Docker run
```

14 commits, ~3,400 lines of code moved + ~800 lines added (Docker +
control-api + dashboard proxy) + ~2,400 lines extracted from
dashboard/app.py into bilbo.api.*.

## What I'd do differently next time

- **Run the smoke test earlier.** I built all 15 steps before validating
  any of them. The first three smoke-test bugs (Dockerfile hatchling,
  missing README, NameError on sys) would have been caught immediately
  if I'd attempted `docker compose build` after step 11.
- **Add a single-tick `docker compose run --rm capture bilbo-monitor`
  smoke at the end of step 7 / step 8** — would have caught the missing
  `sys` import and the empty-DB watchdog crash before they showed up
  as container-startup errors.
- **The bilbo.api extraction was delegated to a subagent**, which saved
  context but meant I had to re-read its output carefully. A smaller
  human-driven extraction (one module at a time) would have given me
  better intuition for the contract.
