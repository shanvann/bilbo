# Code Tour

Per-file map of the bilbo source. Companion to `CLAUDE.md` (architecture
pointers) and `docs/design-decisions.md` (why-it-is-the-way-it-is).

## `src/bilbo/` — main package

| File | Purpose |
|---|---|
| `monitor.py` | Main pipeline — capture → pixel-diff → BIRDEYE → cloud fallback → smooth → alert → dual-write. `--loop` runs forever (capture container); default runs one tick. |
| `watchdog.py` | Capture-staleness watchdog. Runs as a background thread inside the capture container (every 2 min). Reads newest `entries.timestamp`; if older than `WATCHDOG_ALERT_AFTER_MIN`, Telegram-alerts and tracks state in `data/watchdog-state.json`. Recovery ping when captures resume. |
| `capture_service.py` | Capture container entry point — Flask :5557 with `POST /infer` + `GET /healthz`. Runs the monitor and watchdog as daemon threads so warm torch is reused for ad-hoc dashboard re-runs. |
| `cli.py` | Argparse + every `cmd_<name>` handler `monitor.py` dispatches into. Also hosts `train_main()` — the `bilbo-train` console-script entry point used by the Docker training container. |
| `train_classifiers.py` | Train classifiers with corrections/audit data. `--eye-crop-size` and `--experiment-tag` let you train eye-state variants at different input resolutions without clobbering the prod model. |
| `state.py` | Temporal state smoothing (`STATE_CONFIRM_WINDOW` / `STATE_CONFIRM_RUN`), `unknown_prefix_to_absorb` for Unknown→Awake absorption, `putdown_prefix_to_absorb` for FallingAsleep. |
| `alerts.py` | Telegram wake/asleep/safety alerts with cooldown logic. |
| `experiments.py` + `experiments.json` | Shadow pipeline framework — `Experiment` base class, `EyeStateShadowExperiment` generic, manifest-driven registry. New shadows are added by editing `experiments.json`, not Python source. Atomically edited by `bilbo-promote-experiment` during a flip. |
| `training_state.py` | Docker-SDK control plane for the `bilbo-training` container (PID fallback for host-dev). `is_running` / `start` / `abort` / `get_status`. |
| `config.py` | All constants, paths, thresholds, model chain, logging setup. Env-var-driven: `BILBO_ROOT`, `BILBO_DATA_DIR`, `BILBO_MODELS_DIR`, `BILBO_ENV_FILE`, `BILBO_REFERENCES_DIR`. |

### `src/bilbo/pipeline/`

BIRDEYE on-device cascade + cloud API fallback + pixel-diff gate.

| File | Purpose |
|---|---|
| `local_pipeline.py` | 3-stage BIRDEYE orchestration: presence → face detection → eye-state. `run_birdeye_inference(frame_path)` is the single entry point all callers (monitor.py, capture_service /infer, run_single_inference) must use. `maybe_reload_classifiers()` drops the cached singletons when `pipeline/models/latest` flips so retrains take effect within 60 s without a container restart. |
| `classifiers.py` | `BabyPresenceClassifier`, `EyeStateClassifier` (MobileNetV3-Small), `TrainableFaceDetector` (MobileNetV3-Small, primary), `FaceDetector` (YuNet ONNX, fallback). |
| `vision.py` | Cloud API calls (OpenAI GPT-4o), prompt rendering, response parsing. |
| `detect.py` | Pixel-diff empty-bassinet detection (gates BIRDEYE/cloud). |
| `capture.py` | ffmpeg RTSP frame capture. |

### `src/bilbo/storage/`

All SQLite + JSONL persistence. Read paths go through `db.py`.

| File | Purpose |
|---|---|
| `db.py` | All SQLite read/write. Do not open `data/monitor.db` directly elsewhere. |
| `files.py` | Frame retention (oldest-first pruning at the 10 GB cap), JSONL append. |

### `src/bilbo/api/`

Internal Python contract — control-api and capture import only from
here for cross-domain operations. New callers must go through this
layer rather than `bilbo.storage.*` or `bilbo.pipeline.*` directly.

`entries`, `training`, `corrections`, `frames`, `stats`, `inference`,
`system`, `air_quality`, `recap`, `models`.

### `src/bilbo/http/`

Control-API Flask app. Mounts `bilbo.api.*` under `/api/v1/*` on :5556.

### `src/bilbo/scripts/`

Operational scripts. Each has a `main()` and a matching console script
(see `pyproject.toml`); also runnable as `python -m bilbo.scripts.<name>`.

