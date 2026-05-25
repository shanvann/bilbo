#!/usr/bin/env python3
"""AirGradient local data logger.

Polls a local AirGradient air quality monitor on a fixed cadence and writes
one row per reading to SQLite. Configuration via environment variables; see
README.md.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

import requests


DEFAULT_URL = "http://192.168.1.50/measures/current"
DEFAULT_DB_PATH = "./airgradient.db"
DEFAULT_POLL_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10

LOG = logging.getLogger("airgradient_logger")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        LOG.warning("invalid int for %s=%r; falling back to %d", name, raw, default)
        return default


def load_config() -> dict[str, Any]:
    return {
        "url": os.environ.get("AIRGRADIENT_URL", DEFAULT_URL),
        "db_path": os.environ.get("DB_PATH", DEFAULT_DB_PATH),
        "poll_seconds": _env_int("POLL_SECONDS", DEFAULT_POLL_SECONDS),
    }


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _get_float(d: Mapping[str, Any], key: str) -> Optional[float]:
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_int(d: Mapping[str, Any], key: str) -> Optional[int]:
    v = d.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _get_str(d: Mapping[str, Any], key: str) -> Optional[str]:
    v = d.get(key)
    if v is None:
        return None
    return str(v)


def _first_float(d: Mapping[str, Any], keys: Iterable[str]) -> Optional[float]:
    for k in keys:
        v = _get_float(d, k)
        if v is not None:
            return v
    return None


def _first_int(d: Mapping[str, Any], keys: Iterable[str]) -> Optional[int]:
    for k in keys:
        v = _get_int(d, k)
        if v is not None:
            return v
    return None


def map_reading(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map a raw AirGradient JSON payload to our column dict.

    The /measures/current endpoint on AirGradient ONE / Open Air firmware
    returns camelCase keys (e.g. pm003Count, tvocRaw, pm02Compensated). We
    accept both camelCase and snake_case so this works against older firmware
    or third-party endpoints that use snake_case.
    """
    return {
        "serialno": _get_str(payload, "serialno"),
        "wifi": _get_int(payload, "wifi"),
        "co2_ppm": _get_int(payload, "rco2"),
        "pm01": _get_float(payload, "pm01"),
        # Prefer compensated PM2.5 when the firmware exposes it.
        "pm25": _first_float(payload, ("pm02Compensated", "pm02")),
        "pm10": _get_float(payload, "pm10"),
        "pm003_count": _first_int(payload, ("pm003Count", "pm003_count")),
        "temperature_c": _first_float(payload, ("atmpCompensated", "atmp")),
        "humidity_pct": _first_float(payload, ("rhumCompensated", "rhum")),
        "tvoc_raw": _first_int(payload, ("tvocRaw", "tvoc_raw")),
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     TEXT NOT NULL,
    serialno        TEXT,
    wifi            INTEGER,
    co2_ppm         INTEGER,
    pm01            REAL,
    pm25            REAL,
    pm10            REAL,
    pm003_count     INTEGER,
    temperature_c   REAL,
    humidity_pct    REAL,
    tvoc_raw        INTEGER,
    raw_json        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_recorded_at ON readings(recorded_at);
"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_reading(conn: sqlite3.Connection, recorded_at: str,
                   mapped: Mapping[str, Any], raw_json: str) -> None:
    conn.execute(
        """
        INSERT INTO readings (
            recorded_at, serialno, wifi, co2_ppm, pm01, pm25, pm10,
            pm003_count, temperature_c, humidity_pct, tvoc_raw, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recorded_at,
            mapped.get("serialno"),
            mapped.get("wifi"),
            mapped.get("co2_ppm"),
            mapped.get("pm01"),
            mapped.get("pm25"),
            mapped.get("pm10"),
            mapped.get("pm003_count"),
            mapped.get("temperature_c"),
            mapped.get("humidity_pct"),
            mapped.get("tvoc_raw"),
            raw_json,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def fetch_reading(url: str) -> tuple[Mapping[str, Any], str]:
    """Returns (parsed payload, raw text). Raises on HTTP/JSON errors."""
    resp = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json(), resp.text


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_loop(url: str, db_path: str, poll_seconds: int,
             stop: threading.Event) -> None:
    conn = open_db(db_path)
    LOG.info("polling %s every %ds -> %s", url, poll_seconds, db_path)

    while not stop.is_set():
        tick_start = time.monotonic()
        try:
            payload, raw_text = fetch_reading(url)
            mapped = map_reading(payload)
            insert_reading(conn, utc_now_iso(), mapped, raw_text)
            LOG.info(
                "stored co2=%s pm25=%s temp=%s rh=%s",
                mapped.get("co2_ppm"),
                mapped.get("pm25"),
                mapped.get("temperature_c"),
                mapped.get("humidity_pct"),
            )
        except requests.RequestException as e:
            LOG.warning("poll failed: %s", e)
        except json.JSONDecodeError as e:
            LOG.warning("bad JSON from %s: %s", url, e)
        except sqlite3.Error as e:
            LOG.error("sqlite error: %s", e)
        except Exception as e:  # noqa: BLE001
            LOG.exception("unexpected error: %s", e)

        elapsed = time.monotonic() - tick_start
        stop.wait(max(0.0, poll_seconds - elapsed))

    conn.close()
    LOG.info("stopped cleanly")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config()
    stop = threading.Event()

    def _handle(signum, _frame):
        LOG.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    run_loop(cfg["url"], cfg["db_path"], cfg["poll_seconds"], stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
