#!/usr/bin/env python3
"""Machine-usage snapshot for the dashboard's System Load panel.

Gathers load average, CPU cores, memory pressure, disk usage (root +
workspace), baby-monitor data-dir sizes, and the top CPU-consuming
processes — with baby-monitor-related processes split out so the UI can
highlight them (retrain, monitor tick, backfill, bbox_impact, watchdog).

Pure stdlib — no psutil dependency — so it runs in the minimal dashboard
venv without adding to requirements.

Run as a CLI for an on-terminal snapshot:
    python dashboard/system_usage.py           # human-readable
    python dashboard/system_usage.py --json    # raw JSON (for debugging)
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bilbo.config import DATA_DIR, MODELS_DIR  # noqa: E402

# Cache directory-size lookups (du -sk) for this many seconds.
# `frames/` has ~10k files; a recursive walk takes a few seconds, which
# we don't want in the hot path of a 10s-poll UI.
_DU_CACHE_TTL_SEC = 60
_du_cache: dict[str, tuple[float, int | None]] = {}

BABY_MONITOR_KEYWORDS = (
    "train_classifiers",
    "monitor.py",
    "dashboard/app.py",
    "backfill_birdeye_primary",
    "backfill_state",
    "bbox_impact",
    "experiments_backfill",
    "watchdog.py",
    "run_single_inference",
)


def _load() -> dict:
    """Load average + cores. Trend = 1-min minus 5-min."""
    one, five, fifteen = os.getloadavg()
    cores = os.cpu_count() or 1
    return {
        "oneMin": round(one, 2),
        "fiveMin": round(five, 2),
        "fifteenMin": round(fifteen, 2),
        "cores": cores,
        "ratio": round(one / cores, 2),
        "trend": round(one - five, 2),
    }


def _memory() -> dict:
    """Memory snapshot via `vm_stat` (macOS) — page counts → bytes.

    `usedPct` is the *memory pressure* metric — `(active + wired +
    compressed) / total`. macOS aggressively pins file-cache pages in the
    "inactive" pool, which the kernel reclaims the moment any allocator
    needs them; treating those as "used" makes a healthy 8 GB box look
    99 % full and obscures real pressure. `cachedPct` and `freePct` are
    surfaced alongside so the breakdown adds up to 100 %.

    Reconciliation note: a process's RSS (which `ps` reports) excludes
    file cache entirely, so summing `memPct` across processes will only
    approach `usedPct` — never `(usedPct + cachedPct)` — by design.
    """
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except Exception:
        return {}
    page_size = 4096
    lines = out.splitlines()
    if lines and "page size of" in lines[0]:
        try:
            page_size = int(
                lines[0].split("page size of")[1].split("bytes")[0].strip()
            )
        except ValueError:
            pass

    def _pages(key: str) -> int:
        for line in lines:
            if line.startswith(key):
                return int(line.split(":")[1].strip().rstrip("."))
        return 0

    free = _pages("Pages free")
    active = _pages("Pages active")
    inactive = _pages("Pages inactive")
    wired = _pages("Pages wired down")
    compressed = _pages("Pages occupied by compressor")
    total_pages = free + active + inactive + wired + compressed
    total = total_pages * page_size
    used_bytes = (active + wired + compressed) * page_size
    inactive_bytes = inactive * page_size
    free_bytes = free * page_size
    return {
        "totalBytes": total,
        "freeBytes": free_bytes,
        "activeBytes": active * page_size,
        "inactiveBytes": inactive_bytes,
        "wiredBytes": wired * page_size,
        "compressedBytes": compressed * page_size,
        # Pressure metric — ignores reclaimable cache.
        "usedPct": round(used_bytes / total * 100, 1) if total else None,
        # File cache + speculative pages — reclaimable on demand.
        "cachedPct": round(inactive_bytes / total * 100, 1) if total else None,
        "freePct": round(free_bytes / total * 100, 1) if total else None,
        # Convenience: what an allocator effectively has access to.
        "availableBytes": free_bytes + inactive_bytes,
    }


def _disk() -> list:
    """Disk usage for root + workspace volumes."""
    rows = []
    for label, path in (("root", "/"), ("workspace", str(SKILL_DIR))):
        try:
            st = shutil.disk_usage(path)
            rows.append({
                "label": label,
                "path": path,
                "totalBytes": st.total,
                "freeBytes": st.free,
                "usedPct": round((st.total - st.free) / st.total * 100, 1),
            })
        except OSError:
            pass
    return rows


def _du_cached(path: Path) -> int | None:
    """Recursive dir size in bytes, cached for _DU_CACHE_TTL_SEC.

    Uses `du -sk` rather than os.walk — for ~10k files in data/frames/ the
    shell version is ~10× faster because it avoids Python attr lookups per
    entry and the kernel stats are already warm.
    """
    key = str(path)
    now = time.monotonic()
    cached = _du_cache.get(key)
    if cached and now - cached[0] < _DU_CACHE_TTL_SEC:
        return cached[1]
    if not path.exists():
        _du_cache[key] = (now, None)
        return None
    try:
        out = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
        kb = int(out.split()[0])
        size = kb * 1024
    except Exception:
        size = None
    _du_cache[key] = (now, size)
    return size


def _baby_monitor_sizes() -> dict:
    db_path = DATA_DIR / "monitor.db"
    return {
        "dataDirBytes": _du_cached(DATA_DIR),
        "framesDirBytes": _du_cached(DATA_DIR / "frames"),
        "modelsDirBytes": _du_cached(MODELS_DIR),
        "monitorDbBytes": db_path.stat().st_size if db_path.exists() else None,
    }


def _ps_full() -> list[dict]:
    """Every process with pid/cpu/mem/rss/etime/full-command.

    Single ps invocation feeds both the top-N list and the baby-monitor
    process classifier — running ps twice would double-count since the
    numbers drift between calls.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid,pcpu,pmem,rss,etime,command"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            rows.append({
                "pid": int(parts[0]),
                "cpuPct": float(parts[1]),
                "memPct": float(parts[2]),
                "rssKb": int(parts[3]),
                "etime": parts[4],
                "command": parts[5],
            })
        except ValueError:
            continue
    return rows


