# Design Decisions

Key tradeoffs and the reasoning behind them. Companion to `README.md`
(overview) and `docs/database-schema.md` (storage layout).

## Eye-state classifier input resolution — flipped to 448 on 2026-04-14

The eye-state classifier originally ran at 224×224 input (the torchvision MobileNetV3-Small default). The bbox-impact analysis on April 13 showed that the `eyes_open` class was the pipeline's accuracy bottleneck — 66-80% depending on the subset — while `eyes_closed` was at 88-99%. The shadow-experiment infra was built, and a separately-trained 448×448 model was run alongside prod for a day.

On the adversarial subset (82 frames where the user had to correct the label), the 448 model hit 86.6% vs prod's 52.4% — a **+34 point absolute improvement** on the cases that matter most. On the broader comparison (1206 frames in a 7-day window) the gap was +11.1 pts, heavily weighted toward `eyes_open`:

| Class | n | Prod 224 | Shadow 448 | Δ |
|---|---|---|---|---|
| `eyes_open` (needs iris/pupil resolution) | 144 | 80.6% | **95.8%** | **+15.2** |
| `eyes_closed` (eyelash line already resolvable at 224) | 55 | 90.9% | 90.9% | 0.0 |

The physics matched the hypothesis: `eyes_open` recognition needs enough pixels on the iris, `eyes_closed` is already above the resolution floor for the eyelash/lid features the model relies on. Latency went from ~15 ms to ~43 ms per inference — still well under budget given the 1-minute capture cadence.

**Decision:** flipped. `EYE_STATE_INPUT_SIZE = 448` lives in `src/bilbo/config.py`. The prod path in `src/bilbo/pipeline/local_pipeline.py` passes this to `EyeStateClassifier.classify()`. The old 224 weights are preserved at `pipeline/models/experiments/eye_state_224_legacy/latest/eye_state_classifier.pt` for rollback. The matching `eye_state_224_legacy` inverted shadow experiment (which ran the legacy model on every non-empty frame as a regression tripwire) was retired on **2026-04-18** once the 448 model had 6 days of clean production data — the registry entry was removed from `src/bilbo/experiments.json` but the weights and the historical shadow results in `entries.experiments` are preserved. Re-enable by restoring the JSON entry.

**Rollback** is a single command — literally the same promotion flow pointed at the legacy snapshot:

```bash
bilbo-promote-experiment --tag eye_state_224_legacy
```

No retraining, no data migration, no source-file edits. The promotion script handles every step of the flip (snapshot, copy, meta update, SQL patch, manifest edit, backfill, reinfer) and always preserves the current prod as a new rollback snapshot before overwriting, which means rollback-of-rollback is also a single command. See `docs/shadow-to-prod-playbook.md` for the full lifecycle.

**Under the hood** the pipeline's input resolution is read from `pipeline/models/latest/meta.json`, a sidecar written alongside the weights. `config.EYE_STATE_INPUT_SIZE` reads that file on import — a fallback default of 224 kicks in if the sidecar is missing. This means: (a) flipping is a file-write, not a source edit, (b) rollback via the `latest` symlink automatically reverts the crop size because the meta.json in the old version dir is self-contained, (c) `train_classifiers.py` must write meta.json into every version dir it creates — otherwise flipping the symlink would silently revert the runtime to 224. That last invariant is enforced in the training script now (was added 2026-04-14 after a retrain briefly regressed prod by stripping the sidecar).

## Shadow vs Production (historical)

How we safely deployed BIRDEYE without risking production quality. This is the rationale behind the **2026-04-12 flip** (commit `7250067`); BIRDEYE has been primary ever since, and cloud API usage collapsed from ~every non-empty frame to <2%.

| Approach | Risk | Data quality | Cost |
|----------|------|-------------|------|
| Direct deploy | High — bad model = missed wakes | No comparison data | Low |
| A/B split | Medium — some frames get bad model | Partial comparison | Medium |
| **Shadow mode (chosen until alignment ≥95%)** | **Zero — cloud API handles all decisions** | **Every frame compared** | **~$1.20/day at 4-min cadence** |
| **BIRDEYE-primary + cloud fallback (chosen post-flip)** | **Low — cloud catches low-confidence / no-face cases** | **Continuous via user corrections** | **~$0.24/day at 1-min cadence (~95% reduction vs pre-flip-at-1-min)** |

