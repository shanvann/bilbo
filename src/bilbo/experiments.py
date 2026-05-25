"""Shadow experiment framework for BILBO.

Runs alternative pipeline variants alongside the production BIRDEYE cascade.
Each experiment reads the production result and the raw frame, computes its
own answer, and returns a result dict that gets stored under
``entry["experiments"][<name>]``. The primary pipeline output is never
modified — experiments are read-only observers.

Guiding principle: a broken experiment must never abort a capture tick.
``run_all()`` catches any exception raised by an experiment's ``run()`` and
logs it, but the capture pipeline continues.

Adding a new experiment
-----------------------
1. Subclass ``Experiment``, give it a unique ``name`` and a 1-line
   ``description``, and implement ``run(frame_path, entry, *, prod_result)``.
2. Append an instance to ``_REGISTRY`` at the bottom of this file (or register
   from a separate module if you prefer).
3. Run ``python scripts/experiments_backfill.py --name <your-name>`` to
   populate historical data so the dashboard has immediate comparison numbers.

Per-experiment result schema
----------------------------
Every result dict should include these keys so dashboard rendering stays
consistent across experiments:

    {
        "state":        "Asleep" | "Awake" | "Unknown" | "not_present",
        "eyeState":     "eyes_open" | "eyes_closed" | None,
        "eyeConfidence": float in [0, 1] | None,
        "modelVersion": str — identifier for the model/config used,
        "latencyMs":    float — per-frame inference time (for cost tracking),
        "ranAt":        ISO-8601 UTC timestamp,
    }

Anything else the experiment wants to record (crop dims, intermediate
hooks, alternate bbox) is allowed as extra keys — aggregation in
``db.get_experiment_stats`` only reads the standard keys above.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("monitor")


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Experiment(ABC):
    """Base class for a shadow pipeline experiment.

    Subclasses set ``name`` and ``description`` as class attributes and
    implement ``run()``. Expensive resources (model checkpoints, transforms)
    should be lazily loaded inside ``run()`` or cached on the instance — the
    registry holds one instance per experiment for the lifetime of the
    process, so lazy-loaded state persists across capture ticks.
    """

    #: Short, unique identifier. Must be filesystem-safe — it's used as a
    #: JSON key in ``entry.experiments`` and as a CLI argument to the
    #: backfill script.
    name: str = ""

    #: One-line human description. Shown on the dashboard next to the
    #: experiment's metrics row.
    description: str = ""

    @abstractmethod
    def run(
        self,
        frame_path: Path,
        entry: dict,
        *,
        prod_result: dict | None = None,
    ) -> dict | None:
        """Run the experiment on a single frame.

        Arguments
        ---------
        frame_path: Path
            Absolute path to the JPEG frame on disk.
        entry: dict
            The flattened entry dict (same shape that ``db.get_entries()``
            returns). Includes ``faceBbox``, ``faceBboxCorrected`` if
            present, and any other stored fields. **Never mutate this dict.**
        prod_result: dict | None
            The production pipeline's result for this frame (what BIRDEYE
            produced at capture time, mapped through
            ``birdeye_result_to_shadow_blob``). May be None for entries
            where prod didn't run, in which case the experiment should
            usually return None itself.

        Returns
        -------
        dict | None
            A result dict following the schema documented at the top of
            this module, or None to skip this frame (e.g. no face was
            detected, or the experiment isn't applicable to this entry).
            Returning None is semantically different from raising — a None
            return means "not applicable," an exception means "broken."
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all(
    frame_path: Path,
    entry: dict,
    *,
    prod_result: dict | None = None,
    names: list[str] | None = None,
) -> dict[str, dict]:
    """Run every registered experiment against a frame.

    Returns a dict mapping experiment name → result dict. Experiments that
    return None (not applicable) are omitted. Experiments that raise are
    logged and omitted — they do not propagate.

    ``names``, if given, filters the registry to only those experiments.
    Used by the backfill CLI to run a single experiment by name.
    """
    results: dict[str, dict] = {}
    for exp in _REGISTRY:
        if names is not None and exp.name not in names:
            continue
        t0 = time.monotonic()
        try:
            result = exp.run(frame_path, entry, prod_result=prod_result)
        except Exception as e:  # noqa: BLE001 — deliberately broad
            log.warning(
                "experiment %s failed on %s: %s",
                exp.name,
                entry.get("timestamp", "?"),
                e,
            )
            continue
        if result is None:
            continue
        # Stamp latency + ranAt if the experiment didn't set them itself.
        result.setdefault("latencyMs", round((time.monotonic() - t0) * 1000, 2))
        result.setdefault("ranAt", _iso_now())
        results[exp.name] = result
    return results


def get_registry() -> list[Experiment]:
    """Return the live registry list. Mutating the returned list mutates the
    registry — this is the intentional way to register an experiment at
    runtime from a plugin or test."""
    return _REGISTRY


def get_experiment(name: str) -> Experiment | None:
    """Look up a registered experiment by name, or None if not found."""
    for exp in _REGISTRY:
        if exp.name == name:
            return exp
    return None


# ---------------------------------------------------------------------------
# Generic eye-state shadow experiment
# ---------------------------------------------------------------------------
#
# Eye-state variants — different crop sizes, different training runs,
# different rollback baselines — share all the plumbing (lazy load,
# crop-and-classify, disagreement flag). Only a handful of parameters
# actually differ per variant:
#
#   - ``name``: the identifier the dashboard and DB use
#   - ``description``: one-line human hint shown in the card
#   - ``crop_size``: classifier input resolution (must match training)
#   - ``experiment_tag``: folder under pipeline/models/experiments/ where
#     the checkpoint lives
#
# To avoid hand-writing a new subclass per variant, ``EyeStateShadowExperiment``
# takes all four as ``__init__`` arguments and is instantiated from a
# JSON manifest at ``scripts/lib/experiments.json``. The promotion
# script edits that manifest as part of its atomic flow — no Python
# source edits in the middle of a rollout.
#
# History of the slot this class replaces:
#
#   * 2026-04-13 — first experiment ``eye_state_hires_448_retrained``
#     proved out the infrastructure and showed +34 pts on the correction
#     subset vs prod 224 eye-state.
#   * 2026-04-14 — 448 was promoted to prod; shadow inverted to
#     ``eye_state_224_legacy`` (the pre-flip weights) so the dashboard
#     could report "is new prod still winning vs the model it replaced?"
#     as a rollback-observability signal. Both of those are now just
#     manifest entries, not hardcoded classes.


class EyeStateShadowExperiment(Experiment):
    """Generic eye-state shadow — instantiated from a manifest entry.

    Loads an eye-state checkpoint from
    ``pipeline/models/experiments/<experiment_tag>/latest/eye_state_classifier.pt``
    and runs it at ``crop_size`` input against every frame prod also
    scores. The dashboard's Shadow Experiments card reports agreement
    rate and Δ-vs-prod on the correction subset.

    Checkpoint may be absent (never trained, deleted, or the user hasn't
    run the snapshot step yet). In that case ``run()`` returns None for
    every frame — the experiment is a no-op rather than an error. This
    lets a new experiment be registered in the manifest before the
    weights exist, and it starts working automatically once the file
    appears.

    Rollback contract: any experiment in the manifest can be promoted to
    prod by ``scripts/promote_experiment.py --tag <experiment_tag>``,
    which preserves the current prod weights as a new manifest entry
    before overwriting. The promotion is always reversible through the
    same command applied to the preserved tag.
    """

    def __init__(
        self,
        name: str,
        description: str,
        crop_size: int,
        experiment_tag: str,
    ):
        self.name = name
        self.description = description
        # Underscored attrs retained for back-compat with any code that
        # introspected the pre-refactor class constants. New code reads
        # the public attrs below.
        self._CROP_SIZE = int(crop_size)
        self._EXPERIMENT_TAG = str(experiment_tag)
        self.crop_size = self._CROP_SIZE
        self.experiment_tag = self._EXPERIMENT_TAG
        # Lazy-loaded on first applicable call so the experiment can be
        # registered before the model file exists.
        self._model = None
        self._transform = None
        self._device = "cpu"
        self._model_version: str | None = None
        self._crop_bassinet = None
        self._crop_face = None
        self._cv2 = None
        self._torch = None
        self._classes = None
        self._probed_missing = False

    def _checkpoint_path(self) -> Path | None:
        """Return the resolved path to the experiment's eye-state checkpoint,
        or None if the expected location doesn't exist yet. Matches prod's
        filename convention (``eye_state_classifier.pt``) so the same
        training script path works for both prod and experiments."""
        from bilbo.config import MODELS_DIR
        exp_latest = MODELS_DIR / "experiments" / self._EXPERIMENT_TAG / "latest"
        if not exp_latest.is_symlink() and not exp_latest.is_dir():
            return None
        target = exp_latest / "eye_state_classifier.pt"
        if not target.exists():
            return None
        return target

    def _model_version_from_symlink(self) -> str:
        from bilbo.config import MODELS_DIR
        import os
        exp_latest = MODELS_DIR / "experiments" / self._EXPERIMENT_TAG / "latest"
        if exp_latest.is_symlink():
            try:
                return Path(os.readlink(exp_latest)).name
            except OSError:
                pass
        return "unknown"

    def _lazy_init(self) -> bool:
        """Load the 448 eye-state checkpoint if it exists. Returns False if
        the checkpoint is missing — the experiment is a no-op in that case
        (returns None from run() until the user trains it)."""
        if self._model is not None:
            return True

        checkpoint = self._checkpoint_path()
        if checkpoint is None:
            if not self._probed_missing:
                log.info(
                    "experiment %s: checkpoint not yet trained, skipping all frames "
                    "(train with: python scripts/train_classifiers.py --model eye-state "
                    "--eye-crop-size %d --experiment-tag %s ...)",
                    self.name, self._CROP_SIZE, self._EXPERIMENT_TAG,
                )
                self._probed_missing = True
            return False

        import cv2
        import torch
        from torchvision import transforms

        from bilbo.pipeline.classifiers import (
            EYE_STATE_CLASSES,
            _build_mobilenet,
            crop_bassinet,
            crop_face,
        )
        from bilbo.pipeline.local_pipeline import _check_available

        if not _check_available():
            raise RuntimeError("birdeye dependencies unavailable")

        model = _build_mobilenet(num_classes=len(EYE_STATE_CLASSES))
        state = torch.load(checkpoint, map_location=self._device, weights_only=True)
        model.load_state_dict(state)
        model.to(self._device)
        model.eval()
        self._model = model
        self._classes = list(EYE_STATE_CLASSES)
        self._torch = torch
        self._cv2 = cv2
        self._crop_bassinet = crop_bassinet
        self._crop_face = crop_face
        self._transform = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize((self._CROP_SIZE, self._CROP_SIZE)),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            ),
        ])
        self._model_version = self._model_version_from_symlink()
        log.info(
            "experiment %s: loaded checkpoint %s at version %s",
            self.name, checkpoint, self._model_version,
        )
        return True

    def run(
        self,
        frame_path: Path,
        entry: dict,
        *,
        prod_result: dict | None = None,
    ) -> dict | None:
        bbox_norm = entry.get("faceBbox")
        if not (isinstance(bbox_norm, dict) and all(k in bbox_norm for k in ("x1", "y1", "x2", "y2"))):
            return None

        if not self._lazy_init():
            return None

        frame = self._cv2.imread(str(frame_path))
        if frame is None:
            return None

        bassinet = self._crop_bassinet(frame)
        bh, bw = bassinet.shape[:2]
        pixel_bbox = (
            int(round(bbox_norm["x1"] * bw)),
            int(round(bbox_norm["y1"] * bh)),
            int(round(bbox_norm["x2"] * bw)),
            int(round(bbox_norm["y2"] * bh)),
        )
        face_crop = self._crop_face(bassinet, pixel_bbox)
        if face_crop is None or face_crop.size == 0:
            return None

        rgb = self._cv2.cvtColor(face_crop, self._cv2.COLOR_BGR2RGB)
        tensor = self._transform(rgb).unsqueeze(0).to(self._device)
        with self._torch.no_grad():
            logits = self._model(tensor)
            probs = self._torch.softmax(logits, dim=1)[0]
        pred_idx = int(probs.argmax())
        pred_class = self._classes[pred_idx]
        pred_conf = float(probs[pred_idx])

        if pred_class == "eyes_open":
            state = "Awake"
        elif pred_class == "eyes_closed":
            state = "Asleep"
        else:
            state = "Unknown"

        return {
            "state": state,
            "eyeState": pred_class,
            "eyeConfidence": round(pred_conf, 3),
            "modelVersion": self._model_version or "unknown",
            "cropSize": self._CROP_SIZE,
            "faceCropDims": [int(face_crop.shape[1]), int(face_crop.shape[0])],
        }


