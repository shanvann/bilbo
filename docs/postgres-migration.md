# SQLite → Postgres migration

Bilbo's source of truth moved from SQLite to Postgres on 2026-06-12 after the
SQLite database corrupted twice (2026-05-26, 2026-06-12) under concurrent
multi-container access over a macOS bind mount. See
[design-decisions.md](design-decisions.md#storage-postgres-vs-sqlite-vs-jsonl)
for the *why*. This doc is the *how*.

## What changed

* **`bilbo.config.DATABASE_URL`** — new libpq connection string (env-driven).
  In Docker it's injected per container; for host dev it defaults to
  `postgresql://bilbo:bilbo@localhost:5432/bilbo`.
* **`bilbo.storage.db`** — psycopg3 instead of `sqlite3`. One autocommit,
  thread-local connection per thread with `row_factory=dict_row`, so call sites
  keep using `row["col"]`. The `data`/metrics/state JSON columns stay **`text`**
  (not `jsonb`) so `json.dumps`/`json.loads` and `LIKE` substring queries keep
  working unchanged.
* **`deploy/docker-compose.yml`** — new `postgres:16-alpine` service with the
  `pgdata` named volume + healthcheck. Published on host loopback
  (`127.0.0.1:5432`) so the host-networked `capture` reaches it; bridge services
  use the `postgres` DNS name on the renamed default network `bilbo-net`.
* **`bilbo.training_state`** — the on-demand training container now inherits
  `DATABASE_URL` and joins `BILBO_TRAINING_NETWORK` (`bilbo-net`) so it can
  reach `postgres`.
* **`bilbo.api.air_quality` is unaffected** — it reads the *AirGradient logger's*
  separate, single-writer SQLite DB read-only (`AIRGRADIENT_DB_PATH`). That stays
  SQLite.

## Dialect deltas (SQLite → Postgres)

| SQLite | Postgres |
|---|---|
| `?` placeholders | `%s` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT GENERATED ALWAYS AS IDENTITY` |
| `PRAGMA journal_mode/synchronous` | removed (N/A) |
| `executescript()` (multi-statement) | one `execute()` per statement |
| `ALTER TABLE ADD COLUMN` + catch error | `ADD COLUMN IF NOT EXISTS` |
| `strftime('%Y-%m-%dT%H:%M:%SZ','now')` | `to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')` |
| `date(ts, '-4 hours')` (ET bucket) | `to_char((ts)::timestamptz - interval '4 hours', 'YYYY-MM-DD')` |
| `col IS NOT ?` (null-safe ≠) | `col IS DISTINCT FROM %s` |
| `json_extract(data,'$.k')` | `(data::jsonb ->> 'k')` |
| `.fetchone()[0]` (tuple) | alias the column + `["c"]` (dict rows) |
| literal `%` in a parametrized query | escape as `%%` |

Two things kept the port small: timestamps are stored as ISO-8601 **text** and
compared lexicographically (identical in Postgres — every `WHERE timestamp >= ?`
was unchanged beyond `?`→`%s`); and keeping `data` as `text` avoided touching
the dozens of `json.loads(row["data"])` call sites.

`cloudUnavailable` is stored as JSON `true`, so `->>` yields the text `'true'`
(not `1`); the cloud-failure queries match `IN ('1','true')` and `classify()`
accepts `'true'`.

## Recovery + cutover runbook

The migration doubled as corruption recovery. Steps, in order:

1. **Snapshot** the live (corrupt) DB: `data/monitor.db.snapshot.<ts>` (+ `-wal`).
2. **Recover** into a clean file: `sqlite3 <snapshot> .recover | sqlite3 monitor.db.recovered`,
   then confirm `PRAGMA integrity_check` → `ok`. Corrections (the irreplaceable
   training signal) recovered 100%.
3. **Stand up Postgres** (compose `postgres` service, or a throwaway container for
   a dry run).
4. **Load**: `bilbo-migrate-sqlite-to-pg --sqlite data/monitor.db.recovered`.
   It column-preserving-copies every table (dropping `id`, which IDENTITY
   regenerates — nothing joins on it), then backfills from `sleep-log.jsonl` any
   entries missing from the recovered set (the recent rows lost to the corrupt
   pages / WAL). Re-run with `--force` to TRUNCATE + reload.
5. **Verify** the row counts and exercise the dashboard read endpoints.

Cutover (production) pauses baby monitoring for the duration of the final
recover+load, so do it deliberately: stop `capture`, take a final `.recover`
from the now-quiesced live DB, `up -d --build` the new stack (which includes
`postgres`), load, then confirm `bilbo-monitor --status` and the dashboard.