**Decision:** Shadow mode for the build-up phase — cloud API was production and BIRDEYE ran in parallel on every frame for months while we accumulated alignment data. Once alignment crossed 95% and the corrections-driven retraining loop was stable, the pipeline flipped to BIRDEYE-primary with cloud API as the fallback on BIRDEYE bails (`no_face_detected`, `low_confidence`, hard error). The `shadow` sub-dict in every entry is now an immutable record of what BIRDEYE said, kept separate from the user-correctable primary fields.

The flip day also bumped capture cadence from 4 min → 1 min (commit `0045243`, same day), because cloud cost was no longer the constraint. See the **Pipeline History** card on the Models tab for the per-day cost curve across the transition.

## Capture Interval

How often the camera grabs a frame determines how quickly we detect a wake-up and how much disk we burn storing images. This value changed over the life of the project as the cost picture changed.

| Interval | Frames/day | Disk/week | Wake detection delay | Cloud API cost (post-flip) |
|----------|-----------|-----------|---------------------|----------------------------|
| 4 min | 360 | ~1.5 GB | Up to 4 min | ~$0.005/day |
| 2 min | 720 | ~3.0 GB | Up to 2 min | ~$0.01/day |
| **1 min (chosen)** | **1,440** | **~6.0 GB** | **Up to 1 min** | **~$0.02/day** |

**Decision:** 1-minute intervals now that BIRDEYE runs as primary. When the cloud API was on every non-empty frame (~$0.01 each), 4-min intervals were the cost sweet spot — at 1-min they would have cost ~$14/day. Post BIRDEYE-primary flip, the cloud API runs on ~1% of frames as a fallback, so the cost of going to 1-min capture is basically zero and the wake detection latency drops by 4x. The capture interval change also makes the existing `BURST_AWAKE_THRESHOLD = 2 of last 3` wake rule fire ~3 min after a real wake event (was ~12 min at 4-min cadence).

## Detection Pipeline Order

Three systems can analyze a frame (BIRDEYE, pixel-diff, cloud API). The order they run in determines latency, cost, and resilience when one system is down.

| Order | Cost | Resilience | Notes |
|-------|------|------------|-------|
| pixel-diff → cloud + shadow BIRDEYE | ~$1.20/day at 4-min cadence | Cloud-dependent | Pre-flip; BIRDEYE ran every frame as shadow for validation |
| **pixel-diff → BIRDEYE → cloud fallback (chosen, post-2026-04-12)** | **~$0.24/day at 1-min cadence** | **Cloud only needed on BIRDEYE bails (~1-2% of non-empty frames)** | BIRDEYE 3-stage cascade (presence → face → eye-state) handles the hot path on-device; cloud catches `no_face_detected` / `low_confidence` / hard errors. |
| BIRDEYE only | $0 | Degrades when face is hidden; no recovery path | Would be ~98% accurate but the last 2% are the cases we care most about |

**Decision:** pixel-diff → BIRDEYE → cloud fallback. Pixel-diff cheaply gates out empty-bassinet frames before any model runs; BIRDEYE handles the vast majority of non-empty frames in ~130 ms on CPU; the cloud API is a correctness net for the hard cases BIRDEYE flags itself. The **Pipeline History** table on the dashboard is the running audit of this split.

## Local vs Cloud Analysis

The fundamental architecture question: run ML on-device, send frames to a cloud API, or both?

| Approach | Latency | Cost | Accuracy | Privacy |
|----------|---------|------|----------|---------|
| Cloud-primary + BIRDEYE shadow (pre-flip) | 2-5 s | ~$0.01/frame on every non-empty | High (GPT-4o) on every frame | Frames sent to OpenAI |
| **Local-primary + cloud fallback (chosen, post-2026-04-12)** | **~130 ms local, 2-5 s on ~1-2% fallbacks** | **~$0.24/day at 1-min cadence (~95% reduction)** | **~99% on reviewed/corrected ground truth; cloud backs up the hard cases** | **~99% on-device** |
| Local only | ~130 ms | $0 | Degrades silently on face-occluded frames | Full privacy |

**Decision:** Local-primary with cloud fallback. The shadow-mode phase built the alignment data needed to promote BIRDEYE safely; the corrections-driven retraining loop keeps it improving with every label review. Cloud API remains in the pipeline specifically for the frames BIRDEYE can't see clearly — retiring it entirely would mean silently missing the exact events we most want to catch.

