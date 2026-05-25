# Database Schema

Primary storage: `data/monitor.db` (SQLite, WAL mode). All read/write goes
through `bilbo.storage.db`; never open the file directly elsewhere.

The JSONL backup at `data/sleep-log.jsonl` is append-only and captures
*primary* pipeline fields only. SQLite-only fields (notably `bboxImpact`,
`experiments`, `faceBboxCorrected`) are derived after-the-fact — any
SQLite-write code path must merge with the existing `data` JSON blob,
not overwrite it. See [`docs/design-decisions.md`](design-decisions.md#storage-sqlite-vs-jsonl)
for the rationale.

## entries

Main table — one row per captured frame.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| timestamp | TEXT | ISO 8601 UTC (indexed) |
| frame | TEXT | Path to JPEG file |
| baby_present | INTEGER | 1=present, 0=absent |
| state | TEXT | Asleep, Awake, FallingAsleep, Unknown, not_present |
| eye_state | TEXT | eyes_open, eyes_closed, face_not_visible, not_in_bassinet |
| eye_state_edited | INTEGER | 1 if corrected via dashboard (indexed) |
| eye_state_corrected_at | TEXT | When the correction was made |
| detection_method | TEXT | birdeye, vision-api, pixel-diff, spot-check (indexed) |
| model_used | TEXT | openai/gpt-4o, local/mobilenet+mobilenet, n/a |
| shadow_model_version | TEXT | e.g., v_20260408_201030 |
| presence_confidence | REAL | BIRDEYE presence classifier confidence (0-1) |
| eye_confidence | REAL | BIRDEYE eye-state classifier confidence (0-1) |
| diff_score | REAL | Pixel-diff score |
| shadow_birdeye_state | TEXT | What BIRDEYE predicted (shadow) |
| shadow_prod_state | TEXT | What prod pipeline returned |
| shadow_agreed | INTEGER | 1=aligned, 0=misaligned, NULL=no shadow |
| shadow_timings_total | REAL | BIRDEYE inference time in seconds |
| data | JSON | Full entry as JSON (all fields). Notable keys inside: `faceBbox` (BIRDEYE's predicted normalized bbox), `faceBboxCorrected` (user-drawn bbox from the dashboard, treated as ground truth), `bboxImpact` (cached output of `bilbo-bbox-impact` — A/B eye-state result on both bboxes, present on frames that have been analyzed), `experiments` (map of registered shadow-experiment name → result dict, written by `bilbo.experiments` on every capture tick and by `bilbo-experiments-backfill` for historical frames). |
| created_at | TEXT | Row creation timestamp |

## corrections

Dashboard and audit corrections — training signal.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| corrected_at | TEXT | When correction was made (indexed) |
| original_timestamp | TEXT | Entry timestamp being corrected |
| frame | TEXT | Path to frame |
| original_state | TEXT | State before correction |
| corrected_state | TEXT | New state (if sleep state changed) |
| original_eye_state | TEXT | Eye state before correction |
| corrected_eye_state | TEXT | New eye state (eyes_open, eyes_closed, etc.) |
| detection_method | TEXT | What produced the original label |
| source | TEXT | dashboard, audit |
| used_in_training | TEXT | Model version that consumed this correction |

## training_runs

One row per training run — model provenance and metrics.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| version | TEXT | e.g., v_20260408_201030 (indexed) |
| timestamp | TEXT | When training completed |
| entries_total | INTEGER | Total labeled frames used |
| label_sources | JSON | `{"cloud-api": 530, "correction": 35, "audit": 10}` |
| split | JSON | `{"train": 435, "val": 99, "test": 41}` |
| config | JSON | Hyperparameters (epochs, lr, batch_size, etc.) |
| metrics | JSON | Per-classifier sub-dict. Val-set fields (`val_accuracy`, `best_macro_f1`, `best_val_loss`, `per_class`, `train_total`, `val_total`) are optimistically biased because val is used for best-epoch selection. Held-out test fields (`test_total`, `test_accuracy`, `test_macro_f1`, `test_per_class`) describe the saved best checkpoint on an unseen split and are the honest generalization numbers. Face detector carries `test_mean_iou` + `test_conf_accuracy` instead of `test_accuracy`. Train/val/test splits are deterministic via `time_block_split` with SEED=42 and 30-min blocks. |
| models_trained | TEXT | "all", "presence", "eye-state" |
| duration_seconds | REAL | Wall-clock training duration (NULL for runs before 2026-04-09) |
| started_at | TEXT | When training began (ISO 8601 UTC) |
| finished_at | TEXT | When training completed (ISO 8601 UTC) |

## state

Key-value store for runtime state.

| Key | Value | Description |
|-----|-------|-------------|
| head | `{"x": 0.5, "y": 0.3, ...}` | Last known head position for BIRDEYE crop |
| alert | `{"lastEdgeAlert": "..."}` | Alert cooldown timestamps |
| training | `{"status": "running", "containerId": "...", ...}` | Training process state (Docker container ID in container mode; PID in host-dev mode) |
| bbox_impact | `{"count": N, "accuracyOnPredicted": ..., "accuracyOnCorrected": ..., "delta": ..., "flipRate": ..., "perClass": {...}, "modelVersion": "..."}` | Aggregate from `bilbo-bbox-impact`. Read by the dashboard's Face Detection column to show whether corrected bboxes produce better eye-state predictions. Manual-refresh only — never touched by the live pipeline. |
