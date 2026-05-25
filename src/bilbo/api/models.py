"""Model-version management — list, latest, rollback.

Reads from MODELS_DIR. The control-api in step 5 will use these directly.
The dashboard does not currently expose a route for any of them; they are
created here so the API surface is uniform for the next layer.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from bilbo.config import MODELS_DIR


def _training_log_entries() -> dict[str, dict]:
    log_path = MODELS_DIR / "training-log.jsonl"
    if not log_path.exists():
        return {}
    out: dict[str, dict] = {}
    for line in log_path.read_text().strip().splitlines():
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        v = entry.get("version")
        if v:
            out[v] = entry
    return out


def list_versions() -> list[dict]:
    """All versioned model dirs in MODELS_DIR, oldest first.

    Each entry: {version, path, active, trainedAt, entriesTotal,
    correctionsCount, auditCount}. `active` is True for the version the
    `latest` symlink currently points at.
    """
    if not MODELS_DIR.exists():
        return []
    version_dirs = sorted(
        [d for d in MODELS_DIR.iterdir() if d.is_dir() and d.name.startswith("v_")],
        key=lambda d: d.name,
    )
    current = latest_version()
    log_entries = _training_log_entries()
    out = []
    for d in version_dirs:
        meta = log_entries.get(d.name, {})
        sources = meta.get("label_sources", {}) or {}
        out.append({
            "version": d.name,
            "path": str(d),
            "active": d.name == current,
            "trainedAt": meta.get("timestamp"),
            "entriesTotal": meta.get("entries_total"),
            "correctionsCount": sources.get("correction", 0),
            "auditCount": sources.get("audit", 0),
            "metrics": meta.get("metrics"),
        })
    return out


def latest_version() -> str | None:
    """Name of the model version the `latest` symlink resolves to, or
    None if the symlink is missing or dangling."""
    latest_link = MODELS_DIR / "latest"
    if not latest_link.is_symlink():
        return None
    try:
        return latest_link.resolve().name
    except OSError:
        return None


def rollback(version: str) -> dict:
    """Repoint the `latest` symlink at `version` (supports unique partial
    matches). Returns {ok, from, to} or {ok: False, error}.
    """
    if not MODELS_DIR.exists():
        return {"ok": False, "error": "models dir not found"}

    target: Path | None = MODELS_DIR / version
    if not target.is_dir():
        matches = [d for d in MODELS_DIR.iterdir()
                   if d.is_dir() and d.name.startswith("v_") and version in d.name]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            return {"ok": False, "error": f"ambiguous version: {[m.name for m in matches]}"}
        else:
            return {"ok": False, "error": f"version not found: {version}"}

    has_pt = any(target.glob("*.pt"))
    if not has_pt:
        return {"ok": False, "error": f"version {target.name} has no .pt files"}

    latest_link = MODELS_DIR / "latest"
    old_version = None
    if latest_link.is_symlink():
        old_version = latest_link.resolve().name
        latest_link.unlink()
    latest_link.symlink_to(target.name)

    # Mirror behavior of cli.cmd_rollback: also copy weights to the top of
    # MODELS_DIR for backward compatibility.
    for pt_file in target.glob("*.pt"):
        shutil.copy2(pt_file, MODELS_DIR / pt_file.name)

    return {"ok": True, "from": old_version, "to": target.name}
