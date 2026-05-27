# AirGradient Local Logger

Polls an AirGradient air quality monitor on the local network and stores each
reading in a local SQLite database. One process, one row per poll.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+ is required (uses `dict[str, Any]` / `tuple[..., ...]` syntax).

## Test the device first

Before running the logger, confirm the AirGradient is reachable and returning
JSON:

```bash
curl -s http://airgradient.local/measures/current | python -m json.tool
```

If that prints a JSON object with fields like `rco2`, `atmp`, `rhum`, `pm02`,
the logger will work.

> **Note on field names.** Current AirGradient firmware (tested against
> I-9PSL, fw 3.6.2) returns camelCase keys: `pm003Count`, `tvocRaw`,
> `pm02Compensated`, `atmpCompensated`, `rhumCompensated`. The logger reads
> camelCase and falls back to snake_case (`pm003_count`, `tvoc_raw`) so older
> firmware also works. The full payload is always saved to `raw_json`, so any
> field the typed columns miss can still be queried with `json_extract()`.

## Run

```bash
python airgradient_logger.py
```

Configuration via environment variables:

| Variable          | Default                                | Notes                       |
| ----------------- | -------------------------------------- | --------------------------- |
| `AIRGRADIENT_URL` | `http://airgradient.local/measures/current` | Full URL including path |
| `DB_PATH`         | `./airgradient.db`                     | SQLite file path            |
| `POLL_SECONDS`    | `60`                                   | Seconds between polls       |
| `LOG_LEVEL`       | `INFO`                                 | Set `DEBUG` for verbose     |

Example with overrides:

```bash
AIRGRADIENT_URL=http://airgradient.local/measures/current \
DB_PATH=/var/lib/airgradient/airgradient.db \
POLL_SECONDS=30 \
python airgradient_logger.py
```

## Static IP vs mDNS hostname

Two reliable ways to address the device on your LAN:

1. **DHCP reservation (preferred for headless setups).** Reserve the
   AirGradient's MAC to a fixed IP in your router. Robust to mDNS quirks
   on Windows / VLAN-segmented networks.
2. **mDNS hostname.** Most AirGradient firmware advertises something like
   `airgradient_<id>.local`. With Bonjour (macOS) or `avahi-daemon` (Linux), and
   a network that doesn't block multicast, you can use
   `http://airgradient.local/measures/current`. Verify with
   `ping airgradient.local`. Some Wi-Fi APs / guest VLANs block mDNS — fall
   back to a DHCP reservation in that case.

## Querying the data

```bash
sqlite3 airgradient.db
```

**Last hour of readings:**
```sql
SELECT recorded_at, co2_ppm, pm25, temperature_c, humidity_pct
FROM readings
WHERE recorded_at >= datetime('now', '-1 hour')
ORDER BY recorded_at DESC;
```

**Daily averages (last 14 days):**
```sql
SELECT
  substr(recorded_at, 1, 10)       AS day,
  ROUND(AVG(co2_ppm), 0)           AS co2_avg,
  ROUND(AVG(pm25), 1)              AS pm25_avg,
  ROUND(AVG(temperature_c), 1)     AS temp_avg_c,
  ROUND(AVG(humidity_pct), 1)      AS rh_avg
FROM readings
GROUP BY day
ORDER BY day DESC
LIMIT 14;
```

**CO2 spikes above 1500 ppm:**
```sql
SELECT recorded_at, co2_ppm
FROM readings
WHERE co2_ppm > 1500
ORDER BY recorded_at DESC;
```

**Hourly rollup (last 24 h):**
```sql
SELECT
  substr(recorded_at, 1, 13) || ':00' AS hour,
  ROUND(AVG(co2_ppm), 0)              AS co2_avg,
  ROUND(AVG(pm25), 1)                 AS pm25_avg
FROM readings
WHERE recorded_at >= datetime('now', '-1 day')
GROUP BY hour
ORDER BY hour;
```

**Inspect the raw JSON for a row** (handy when AirGradient firmware adds new
fields you want to backfill):
```sql
SELECT raw_json FROM readings ORDER BY id DESC LIMIT 1;
```

## Running as a service

### systemd (Linux)

`airgradient-logger.service` is included as a starting point. Adjust paths,
user, and env vars, then install:

```bash
sudo cp airgradient-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now airgradient-logger
sudo journalctl -u airgradient-logger -f
```

### Docker

```bash
docker compose up -d
docker compose logs -f
```

The compose file mounts `./data/` so the SQLite file survives container
rebuilds. Edit env vars in `docker-compose.yml` for your device.

## Behavior notes

- On startup, the DB and `readings` table (and `idx_readings_recorded_at`
  index) are created if missing.
- One row per successful poll. The full JSON response is stored in `raw_json`
  for forward compatibility with new AirGradient firmware fields (e.g. fields
  not yet broken out into typed columns).
- Network errors, JSON decode errors, and SQLite errors are logged at
  `WARNING` / `ERROR` and the loop continues — a flaky monitor or brief Wi-Fi
  drop won't kill the logger.
- `SIGINT` (Ctrl-C) and `SIGTERM` (systemd / docker stop) interrupt the sleep
  immediately and shut the loop down between polls.
- Polling cadence is "wall-clock minus poll latency": if a poll takes 2 s, the
  next one fires `POLL_SECONDS - 2` later, so cadence stays roughly constant.