## Wake Confirmation

A single "Awake" frame could be noise (classifier error, motion blur). We need a confirmation strategy that filters false alarms without delaying real alerts or blocking the pipeline.

| Approach | Detection delay | Blocking time | Complexity |
|----------|----------------|---------------|------------|
| Single frame | Instant | 0 | Low, but noisy (false alarms) |
| Burst capture (old) | +2 min | **2 min blocking** (sleeps between captures) | High (extra captures, API calls) |
| **Look-back (chosen)** | +3 min (at 1-min interval) | **0 (non-blocking)** | Low (check last 3 entries) |

**Decision:** Look-back confirmation. Requiring 2/3 entries to show "Awake" filters noise without blocking the pipeline. At 1-min intervals, confirmation takes ~3 minutes of consecutive captures (was ~12 min at the old 4-min cadence). The window is hardcoded at 3 frames in `alerts.check_wake_confirmation` as `[-3:]`; if you want to widen it for lower false-positive risk at the faster capture rate, parameterize that slice and the `BURST_AWAKE_THRESHOLD` config constant together.

**Asleep alert (mirror).** `alerts.should_alert_asleep` + `alerts.check_asleep_confirmation` are symmetric to the wake pair: 2/3 of last 3 frames `Asleep` confirms a sleep-onset transition, gated on a prior `Awake` in the `WAKE_WINDOW` lookback so the alert fires only on awake→asleep drift, not on a baby placed already-asleep (which the caretaker just did anyway). Independent 30-min cooldown via `lastAsleepAlert` in `alert-state.json`; both cooldowns reset together on `babyPresent=False` so the next placement session starts fresh.

## Capture Watchdog

Monitoring outages (e.g. 2026-04-16: 16h44m gap) are invisible until the next morning, because nothing alerts when the monitor itself stops working — wake/asleep alerts depend on the monitor running. The watchdog closes that loop.

| Approach | Detects | Doesn't detect | Cost |
|----------|---------|----------------|------|
| In-monitor self-check | Single capture failures | Monitor crashed entirely; container restart loop hides it | Free (already in pipeline) |
| **Capture-container background thread (chosen)** | RTSP outage, monitor crash, capture loop stall | Host off / Docker daemon down | Tiny (one SQL `MAX(timestamp)` query every 2 min) |
| Push-style cloud heartbeat | All of the above + laptop off | Cloud down | Higher (need a cloud endpoint) |

**Decision:** Watchdog thread (`bilbo.watchdog.run_loop`) inside the capture container, running every 2 min. It reads the newest `entries.timestamp` from SQLite, and if it's older than `WATCHDOG_ALERT_AFTER_MIN` (default 5 min), it sends a Telegram alert. State machine in `data/watchdog-state.json` tracks `outage_started_at` / `last_alert_at` so a multi-hour outage gets one initial ping, one reminder per `WATCHDOG_REMINDER_AFTER_MIN` (default 60 min), and one "captures resumed" ping on recovery — no spam, but no silent multi-hour gaps either.

The "host off" failure mode is left uncovered. The right fix for that is a push-style heartbeat to a cloud endpoint that alerts when it stops hearing from the monitor; out of scope for the current iteration. Mitigated separately by `pmset -a sleep 0 disksleep 0 autopoweroff 0 standby 0` so the host won't enter idle sleep while plugged in.

## Temporal State Smoothing — added 2026-04-14

The primary `state` field was originally derived per-frame from the eye-state classifier (`eyes_open → Awake`, `eyes_closed → Asleep`). That's brittle: one mis-classified frame or a one-second REM blink would flip the state and make the timeline look like dozens of tiny wake-ups between real sleep blocks. The wake alert had its own 2-of-3 look-back confirmation on top, but the stored `state` itself — the thing the dashboard Timeline, Events feed, and SQL aggregations read — was still raw per-frame.

| Approach | False flip rate | Implementation |
|---|---|---|
| Per-frame (old) | High — every noisy frame flips `state` | Single-frame `eyes_open → Awake` mapping at capture time |
| Smooth at read time (dashboard only) | Low — but every consumer needs to re-implement | Timeline code walks history each render |
| **Smooth at write time (chosen)** | **Low — one consistent definition across all readers** | `bilbo.state::smooth_state_temporal` runs before persistence |

