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
  → temporal state smoothing: 4 consecutive eyes_open/closed in last 6 present frames → Awake/Asleep
    → else carry forward previous smoothed state (or Unknown); raw per-frame state preserved in `rawState`
  → wake confirmation: 2/3 of last 3 entries Awake (after prior Asleep)? → Telegram alert (now trivially satisfied post-smoothing)
  → asleep confirmation: 2/3 of last 3 entries Asleep (after prior Awake)? → Telegram alert
  → edge detection: DISABLED post-flip — see github issue #3

macOS launchd (every 2 min) → watchdog.py
  → newest entries.timestamp older than WATCHDOG_ALERT_AFTER_MIN? → Telegram alert
  → recovered after outage? → Telegram "captures resumed" ping
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
- **Eye state** — tight face crop (from the face detector's bbox) → `eyes_open` / `eyes_closed`. **Input size is 448×448 since the 2026-04-14 flip** (was 224×224); controlled by `scripts/lib/config.py::EYE_STATE_INPUT_SIZE`. The prod checkpoint must be trained at this size — a mismatch is silent but produces degraded predictions because learned feature positions land in the wrong spatial cells.

Head position is adaptive: when BIRDEYE falls back to the cloud API, the API returns the head's approximate location in `data/head-state.json`, which BIRDEYE uses to center its crop on the next tick.

**Rollback for the 448 flip:** copy `pipeline/models/experiments/eye_state_224_legacy/latest/eye_state_classifier.pt` over `pipeline/models/latest/eye_state_classifier.pt`, set `EYE_STATE_INPUT_SIZE = 224`, reload launchd. The snapshot is preserved for exactly this purpose. The matching `eye_state_224_legacy` shadow experiment was retired on 2026-04-18 (registry entry removed from `scripts/lib/experiments.json`) — historical shadow results remain in the `entries.experiments` column as an audit trail, and the experiment can be reinstated by restoring the JSON entry.

| Metric | Value |
|---|---|
| Eye-state macro-F1 on held-out test (448, v_20260413_171025) | 1.000 (13/13 eyes_open, 90/90 eyes_closed, small val set) |
| Eye-state accuracy on the adversarial correction subset (448 vs prior 224) | 86.6% vs 52.4% (+34.2 pts on n=82) |
| Presence macro-F1 vs reviewed/corrected ground truth | ~0.99 |
| Frames handled locally (no cloud API call) | ~99% of non-empty |
| Total inference latency | ~130-135ms on CPU (was ~80-100ms at 224) |
| Daily cloud API cost (post-flip) | ~$0.01/day (was ~$1.17/day) |

### Related skill
- `baby-report` — generates activity reports including monitor performance (`--section monitor`)

## Config

- RTSP stream URL + OpenAI key: `/Users/shanit/.openclaw/workspace/.env.baby-monitor`
- Vision prompt: `references/prompt.md`
- Launchd plists: `~/Library/LaunchAgents/com.baby-monitor.plist` (60s interval), `com.baby-monitor-watchdog.plist` (120s interval), `com.baby-monitor-dashboard.plist` (persistent), `com.baby-monitor-retrain.plist` (daily, currently disabled)
- Classifier models: `pipeline/models/presence_classifier.pt`, `pipeline/models/eye_state_classifier.pt`
- Head state: `data/head-state.json`
- Watchdog state: `data/watchdog-state.json` (outage/recovery state machine for the capture watchdog)

## Scheduling (launchd)

```bash
launchctl list | grep baby-monitor                                          # status (exit 0 = ok)
launchctl unload ~/Library/LaunchAgents/com.baby-monitor.plist              # stop monitor
launchctl load   ~/Library/LaunchAgents/com.baby-monitor.plist              # start monitor
launchctl load   ~/Library/LaunchAgents/com.baby-monitor-watchdog.plist     # capture watchdog (every 2 min)
launchctl load   ~/Library/LaunchAgents/com.baby-monitor-dashboard.plist    # dashboard (persistent)
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
| `monitor.py --backfill-shadow --hours N [--only-stale] [--limit N] [--dry-run]` | Re-run BIRDEYE inference on historical entries and write results into the **audit-trail `shadow` dict only** — does NOT touch the user-facing primary fields. Use after deploying a new model when you want the shadow audit brought up to date with the new weights. `--only-stale` skips entries already tagged with the deployed version. For a primary-field refresh (i.e. to rescue `eyeState` on pre-BIRDEYE-primary-flip frames so the state smoother has signal), use `scripts/backfill_birdeye_primary.py` instead. |
| `monitor.py --verbose` | Print all log messages to stderr |
| `run_single_inference.py <ts>` | Re-run BIRDEYE on the frame for one timestamp and update its `shadow` audit fields (does NOT touch user-facing primary fields or corrections). Thin wrapper around `lib.local_pipeline.run_birdeye_inference` and `birdeye_result_to_shadow_blob` — same shared helpers `monitor.py` uses, so the two paths cannot drift. Called by the dashboard's `/api/run-inference` button via subprocess. |

### watchdog.py — capture-staleness watchdog

Independent launchd job (`com.baby-monitor-watchdog`, 120s interval) — runs alongside the monitor, not from inside it. Reads the newest `entries.timestamp` via `db.get_last_entry()`. If the age exceeds `WATCHDOG_ALERT_AFTER_MIN` minutes (default 5), it sends a Telegram alert and persists `outage_started_at` / `last_alert_at` in `data/watchdog-state.json`. While an outage is ongoing, sends one reminder per `WATCHDOG_REMINDER_AFTER_MIN` (default 60). On recovery (newest timestamp is fresh again), sends a single "captures resumed after N min" ping and clears the state file.

Catches: RTSP outage, monitor crash, launchd stall on the capture job, Wi-Fi disassociation. Doesn't catch: laptop off/unplugged — nothing on this machine runs in that case. The right fix for that is a push-style cloud heartbeat; not built yet.

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

**Post-retrain chain (automatic since 2026-04-19):** `monitor.py --retrain` (and the dashboard's Retrain button, which shells to the same command) triggers a chain after the new model is deployed: `backfill_birdeye_primary.py --start=<7 days ago>` → `backfill_state.py` → `bbox_impact.py --force`. This keeps the dashboard's Per-class / Bbox-impact numbers pinned to the deployed model without a manual follow-up. Opt out with `--skip-post-retrain`; widen the backfill window with `--post-retrain-backfill-days N`. Chain-step failures are logged but non-fatal — the retrained model is already persisted.

## Directory structure

```
skills/baby-monitor/
├── scripts/                        # All source code
│   ├── monitor.py                  # Main pipeline (cron entry point)
│   ├── train_classifiers.py        # Training script (--eye-crop-size + --experiment-tag for shadow variants, writes meta.json into every version dir)
│   ├── promote_experiment.py       # One-command shadow → prod flip, rollback is the same command pointed at the legacy snapshot. See docs/shadow-to-prod-playbook.md
│   ├── bbox_impact.py              # A/B eye-state on predicted vs corrected bboxes (manual-run, caches into state.bbox_impact for the dashboard)
│   ├── experiments_backfill.py     # Run registered shadow experiments against historical frames
│   ├── backfill_birdeye_primary.py # Re-run BIRDEYE over a time window and write into the **primary** eyeState/faceBbox/presence+eye-confidence fields (NOT just the shadow audit). Skips `eye_state_edited=1` rows. Use when deploying a new model and you want the temporal state smoother to re-fire over refreshed eye-state signal. Pair with `backfill_state.py` afterwards.
│   ├── backfill_state.py           # Walk the DB in timestamp order and re-run `smooth_state_temporal` on every entry, rewriting `state` + seeding `rawState`. Re-runnable if `STATE_CONFIRM_WINDOW`/`STATE_CONFIRM_RUN` change.
│   ├── watchdog.py                 # Capture-staleness watchdog — own launchd job (every 2 min), Telegram alert when newest entry is stale, state in data/watchdog-state.json
│   ├── run_single_inference.py     # Dashboard re-run button entry (subprocessed by Flask)
│   ├── requirements.txt            # All Python deps
│   └── lib/
│       ├── experiments.py          # Shadow pipeline framework — Experiment base class, EyeStateShadowExperiment (manifest-driven), run_all()
│       ├── experiments.json        # Manifest for registered shadow experiments (edited atomically by promote_experiment.py)
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
│   ├── alert-state.json            # Wake + asleep alert cooldowns (lastActiveWakeAlert, lastAsleepAlert)
│   ├── watchdog-state.json         # Capture-watchdog outage/recovery state machine
│   ├── watchdog-stdout.log         # Watchdog launchd stdout
│   └── watchdog-stderr.log         # Watchdog launchd stderr (this is where the watchdog log lines actually land)
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
| `state` | Asleep, Awake, Unknown, not_present | **Temporally smoothed** (2026-04-14). Only flips to Awake/Asleep after 4 consecutive agreeing `eyeState` readings (`eyes_open` / `eyes_closed`) in the last 6 baby-present frames. Between flips, the previous smoothed state is carried forward. See `lib/state.py` + `STATE_CONFIRM_WINDOW` / `STATE_CONFIRM_RUN` in `lib/config.py`. |
| `rawState` | Asleep, Awake, Unknown, not_present | Unsmoothed per-frame state — what BIRDEYE's eye-state mapping or the cloud API returned for this frame alone. Preserved so history can be re-smoothed offline if the window/run thresholds change. Added 2026-04-14. |
| `presenceConfidence` | 0.0–1.0 | Birdeye Classifier 1 confidence |
| `eyeConfidence` | 0.0–1.0 | Birdeye Classifier 2 confidence |
| `eyeState` | eyes_open, eyes_closed, face_not_visible | Birdeye raw eye classification |
| `faceBbox` | `{"x1": 0.57, "y1": 0.09, "x2": 0.76, "y2": 0.40}` | Normalized bbox from BIRDEYE's face detector (relative to the bassinet crop) |
| `faceBboxCorrected` | same shape as `faceBbox` | User-drawn bbox from the dashboard's face-box tool; treated as ground truth by `bbox_impact.py` |
| `bboxImpact` | `{"onPredicted": {...}, "onCorrected": {...}, "groundTruth": ..., "modelVersion": ...}` | Per-frame cache from `bbox_impact.py` — measurement only, never overwrites `eyeState` |
| `experiments` | `{"<experiment_name>": {"state": ..., "eyeState": ..., "eyeConfidence": ..., "modelVersion": ..., "latencyMs": ..., "ranAt": ...}}` | Per-frame shadow pipeline results, one entry per registered experiment in `scripts/lib/experiments.py`. Written by `monitor.py` at capture time and by `experiments_backfill.py` for historical frames. Read-only observers — never touch `eyeState` or `state`. SQLite-only field: any write path that touches the `data` column must merge, not overwrite. |

### training_runs metrics schema (per classifier)

Each per-classifier sub-dict inside `training_runs.metrics` carries both
val-set and test-set metrics. Val is used for best-epoch selection
(optimistically biased); test is held out and scored exactly once with
the saved best weights, so it's the honest generalization number.

| Key | Meaning |
|---|---|
| `train_total` | Number of training samples the Dataset class produced after per-classifier filtering |
| `val_total` | Same, for the validation set |
| `test_total` | Same, for the held-out test set. Added 2026-04-13; older runs backfilled via one-shot helper, see the git log. |
| `val_accuracy` / `best_macro_f1` | Val-set best-epoch metrics |
| `test_accuracy` / `test_macro_f1` | Test-set metrics on the saved best checkpoint (populated by training runs after 2026-04-13) |
| `test_per_class` | `{cls: {precision, recall, f1, support}}` on the test set |
| For the face detector only: `test_mean_iou`, `test_conf_accuracy`, `test_iou_samples` | Same scoring as the val IoU / confidence metrics but on held-out test |
| `headPosition` | `{"x": 0.35, "y": 0.22, "visible": true}` | From cloud API (when called) |
| `birdeyeTimings` | `{"presence": 0.02, "eye_state": 0.02, "total": 0.05}` | Per-stage timing |
| `sleepPosition` | Back, Side, Stomach, Unknown | Cloud API only |
| `alerts` | list of strings | Safety alerts triggered |

### Temporal state smoothing (2026-04-14)

The primary `state` field is temporally smoothed before persistence. The raw per-frame eye-state classification (`eyes_open` / `eyes_closed`) is noisy — a single mis-classified frame or a brief REM blink can flip a point-in-time reading. The rule in `lib/state.py`:

- Within the last 6 baby-present frames (including the current one), a run of **4 consecutive `eyes_open`** readings → `state = Awake`. Same for `eyes_closed` → `Asleep`.
- Otherwise, the previous smoothed state is carried forward. With no prior Awake/Asleep in history, state degrades to `Unknown`.
- Cloud-API fallback frames (no `eyeState`) and intermediate classes (`face_not_visible`, `low_confidence`) break the consecutive run.
- Window / run thresholds: `STATE_CONFIRM_WINDOW = 6`, `STATE_CONFIRM_RUN = 4` in `lib/config.py`.

The unsmoothed per-frame reading is preserved in `rawState` so history can be re-smoothed offline if the thresholds change (`scripts/backfill_state.py`).

**History lookup goes through SQLite, not JSONL.** The live path in `monitor.py` calls `db.get_recent_entries(STATE_CONFIRM_WINDOW - 1)` (SQL `LIMIT` query) rather than `storage.get_recent_entries` (JSONL tail read). This is load-bearing: the JSONL reader used a fixed 600-bytes-per-entry budget that silently under-returned once real entries grew to ~1.4 KB (post shadow + experiments dicts), so asking for 5 history frames was yielding only 2, the 4-of-6 rule could never fire, and every present frame cascaded into Unknown for ~24 hours before the bug was caught (2026-04-15). `storage.get_recent_entries` now uses an adaptive read-size that doubles on underflow, but the SKILL is clear on the architectural rule: **read paths must use SQLite via `lib/db.py`**; JSONL is only the append-only backup. As of 2026-04-15 all runtime read callers have been migrated: `alerts.should_burst` / `check_wake_confirmation` (wake detection), `detect.detect_empty_bassinet` (pixel-diff reference), `monitor.py` cloud-fallback heuristic, and `dashboard/app.py::api_sleep_stats` all go through `db`. JSONL read paths remain only in `train_classifiers.py` and `cli.py` backtest/audit subcommands, which intentionally want the append-only log as historical ground truth.

**Unknown → Awake absorption (2026-04-15).** After smoothing, if the new `state` is `Awake`, any immediately-preceding contiguous run of `Unknown` + `babyPresent` frames whose total span is less than `UNKNOWN_ABSORB_MAX_MINUTES` (default 15) is retroactively re-flipped to `Awake`. The rationale: if BIRDEYE briefly lost the face (hand over eyes, crib shift, face pressed into mattress) and then caught 4 consecutive `eyes_open`, the ambiguous gap was almost certainly "awake but momentarily unreadable", not a period of actual sleep. The rule is **asymmetric by design** — the analogous Unknown → Asleep direction is not applied because pre-wake ambiguity is a weaker signal than the wake itself and the cost of mis-labeling ambiguous time as sleep is higher. The absorption helper lives in `lib/state.py::unknown_prefix_to_absorb`, the live path applies it after `db.insert_entry` by rewriting historical rows via `db.update_entry`, and `scripts/backfill_state.py` applies it in a second pass after the forward smoothing sweep.

### Active wake + asleep alerts

When the baby's smoothed state transitions to "Awake" after being "Asleep" (or vice versa), the pipeline confirms by checking the last 3 entries. If 2+ agree, sends a Telegram alert. Wake alerts include inline feedback buttons; asleep alerts are plain text. Each direction has its own 30-min cooldown (`lastActiveWakeAlert`, `lastAsleepAlert` in `data/alert-state.json`); both reset together when baby is removed (`babyPresent=False`) so the next placement session starts fresh.

The asleep alert is **gated on a prior `Awake` in the lookback window** so it only fires on awake→asleep drift, not on "baby placed already-asleep" (which the caretaker just did and doesn't need a ping for).

**Note:** since the primary `state` is now temporally confirmed (4-of-6 consecutive), the 2-of-3 confirmation is trivially satisfied on any state transition and acts mainly as the prior-state gate + cooldown enforcer.

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
