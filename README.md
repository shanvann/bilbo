# BILBO — Baby Intelligent Lookout & Behavior Observer

![BILBO — Baby Intelligent Lookout & Behavior Observer](docs/bilbo.png)

A baby bassinet monitor that captures a frame every minute from an IP
camera, classifies sleep state on-device with a 3-stage MobileNetV3
cascade (BIRDEYE), and falls back to a cloud API (GPT-4o) only when
the local cascade can't see a face (~1% of non-empty frames). Includes
a Flask dashboard for frame review, label correction, model
retraining, and performance tracking. Runs as a 4-container Docker
stack.

## What It Does

- **Tracks sleep state** — Asleep, Awake, FallingAsleep, Unknown — via on-device **BIRDEYE** (3-stage MobileNetV3-Small cascade: presence → face detection → eye-state). A cloud API (GPT-4o) is called only when BIRDEYE can't find a face or has low confidence (~1–2% of non-empty frames). The `shadow` sub-dict and `shadow_birdeye_*` columns are an immutable audit trail of what the model said per frame, separate from the user-correctable primary fields.
- **Detects wake-ups and sleep-onset** — confirms by checking the last 3 entries (2-of-3 agreeing), then sends a Telegram alert. Wake alerts include feedback buttons; asleep alerts fire only on awake→asleep transitions (skipped on placed-already-asleep).
- **Capture watchdog** — runs as a background thread inside the capture container (every 2 min) and pings Telegram if no new frame has been written in `WATCHDOG_ALERT_AFTER_MIN` minutes. Catches RTSP outages, monitor crashes, container restarts.
- **Safety alerts** — immediate notification if baby is pressed against the bassinet side.
- **Dashboard** — live camera feed, timeline, frame-by-frame review with eye state correction, block-level labeling, model performance metrics, training stats, retrain button.
- **Continuous improvement** — corrections from the dashboard feed retraining. Model versions are tracked with metrics, rollback support, and post-retrain re-inference. See [docs/training.md](docs/training.md).
- **SQLite storage** — indexed queries; JSONL kept as append-only backup. See [docs/database-schema.md](docs/database-schema.md).

> **Note:** The camera only monitors the bassinet. Sleep elsewhere (stroller, arms, car seat) is not captured.

## Architecture