**Decision:** smooth at write time, in `monitor.py` right before the entry hits SQLite / JSONL. The rule: within the last `STATE_CONFIRM_WINDOW = 6` baby-present frames (including the current one), a run of `STATE_CONFIRM_RUN = 4` consecutive `eyes_open` readings confirms `Awake`; same for `eyes_closed → Asleep`. Otherwise carry forward the previous smoothed state; degrade to `Unknown` only if there's no Awake/Asleep in history to carry. Non-present frames and intermediate classes (`face_not_visible`, `low_confidence`) break the run, as do cloud-API fallback frames that don't populate `eyeState`.

**Preserving the raw signal.** Each entry now carries a `rawState` field holding the per-frame (unsmoothed) state. Nothing in the live pipeline reads it — it exists solely so `bilbo-backfill-state` can re-smooth historical entries when the thresholds change, without feeding already-smoothed `state` back into the smoother. The frame-level `eyeState` classifier label is never touched by the smoother and is still the thing the dashboard shows in the block-detail view and the thing the user corrects per-frame.

**One-time backfill.** `bilbo-backfill-state` walks the DB in timestamp order and rewrites `state` + `rawState` on every entry using the same smoothing function. Non-destructive to `eyeState`, `eye_state_edited`, and all user corrections. Run once after deploying the smoothing change, or any time the window/run thresholds are adjusted.

**Upstream companion: primary-field inference backfill.** The state smoother reads `eyeState` from history. Pre-BIRDEYE-flip cloud-primary frames don't have an `eyeState` because the cloud API never emitted one, so smoothing those frames just carries forward Unknown. `bilbo-backfill-primary --start <ISO-ts>` re-runs BIRDEYE with the currently deployed weights over a time window and writes the new predictions into the **primary** `eyeState` / `faceBbox` / `presenceConfidence` / `eyeConfidence` fields (and also refreshes the `shadow` audit dict so it stays consistent). Corrected rows (`eye_state_edited = 1`) are skipped by default so user ground-truth labels are preserved. After running this, re-run `bilbo-backfill-state` so the smoother re-fires over the refreshed eye-state signal. This is different from `bilbo-monitor --backfill-shadow` which writes into the shadow audit dict *only* and leaves the primary fields (and therefore the smoother's input) untouched.

**Interaction with the wake alert.** The 2-of-3 wake confirmation in `alerts.check_wake_confirmation` is now strictly weaker than the smoothing rule — a smoothed `Awake` already implies at least 4 consecutive `eyes_open` in the look-back window. The wake check is kept because it still enforces the prior-Asleep gate and the cooldown, but the quorum itself is trivially satisfied on any Asleep→Awake transition. Not a bug, just a note for anyone reading the alert path and wondering why it looks redundant.

**Why a write-time rule and not a render-time rule.** The dashboard, the report skill, the SQLite aggregation queries used by the Pipeline/Events panels, and any future consumer of `entries.state` would otherwise each have to re-derive the same rule. Centralizing at write time means there's exactly one definition of Awake/Asleep in the system, and it lives in one 60-line module.

**History lookup must go through SQLite (incident 2026-04-15).** The live smoother in `monitor.py` calls `db.get_recent_entries(n)` — an indexed `LIMIT` query — to fetch the previous `STATE_CONFIRM_WINDOW - 1` frames. For ~24 hours before this was discovered it was calling `bilbo.storage.files.get_recent_entries(n)` instead, which tails the JSONL file with a fixed `n * 600` byte budget. Real entries had grown to ~1,455 bytes (shadow dict + experiments dict + faceBbox), so asking for 5 history frames was returning only 2 — the 4-of-6 consecutive rule could never fire, every present frame fell through to carry-forward, and carry-forward cascaded into `Unknown` indefinitely. The timeline showed 498 consecutive Unknown blocks in what should have been a clear Asleep stretch. The fix was twofold: (1) switch the live smoother's history read to SQLite (matches the architectural rule `read paths must use SQLite via bilbo.storage.db`, which is already in the dual-write notes), and (2) make `bilbo.storage.files.get_recent_entries` adaptive — double the read window on underflow — so the other callers (`alerts.should_burst`, `alerts.check_wake_confirmation`) stop silently getting fewer rows than they asked for. The lesson is less about the byte budget and more about **silent undercounts are the worst kind of bug in a smoothing rule**: the consumer can't tell the difference between "history had no matching run" and "history was truncated before the rule could see the run." Anything downstream of a rolling-window rule should assert that the window it received is the size it requested. Same-day follow-up: all runtime JSONL readers migrated to SQLite — `alerts.should_burst` / `check_wake_confirmation`, `detect.detect_empty_bassinet`, the cloud-fallback position heuristic in `monitor.py`, and `dashboard/app.py::api_sleep_stats` (which was previously slurping the entire JSONL on every request). JSONL read paths now exist only in training (`train_classifiers.py`) and historical backtest/audit tools (`cli.py`), both of which intentionally want the append-only log as ground truth.