| File | Console script | Purpose |
|---|---|---|
| `run_single_inference.py` | `bilbo-inference` | Re-run BIRDEYE on one frame by timestamp; used by control-api's POST /api/v1/run-inference path via capture's `/infer`. |
| `backfill_birdeye_primary.py` | `bilbo-backfill-primary` | Re-run BIRDEYE with the currently deployed weights over a time window and write into the **primary** `eyeState` / `faceBbox` / `presenceConfidence` / `eyeConfidence` fields (plus refresh the `shadow` audit dict). Skips `eye_state_edited=1` rows. Pair with `bilbo-backfill-state` after. |
| `backfill_state.py` | `bilbo-backfill-state` | Re-smooth `state` + `rawState` over the primary `eyeState` signal for the whole DB. Cheap, re-runnable. |
| `bbox_impact.py` | `bilbo-bbox-impact` | Measure eye-state accuracy on predicted vs corrected bboxes; caches into the `state` table for the dashboard. |
| `experiments_backfill.py` | `bilbo-experiments-backfill` | Run registered shadow experiments against historical frames so the dashboard has immediate comparison data after a new experiment lands. |
| `promote_experiment.py` | `bilbo-promote-experiment` | **One-command shadow → prod promotion.** Bundles the flip (snapshot, copy, meta update, metrics patch, stale-key cleanup, manifest edit, backfill, reinfer). Rollback is the same command pointed at the legacy snapshot tag. See `docs/shadow-to-prod-playbook.md`. |

## CLI cheatsheet

```bash
bilbo-monitor                                       # one tick (capture container runs --loop)
bilbo-monitor --dry-run                             # test without writing
bilbo-monitor --capture-only                        # grab one frame and exit
bilbo-monitor --analyze FRAME                       # re-run cloud API on an existing frame
bilbo-monitor --retrain                             # retrain with pending corrections (auto-runs post-retrain chain)
bilbo-monitor --retrain --force                     # retrain even if no new corrections
bilbo-monitor --retrain --skip-post-retrain         # retrain only (skip auto backfill + bbox_impact refresh)
bilbo-monitor --retrain --post-retrain-backfill-days 14   # widen auto primary-backfill window
bilbo-monitor --eval-corrections                    # re-eval the deployed model on corrections (no retrain)
bilbo-monitor --audit --sample 50                   # spot-check BIRDEYE vs cloud API disagreements
bilbo-monitor --list-models                         # show model versions + metrics
bilbo-monitor --rollback VERSION                    # revert to a previous model
bilbo-monitor --backtest --birdeye                  # BIRDEYE accuracy vs cloud API ground truth
bilbo-monitor --status                              # system health (gaps, disk, recent stats)
bilbo-monitor --last 10                             # recent log entries
bilbo-monitor --backfill-shadow --hours 168 --only-stale   # re-run BIRDEYE on history → SHADOW dict only

bilbo-bbox-impact                                   # cache eye-state A/B on predicted vs corrected bbox
bilbo-bbox-impact --limit 20 --verbose              # iterate during development
bilbo-bbox-impact --dry-run                         # compute without persisting
bilbo-bbox-impact --force                           # re-run on already-cached frames against the current model

bilbo-experiments-backfill                          # run all registered shadow experiments over history
bilbo-experiments-backfill --name eye_state_448 --force   # one experiment, force re-run
bilbo-experiments-backfill --hours 24 --limit 50 --verbose
```

## `report/` — sibling reporting skill

```bash
report.py --range 24h                    # full report
report.py --section monitor              # model performance only
report.py --format json                  # structured output
```

Sections: `sleep`, `feeding`, `pumping`, `diapers`, `weight`, `monitor`.

Untouched by the package refactor — still uses its own `report/scripts/lib/` layout.

## `airgradient-logger/` — sibling air-quality logger

Standalone polling daemon for an AirGradient indoor air-quality monitor on
the LAN. Writes one row per reading into a SQLite database at
`airgradient-logger/data/airgradient.db`. The dashboard's Air Quality tab
reads this DB read-only and pairs it with bassinet state transitions
from the monitor DB to overlay state-change vlines on the time-series
charts.

```bash
cd airgradient-logger
AIRGRADIENT_URL=http://192.168.x.x/measures/current \
  DB_PATH=data/airgradient.db \
  venv/bin/python airgradient_logger.py     # foreground

# Or via docker-compose against airgradient-logger/docker-compose.yml
```

Field mapping reads both camelCase (current firmware: `pm003Count`,
`tvocRaw`, `pm02Compensated`, `atmpCompensated`, `rhumCompensated`) and
snake_case for older firmware. Full payload preserved as `raw_json` so
any field the typed columns miss can be queried via `json_extract()`
(the dashboard uses this to pull `tvocIndex` without a schema migration).