# ---------------------------------------------------------------------------
# Registry — loaded from scripts/lib/experiments.json
# ---------------------------------------------------------------------------
#
# The registry is a list of Experiment instances constructed from entries
# in a JSON manifest, not a hand-maintained Python list. The manifest
# lives at ``scripts/lib/experiments.json`` next to this file and is
# edited atomically by ``scripts/promote_experiment.py`` during a flip.
#
# Manifest schema::
#
#     {
#       "eye_state": [
#         {
#           "name": "eye_state_224_legacy",
#           "description": "Previous prod model (224×224 input)",
#           "crop_size": 224,
#           "experiment_tag": "eye_state_224_legacy"
#         }
#       ]
#     }
#
# The ``eye_state`` key is a list so multiple eye-state shadows can run
# concurrently (e.g. when you're evaluating a new model against BOTH
# the current prod AND the previous legacy). Other classifier types
# (presence, face_detect) can be added as new top-level keys when they
# grow shadow support.
#
# If the manifest is missing or unreadable the registry starts empty —
# the framework still loads and runs, it just reports no experiments.

_MANIFEST_PATH = Path(__file__).resolve().parent / "experiments.json"


def _load_registry() -> list[Experiment]:
    """Load the experiment registry from the JSON manifest.

    Broken or missing manifest → empty registry, warning logged.  A
    malformed single entry → that entry is skipped, others still load.
    Never raises — the shadow framework must not break the prod pipeline
    under any circumstances.
    """
    if not _MANIFEST_PATH.exists():
        log.info("experiments: no manifest at %s, registry is empty", _MANIFEST_PATH)
        return []
    try:
        import json
        manifest = json.loads(_MANIFEST_PATH.read_text())
    except (OSError, ValueError) as e:
        log.warning("experiments: failed to read manifest %s: %s", _MANIFEST_PATH, e)
        return []

    registry: list[Experiment] = []

    for entry in manifest.get("eye_state") or []:
        try:
            exp = EyeStateShadowExperiment(
                name=entry["name"],
                description=entry.get("description", ""),
                crop_size=entry["crop_size"],
                experiment_tag=entry["experiment_tag"],
            )
        except (KeyError, TypeError, ValueError) as e:
            log.warning(
                "experiments: skipping malformed eye_state entry %r: %s",
                entry, e,
            )
            continue
        registry.append(exp)

    if registry:
        log.info(
            "experiments: loaded %d experiment(s) from manifest: %s",
            len(registry), [e.name for e in registry],
        )
    return registry


_REGISTRY: list[Experiment] = _load_registry()


def reload_registry() -> list[Experiment]:
    """Re-read the manifest and replace the in-memory registry.

    Used by ``scripts/promote_experiment.py`` after it edits the
    manifest so a subsequent ``run_all()`` call in the same process
    sees the new set of experiments. Never called at import time.
    """
    global _REGISTRY
    _REGISTRY = _load_registry()
    return _REGISTRY