## Unknown → Awake absorption (added 2026-04-15)

The temporal smoother's 4-of-6 consecutive-eye-state rule produces a lot of tolerable `Unknown` frames between confirmed states — every time BIRDEYE's face detector briefly loses the baby (hand over eyes, crib shift, face pressed into mattress, eye-state confidence dip), the run breaks and subsequent frames carry forward the previous state. When the previous state was also Unknown (e.g., long periods of face_not_visible during fussy play), the timeline shows large Unknown stretches that are almost certainly awake time from a human observer's perspective.

The post-smoothing rule: **when a new `Awake` state is confirmed, any immediately-preceding contiguous run of `Unknown` + `babyPresent` frames whose total span is less than `UNKNOWN_ABSORB_MAX_MINUTES` (default 15) is retroactively reclassified as `Awake`.**

| Approach | Accuracy | Complexity | Downgrade risk |
|---|---|---|---|
| None (Unknown stays Unknown) | Dashboard under-reports awake time | Zero | None, but data is misleading |
| Symmetric (also Unknown → Asleep) | Over-applies to pre-wake ambiguity | Medium | Real "woke up briefly then back to sleep" gets laundered |
| **Asymmetric Unknown → Awake only (chosen)** | Matches the observable signal — the wake is strong, the pre-wake ambiguity is unreliable | Low | Only risk is under-absorbing, which preserves the raw signal |

**Where it runs.** The helper `unknown_prefix_to_absorb` in `bilbo.state` takes a candidate "current" entry and the recent history window, walks backward through contiguous Unknown+present frames, measures the span, and returns the rows to rewrite if the span is within budget. It's called from two places:

- `monitor.py` (live path): after the current entry is persisted, it fetches a history window larger than the absorption budget (`max(STATE_CONFIRM_WINDOW, UNKNOWN_ABSORB_MAX_MINUTES + 5)` rows ≈ 20 minutes), calls the helper, and rewrites each absorbed historical row via `db.update_entry`. The write is post-insert so a failure partway through leaves the DB consistent at the pre-absorption state.
- `bilbo-backfill-state` (historical): after the pass-1 forward-smoothing sweep, a pass-2 single-pass walk accumulates contiguous Unknown runs and flushes them to Awake when a terminating Awake is within budget. A pass-3 diff against stored state builds the SQL update batch.

**Boundaries that break the run.** Asleep, Awake, not_present, or any non-Unknown state stops the backward walk. A baby-removal (`not_present`) in the middle of the window means only the post-removal Unknown frames can be absorbed, not anything before the removal. `eye_state_edited = 1` corrections are stored in the `eye_state` column but not read by the absorber — the absorber operates on the derived `state` field only, so user eye-state corrections already propagate through the smoother on re-smooth.

## FallingAsleep (putdown-pattern absorption, added 2026-04-20)

Companion rule for the mirror-image of Unknown→Awake, specifically targeting the *putdown-to-sleep* case. The pattern is narrow by design: `not_present → Unknown+babyPresent (run) → Asleep`. When the live smoother (or `bilbo-backfill-state` pass-3) confirms `Asleep` AND the preceding contiguous `Unknown+babyPresent` run is bookended by `not_present`, the run is reclassified by span:

| Run span | New state | Reason |
|----------|-----------|--------|
| ≤ `FALLING_ASLEEP_MAX_MINUTES` (default 30) | `FallingAsleep` | Textbook putdown → settle → sleep. The ambiguous frames are the transition itself, and it's useful to see it as a distinct color on the timeline. |
| > 30 min | `Awake` | Baby was in the bassinet "crib-awake" for a long stretch before dozing off — the ambiguous frames were mostly awake time, not the sleep transition. |

