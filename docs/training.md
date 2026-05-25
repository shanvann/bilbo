# Continuous Improvement Loop

How frame-level corrections turn into better classifiers, with full
provenance.

```
1. Monitor    — BIRDEYE decides every non-empty frame; cloud API fallback
                on ~1-2% of frames writes the shadow audit dict anyway.
2. Review     — dashboard shows BIRDEYE vs corrected-ground-truth Macro F1;
                block-level review checkboxes build up trusted labels.
3. Correct    — edit eye state per-frame or per-block in the dashboard;
                also draw corrected face bboxes for IoU + bbox-impact
                analyses.
4. Retrain    — manual only. Click the dashboard button (spawns a
                bilbo-training Docker container), or run
                `bilbo-monitor --retrain` on the host.
5. Refresh    — after a successful retrain, the post-retrain chain runs
                automatically:
                  • bilbo-backfill-primary (7-day window by default)
                  • bilbo-backfill-state (re-smooth `state`)
                  • bilbo-bbox-impact --force
                so the dashboard Per-class / Bbox-impact numbers track
                the new model. Opt out with --skip-post-retrain.
6. Verify     — post-retrain re-inference on corrected frames; the run's
                training_runs row gets a metrics.correction_agreement
                sub-dict patched in.
7. Track      — versioned model dirs with metrics + deltas + rollback.
                Last 20 versions kept.
8. Shadow     — train an alternate candidate (different crop size /
                architecture / loss), register in `experiments.json`,
                observe Δ on the dashboard, flip with `bilbo-promote-experiment`.
                See docs/shadow-to-prod-playbook.md for the full lifecycle.
```

**Label priority** (during training):
dashboard corrections > audit disagreements > cloud/BIRDEYE model labels.

**Model versioning:** timestamped directories
(`pipeline/models/v_YYYYMMDD_HHMMSS/`), a `latest` symlink, the
`training_runs` table for metrics, last 20 versions kept on disk.
Rollback flips the symlink; `bilbo-monitor --list-models` shows the
history and `bilbo-monitor --rollback VERSION` reverts.

**Training state.** A `bilbo-training` Docker container is the source
of truth — `bilbo.training_state.is_running()` checks Docker first,
with a legacy PID-fallback for host-dev runs (`bilbo-monitor --retrain`
directly on the host). The dashboard's `/api/retrain` rejects starts
when one is already running and `/api/retrain/abort` stops the
container. Don't store training status in process-local globals.

**Retraining is manual-only.** There is no scheduled retrain —
cloud-API labels aren't trusted training signal without manual review
first. Retrain when a batch of user corrections is ready (dashboard
button, `bilbo-monitor --retrain` on a host, or
`docker compose run --rm` against `bilbo:latest` with `bilbo-train`).