def _short_command(cmd: str) -> str:
    """Trim a full argv string to something readable in a table cell."""
    # Keep the script basename if it's a python-runs-script invocation.
    for token in cmd.split():
        if token.endswith(".py"):
            return Path(token).name
    # Otherwise take the executable basename.
    head = cmd.split(None, 1)[0] if cmd else ""
    return Path(head).name or cmd[:40]


def _classify(procs: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (top_by_cpu, top_by_memory, baby_monitor_processes).

    Baby-monitor procs are extracted by keyword match on the full command;
    they may also appear in the top-by-* lists if they're truly busy.

    Top-by-memory is sorted by RSS (resident set size) — the actual memory
    pages held in physical RAM. memPct from `ps` reflects the same value
    relative to total system memory but RSS is what's user-meaningful when
    looking at a single process.
    """
    bm = []
    for p in procs:
        script = next((k for k in BABY_MONITOR_KEYWORDS if k in p["command"]), None)
        if script:
            bm.append({**p, "script": script, "command": _short_command(p["command"])})
    top_cpu = sorted(procs, key=lambda r: r["cpuPct"], reverse=True)[:8]
    top_cpu = [{**p, "command": _short_command(p["command"])} for p in top_cpu]
    top_mem = sorted(procs, key=lambda r: (r.get("rssKb") or 0), reverse=True)[:8]
    top_mem = [{**p, "command": _short_command(p["command"])} for p in top_mem]
    return top_cpu, top_mem, bm


def gather() -> dict:
    procs = _ps_full()
    top_cpu, top_mem, bm_procs = _classify(procs)
    return {
        "asOf": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "load": _load(),
        "memory": _memory(),
        "disk": _disk(),
        "babyMonitor": {
            "sizes": _baby_monitor_sizes(),
            "processes": bm_procs,
        },
        "topProcesses": top_cpu,
        "topByMemory": top_mem,
    }


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def _cli(data: dict) -> None:
    load = data["load"]
    arrow = "↑" if load["trend"] > 0.1 else ("↓" if load["trend"] < -0.1 else "→")
    print(
        f"Load: {load['oneMin']} / {load['fiveMin']} / {load['fifteenMin']}  "
        f"(cores={load['cores']}, ratio={load['ratio']}x {arrow})"
    )
    mem = data["memory"]
    if mem:
        print(f"Memory: {_fmt_bytes(mem.get('totalBytes'))} total  "
              f"{mem.get('usedPct')}% used")
    for d in data["disk"]:
        print(f"Disk [{d['label']}]: {_fmt_bytes(d['freeBytes'])} free, {d['usedPct']}% used")
    sizes = data["babyMonitor"]["sizes"]
    print(
        f"Baby monitor: data={_fmt_bytes(sizes['dataDirBytes'])}  "
        f"frames={_fmt_bytes(sizes['framesDirBytes'])}  "
        f"models={_fmt_bytes(sizes['modelsDirBytes'])}  "
        f"db={_fmt_bytes(sizes['monitorDbBytes'])}"
    )
    if data["babyMonitor"]["processes"]:
        print("Baby monitor processes:")
        for p in data["babyMonitor"]["processes"]:
            print(f"  pid={p['pid']:<7} cpu={p['cpuPct']:>5}%  {p['etime']:>10}  {p['script']}")
    print("Top processes (by CPU):")
    for p in data["topProcesses"][:5]:
        print(
            f"  pid={p['pid']:<7} cpu={p['cpuPct']:>5}% mem={p['memPct']:>4}%  "
            f"rss={_fmt_bytes(p['rssKb'] * 1024):>8}  {p['etime']:>10}  {p['command']}"
        )


if __name__ == "__main__":
    data = gather()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        _cli(data)