**Why only the `not_present`-bookended pattern?** A pattern like `Awake → Unknown → Asleep` (no removal in between) is also a natural fall-asleep sequence, but the absorption there would conflict with the existing asymmetric-toward-Awake rationale (pre-sleep ambiguity is a weaker signal than the sleep confirmation itself). Limiting this to the putdown case avoids reclassifying pre-sleep ambiguity that wasn't preceded by a fresh placement.

**Why `FallingAsleep` is a first-class state value (not a flag on `Awake`).** The timeline, bassinet chart, and corrections-breakdown chip all want to distinguish this from true crib-awake time — same reason Unknown was elevated out of the Asleep bucket in 2026-04-14. The dashboard renders it as light green between Awake (yellow) and Asleep (green). Alerts are unaffected: `should_alert_asleep` requires `"Awake" in recent_present_states` to fire, and `FallingAsleep` doesn't match, so the putdown case stays silent (as it was before this rule).

**Pass ordering interaction (backfill).** `bilbo-backfill-state` runs Pass 2 (Unknown→Awake) before Pass 3 (FallingAsleep putdown). If the same Unknown run is eligible for both — for instance, a wake-up that shortly preceded sleep — Pass 2 wins (flips to Awake), and Pass 3 sees no Unknown run to classify. This matches live-path behaviour and the design intuition: a wake-up that shortly precedes sleep is really a wake-up.

Helper: `bilbo.state.putdown_prefix_to_absorb`. Threshold: `FALLING_ASLEEP_MAX_MINUTES` in `bilbo.config`.

## Frame Retention

Captured frames are needed for retraining classifiers, backtesting detection changes, and reviewing alerts. More retention means more data but more disk.

| Retention | Disk budget | Use case |
|-----------|------------|----------|
| 1 day | ~1.5 GB | Debugging only |
| 3 days | ~4.5 GB | Short-term review |
| 7 days | ~10.5 GB | Weekly retraining, single-week backtests |
| **~17 days (chosen)** | **10 GB cap** | **Multi-week backtests + retraining on long history** |

**Decision:** 10 GB cap. At 1-min intervals and ~433 KB/frame this holds roughly 17 days of frames — down from ~67 days at the old 4-min cadence, which is the main tradeoff of the faster sampling rate. Still enough for multi-week backtests and for retraining on a meaningful history. Oldest-first pruning kicks in once the directory exceeds the cap. If you want more retention, either raise `MAX_FRAMES_KB` in `bilbo.config` or move frames to external storage on a nightly cron.

**Training-aware exception (issue #5).** `enforce_disk_limit()` skips pruning while a training run is active (`bilbo.training_state.is_running()`). Long trainings iterate `self.samples` populated at `__init__`; if retention deletes frames mid-run, `__getitem__` hits `None` images and recurse-resamples, which gets very slow once many adjacent samples go missing. Disk overshoot during a training run is bounded — at 1 frame/min × ~600 KB, a 6-hour run adds ~210 MB, well under the 10 GB cap.

## Storage: SQLite vs JSONL

How to store and query monitoring data efficiently.

| Approach | Read speed (24h query) | Write safety | Query flexibility |
|----------|----------------------|-------------|-------------------|
| JSONL only (old) | ~50ms (scan 2800 lines) | Append-only, no corruption | grep/jq only |
| **SQLite + JSONL backup (chosen)** | **~6ms (indexed query)** | **Atomic writes, WAL mode** | **SQL aggregation** |

**Decision:** SQLite as primary read/write, JSONL as append-only backup. Dashboard APIs went from ~50ms to ~6ms. Corrections count went from ~30ms (full scan) to ~0.1ms (indexed).

- **JSONL only** — simple, grep-friendly, but O(n) for every query. At 1440 frames/day, the file grows fast.
- **SQLite + JSONL (chosen)** — indexed queries, atomic writes, SQL aggregation for dashboard stats. JSONL backup preserved for raw access and disaster recovery.

**Important subtlety — some fields live in SQLite only.** The JSONL file captures only the primary pipeline fields written by `monitor.py` at capture time. Secondary fields written by analysis scripts or the shadow-experiment framework (notably `bboxImpact`, `experiments`, and `faceBboxCorrected`) live in SQLite only, because they're derived after the fact and don't need append-only durability. Any code path that writes back to the `data` column **must merge with the existing SQLite blob**, not overwrite it — otherwise the SQLite-only fields are silently stripped. The retrain re-inference loop in `bilbo.cli::_reinfer_corrections_against_current_model` does this merge explicitly; treat that as the canonical pattern for any future SQLite-write path.
