#!/usr/bin/env python3
"""Machine-usage snapshot for the dashboard's System Load panel.

Gathers load average, CPU cores, memory pressure, disk usage, and a
per-container view of the bilbo stack (CPU%, RSS, uptime) via the
Docker SDK. Pre-Docker layout used host `ps` + `vm_stat` (macOS); both
paths are kept for host-dev fallback when the Docker socket isn't
reachable.

Run as a CLI for an on-terminal snapshot:
    python -m bilbo.api.system            # human-readable
    python -m bilbo.api.system --json     # raw JSON (for debugging)
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bilbo.config import BILBO_ROOT, DATA_DIR, MODELS_DIR  # noqa: E402

# Cache directory-size lookups (du -sk) for this many seconds.
# `frames/` has ~10k files; a recursive walk takes a few seconds, which
# we don't want in the hot path of a 10s-poll UI.
_DU_CACHE_TTL_SEC = 60
_du_cache: dict[str, tuple[float, int | None]] = {}


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
    """Memory snapshot. Reads /proc/meminfo on Linux, falls back to vm_stat on macOS.

    `usedPct` is a pressure metric — `(MemTotal - MemAvailable) / MemTotal`
    on Linux, or `(active + wired + compressed) / total` on macOS. Both
    intentionally treat reclaimable file cache as available so a healthy
    box doesn't peg near 100 % from cache pinning. `cachedPct` and
    `freePct` are surfaced alongside.
    """
    if Path("/proc/meminfo").exists():
        try:
            meminfo: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if not parts:
                    continue
                # values are in kB
                meminfo[key] = int(parts[0]) * 1024
        except OSError:
            return {}
        total = meminfo.get("MemTotal", 0)
        if not total:
            return {}
        free = meminfo.get("MemFree", 0)
        available = meminfo.get("MemAvailable", free)
        cached = meminfo.get("Cached", 0) + meminfo.get("Buffers", 0) + meminfo.get("SReclaimable", 0)
        used = total - available
        return {
            "totalBytes": total,
            "freeBytes": free,
            "availableBytes": available,
            "cachedBytes": cached,
            "usedPct": round(used / total * 100, 1),
            "cachedPct": round(cached / total * 100, 1),
            "freePct": round(free / total * 100, 1),
        }
    # macOS host-dev fallback.
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
        "usedPct": round(used_bytes / total * 100, 1) if total else None,
        "cachedPct": round(inactive_bytes / total * 100, 1) if total else None,
        "freePct": round(free_bytes / total * 100, 1) if total else None,
        "availableBytes": free_bytes + inactive_bytes,
    }


def _disk() -> list:
    """Disk usage for root + workspace volumes."""
    rows = []
    for label, path in (("root", "/"), ("workspace", str(BILBO_ROOT))):
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


def _postgres_db_bytes() -> int | None:
    """On-disk size of the Postgres database in bytes (None if unavailable).

    Replaces the old `data/monitor.db` file-size stat — the DB is no longer a
    single file on the shared volume.
    """
    try:
        from bilbo.storage.db import get_connection  # noqa: PLC0415
        row = get_connection().execute(
            "SELECT pg_database_size(current_database()) AS n"
        ).fetchone()
        return row["n"] if row else None
    except Exception:
        return None


def _baby_monitor_sizes() -> dict:
    return {
        "dataDirBytes": _du_cached(DATA_DIR),
        "framesDirBytes": _du_cached(DATA_DIR / "frames"),
        "modelsDirBytes": _du_cached(MODELS_DIR),
        "monitorDbBytes": _postgres_db_bytes(),
    }


_BILBO_CONTAINER_PREFIXES = ("bilbo-", "airgradient-")


def _fmt_etime(seconds: float) -> str:
    """ps-style elapsed time: D-HH:MM:SS / HH:MM:SS / MM:SS."""
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, secs = divmod(s, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:{secs:02d}"
    if hours:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _container_stat(client, container, total_mem_bytes: int) -> dict:
    """Snapshot a single container as a process-table row.

    cpuPct / memPct math:
      cpuPct  — Docker stats give per-container CPU usage in nanoseconds
                and total system usage over the sampling window. Ratio
                × online_cpus × 100 = `docker stats` CPU%.
      memPct  — RSS / host total. Normalizing against host memory (not the
                container limit) keeps the per-row % comparable to the
                headline pressure metric.
    """
    try:
        s = client.api.stats(container.id, stream=False)
    except Exception:
        return {}
    cpu = s.get("cpu_stats", {}) or {}
    precpu = s.get("precpu_stats", {}) or {}
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - precpu.get("cpu_usage", {}).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
    online_cpus = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    cpu_pct = (cpu_delta / system_delta) * online_cpus * 100 if system_delta > 0 and cpu_delta > 0 else 0.0
    mem = s.get("memory_stats", {}) or {}
    mem_usage = mem.get("usage", 0)
    cache = (mem.get("stats", {}) or {}).get("cache", 0) or (mem.get("stats", {}) or {}).get("inactive_file", 0)
    rss_bytes = max(mem_usage - cache, 0)
    mem_pct = (rss_bytes / total_mem_bytes * 100) if total_mem_bytes else 0.0
    started = container.attrs.get("State", {}).get("StartedAt", "")
    etime = "—"
    if started:
        try:
            # StartedAt is RFC3339Nano; strip sub-second precision past 6 digits
            # because fromisoformat in 3.12 still chokes on 9-digit nanos.
            iso = started.rstrip("Z")
            if "." in iso:
                head, frac = iso.split(".")
                iso = head + "." + frac[:6]
            started_dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
            etime = _fmt_etime((datetime.now(timezone.utc) - started_dt).total_seconds())
        except ValueError:
            pass
    name = container.name
    image = (container.image.tags or [container.image.short_id])[0]
    return {
        "pid": container.short_id,
        "cpuPct": round(cpu_pct, 1),
        "memPct": round(mem_pct, 1),
        "rssKb": rss_bytes // 1024,
        "etime": etime,
        "command": image,
        "script": name,
    }


def _bilbo_containers() -> list[dict]:
    """Bilbo + sibling containers as process-table rows.

    Each container's stats() call is a 1-second sample on the Docker
    daemon side, so we parallelize across containers via a small thread
    pool to keep the endpoint snappy.
    """
    try:
        import docker  # imported lazily — host-dev without docker still works
        from concurrent.futures import ThreadPoolExecutor
    except ImportError:
        return []
    try:
        client = docker.from_env()
    except Exception:
        return []
    try:
        all_containers = client.containers.list()
    except Exception:
        return []
    containers = [c for c in all_containers if any(c.name.startswith(p) for p in _BILBO_CONTAINER_PREFIXES)]
    if not containers:
        return []
    mem = _memory()
    total_mem = mem.get("totalBytes", 0)
    with ThreadPoolExecutor(max_workers=min(len(containers), 6)) as pool:
        rows = list(pool.map(lambda c: _container_stat(client, c, total_mem), containers))
    return [r for r in rows if r]


def gather() -> dict:
    containers = _bilbo_containers()
    top_cpu = sorted(containers, key=lambda r: r["cpuPct"], reverse=True)[:8]
    top_mem = sorted(containers, key=lambda r: r.get("rssKb") or 0, reverse=True)[:8]
    return {
        "asOf": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "load": _load(),
        "memory": _memory(),
        "disk": _disk(),
        "babyMonitor": {
            "sizes": _baby_monitor_sizes(),
            "processes": containers,
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