BIRDEYE-primary since 2026-04-12 (commit `7250067`). Before that, the cloud API was production and BIRDEYE ran as a shadow for validation; see [Shadow vs Production (historical)](docs/design-decisions.md#shadow-vs-production-historical) for the rationale behind the flip.

```
capture container (--loop, 60s) → capture frame (ffmpeg)
  → Pixel-diff: empty bassinet? → store state=not_present, skip BIRDEYE
  → BIRDEYE (3-stage cascade on CPU, ~130 ms):
       presence → face detector → eye-state
  → If BIRDEYE bails (no_face_detected / low_confidence / hard error):
       Cloud API fallback (GPT-4o) writes primary fields; BIRDEYE result
       still stored as the `shadow` audit dict.
       If the cloud API itself fails: degrade to BIRDEYE primary, set
       `cloudUnavailable=true`, promote a `low_confidence` `eyeState`
       to a real `Awake`/`Asleep` rawState — the 4-of-6 temporal smoother
       still gates the smoothed `state`.
  → Dual-write: SQLite (primary) + JSONL (append-only backup)
  → Temporal smoothing: 4-of-6 consecutive eyes_open/closed → Awake/Asleep
  → Unknown → Awake absorption (<15 min run before a confirmed Awake)
  → FallingAsleep putdown-pattern absorption
  → Wake/Asleep alerts: 2/3 of last 3 entries match + prior opposite
       state + 30-min cooldown → Telegram alert.

watchdog thread (every 2 min, in capture container)
  → newest DB timestamp older than WATCHDOG_ALERT_AFTER_MIN? → Telegram alert
  → recovered after outage? → Telegram "captures resumed" ping
```

Detailed rationale for every step lives in [docs/design-decisions.md](docs/design-decisions.md).

## 4-container layout

```
┌──────────────┐   HTTP /api/*    ┌────────────────┐
│  dashboard   │ ───────────────▶ │   control-api  │
│   :5555      │   reverse proxy  │     :5556      │
└──────────────┘                  └────────┬───────┘
                                           │
                          ┌────────────────┼─────────────────┐
                          │                │                 │
                          ▼                ▼                 ▼
              ┌─────────────────┐    shared volumes    spawns via
              │    capture      │    data/             /var/run/docker.sock
              │  bilbo-capture  │    pipeline/models/        │
              │  --loop         │                            ▼
              │  + watchdog     │             ┌─────────────────────┐
              │  + POST /infer  │             │     training        │
              │  :5557 internal │             │  bilbo-train ...    │
              │  (torch + cv2)  │             │  one-shot, auto_rm  │
              └─────────────────┘             └─────────────────────┘
```

`capture` runs on host networking for RTSP; `control-api` and
`dashboard` are bridged. The `training` service is on-demand —
spawned by control-api via the Docker socket when the dashboard's
"Retrain" button is clicked, auto-removed when done.

## Hardware

1. IP security camera with RTSP support — e.g., TP-Link Tapo C100/C200 (~$25–40)
2. Gooseneck microphone stand with clamp — clamp to bassinet frame (~$15–20)
3. A machine with Docker (Mac with Docker Desktop, or any Linux box)

Mount camera on gooseneck → aim down at bassinet → connect to Wi-Fi → note RTSP URL.

## Software setup

### Prerequisites

- Docker + Docker Compose
- An IP camera reachable over RTSP from the host
- (Optional, for host-dev) Python 3.12+ (PyTorch requires ≤3.13) and ffmpeg

### Secrets

Copy `.env.example` to `.env` at the repo root and fill in:

```bash
RTSP_STREAM_URL="rtsp://username:password@192.168.x.x/stream1"
OPENAI_API_KEY="sk-..."
TELEGRAM_BOT_TOKEN="123456:ABC..."
TELEGRAM_CHAT_ID="123456789"
```

### Pretrained model weights

The BIRDEYE cascade needs pretrained weights. The bootstrap script
fetches the latest from [Shanit/BIRDEYE on Hugging Face](https://huggingface.co/Shanit/BIRDEYE)
and points `pipeline/models/latest` at them:

```bash
./scripts/bootstrap-models.sh
```

These weights are trained on the maintainer's specific bassinet —
they'll run on yours, but accuracy will improve as you retrain via
the dashboard's correction loop (see [docs/training.md](docs/training.md)).

### Start the stack

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Three always-on containers (`capture`, `control-api`, `dashboard`) come
up; the dashboard is at `http://localhost:5555`. The training container
is on-demand — control-api spawns it via the mounted Docker socket
when the dashboard's "Retrain" button is clicked.

Operational commands:

```bash
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f capture
docker compose -f deploy/docker-compose.yml exec capture bilbo-monitor --status
docker compose -f deploy/docker-compose.yml down
```

For host-dev (running without Docker): `pip install -e ".[ml,control-api,capture]"`
and run the console scripts directly — see [docs/code-tour.md](docs/code-tour.md)
for the full CLI cheatsheet.

## Further reading

- [`docs/design-decisions.md`](docs/design-decisions.md) — every "why we chose X" with tradeoff tables (eye-state crop flip, BIRDEYE-primary cutover, capture interval, watchdog, temporal smoothing, FallingAsleep, frame retention, SQLite-vs-JSONL).
- [`docs/database-schema.md`](docs/database-schema.md) — full column reference for `entries`, `corrections`, `training_runs`, `state`.
- [`docs/training.md`](docs/training.md) — corrections → retrain → re-inference loop, model versioning, post-retrain chain.
- [`docs/code-tour.md`](docs/code-tour.md) — per-file map of `src/bilbo/`, full CLI cheatsheet, sibling `report/` and `airgradient-logger/` notes.
- [`docs/api.md`](docs/api.md) — HTTP API reference: every `/api/v1/*` route, its query/body, and which `bilbo.api.*` function backs it.
- [`docs/shadow-to-prod-playbook.md`](docs/shadow-to-prod-playbook.md) — how to ship a new eye-state model. **Required reading before shipping a classifier change.**
- [`CLAUDE.md`](CLAUDE.md) — orientation for Claude Code working with this repo.

## Dashboard

Live at `http://localhost:5555`. Tabs: Live (camera + timeline + block detail), Models (BIRDEYE classifiers, training, pipeline history, eye-state daily metrics), Events (recap video, recent transitions), Air Quality (AirGradient sensor + bassinet state overlay).

Tab and panel breakdown:

1. **Status bar** — current state, duration, system health
2. **Live camera frame** — updates every capture interval
3. **Timeline** — 24h colored bar, date navigation, in/out time stats
4. **Block detail** — click any block to review frames, correct labels, draw face bboxes
5. **BIRDEYE Classifiers** (Models) — per-stage P/R/F1 vs corrected ground truth, training validation, retrain button + abort
6. **Pipeline** (Models) — cloud cost, BIRDEYE latency, monitoring gaps
7. **Pipeline History** (Models) — per-day detection-method breakdown + cost + model versions
8. **Eye-State Daily Metrics** (Models) — per-day P/R/F1 line charts per eye-state class
9. **Daily Recap** (Events) — MP4 time-lapse of a day's in-bassinet frames (in-bassinet only, skips empty crib stretches)
10. **Recent Events** (Events) — state transitions
11. **Air Quality** — Hero snapshot cards, Baby Comfort Score (0–100 weighted composite), alerts, insights, recommendations, trend charts with bassinet state-change vlines

REST API: the dashboard reverse-proxies `/api/*` to `control-api:5556/api/v1/*`. See [docs/api.md](docs/api.md) for the route reference; the underlying `bilbo.api.*` modules are listed in [docs/code-tour.md](docs/code-tour.md).

## Data (not in repo)

All data files are gitignored:

- `.env` — RTSP URL, API keys, credentials (whitelist `.env.example` is checked in)
- `data/monitor.db` — SQLite database (primary storage)
- `data/sleep-log.jsonl` — JSONL backup of all entries
- `data/corrections.jsonl` — JSONL backup of corrections
- `data/frames/` — captured camera frames (10 GB cap, ~17 days at 1-min intervals)
- `data/training-state.json` — training run state (Docker container ID + persisted summary)
- `data/head-state.json` — last known head position
- `data/watchdog-state.json` — capture-watchdog outage/recovery state machine
- `data/system.log`, `data/cron-*.log` — system logs (rotating, 5 MB × 3)
- `pipeline/models/` — versioned model checkpoints (last 20 kept) and the `latest` symlink. Initial weights at [Shanit/BIRDEYE](https://huggingface.co/Shanit/BIRDEYE) on Hugging Face.
- `pipeline/output/` — training data, validated face crops
- `airgradient-logger/data/airgradient.db` — AirGradient time-series readings
- `airgradient-logger/logs/` — logger stdout/stderr
- `venv/`, `airgradient-logger/venv/`, `report/venv/` — host-dev virtualenvs
