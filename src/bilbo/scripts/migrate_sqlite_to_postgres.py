#!/usr/bin/env python3
"""One-time migration: load a SQLite monitor.db into Postgres.

Bilbo's source of truth moved from SQLite (which corrupted under concurrent
multi-container access over a macOS bind mount) to Postgres. This script reads
a SQLite database file — normally the `.recover`ed copy of the last good
monitor.db — and copies every table into the Postgres database named by
`DATABASE_URL`.

Strategy
--------
* Column-preserving copy: each SQLite row is inserted into the matching
  Postgres table using the intersection of the two schemas' columns. This
  keeps fields the high-level `insert_*` helpers would drop (reviewed,
  reviewed_at, created_at, eye_state_corrected_at, ...).
* The `id` column is NOT copied — Postgres regenerates it via IDENTITY. Nothing
  references entries/corrections/training_runs by integer id across tables
  (corrections ↔ entries join on the `*timestamp` text columns), and the
  dashboard always reads fresh ids, so regeneration is safe.
* JSONL backfill: after the table copy, any entry present in the append-only
  `sleep-log.jsonl` backup but missing from the loaded set (by timestamp) is
  inserted via the normal `insert_entry` path. This recovers the most-recent
  rows that lived only in the SQLite WAL / were lost to the corrupt pages.

Usage
-----
    python -m bilbo.scripts.migrate_sqlite_to_postgres \
        --sqlite data/monitor.db.recovered [--jsonl data/sleep-log.jsonl] \
        [--force] [--no-jsonl-backfill] [--dry-run]

`--force` truncates the Postgres tables first (required if they're non-empty).
"""

import argparse
import json
import sqlite3
import sys

from bilbo.config import JSONL_FILE
from bilbo.storage.db import get_connection, get_db, init_db, insert_entry

TABLES = ("entries", "corrections", "training_runs", "state")


def _pg_columns(pg, table: str) -> list[str]:
    rows = pg.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [r["column_name"] for r in rows]


def _copy_table(sq: sqlite3.Connection, pg, table: str, dry_run: bool) -> int:
    pg_cols = set(_pg_columns(pg, table))
    sq_rows = sq.execute(f"SELECT * FROM {table}").fetchall()
    if not sq_rows:
        print(f"  {table}: 0 rows in source")
        return 0

    # Intersect schemas, drop the auto-generated id.
    src_cols = [c for c in sq_rows[0].keys() if c in pg_cols and c != "id"]
    placeholders = ", ".join(["%s"] * len(src_cols))
    collist = ", ".join(src_cols)
    sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders})"
    payload = [tuple(row[c] for c in src_cols) for row in sq_rows]

    print(f"  {table}: {len(payload)} rows  (cols: {collist})")
    if dry_run:
        return len(payload)
    with pg.transaction(), pg.cursor() as cur:
        cur.executemany(sql, payload)
    return len(payload)


def _backfill_from_jsonl(pg, jsonl_path, dry_run: bool) -> int:
    if not jsonl_path.exists():
        print(f"  jsonl backfill: {jsonl_path} not found — skipping")
        return 0

    existing = {
        r["timestamp"]
        for r in pg.execute("SELECT timestamp FROM entries").fetchall()
    }
    added = 0
    for line in jsonl_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = entry.get("timestamp")
        if not ts or ts in existing:
            continue
        if not dry_run:
            insert_entry(entry)
        existing.add(ts)
        added += 1
    print(f"  jsonl backfill: {added} entries present in JSONL but missing from load")
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", required=True,
                    help="Path to the source SQLite DB (e.g. data/monitor.db.recovered)")
    ap.add_argument("--jsonl", default=str(JSONL_FILE),
                    help="Path to sleep-log.jsonl for the recent-entry backfill")
    ap.add_argument("--no-jsonl-backfill", action="store_true",
                    help="Skip the JSONL backfill step")
    ap.add_argument("--force", action="store_true",
                    help="TRUNCATE the Postgres tables before loading (required if non-empty)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be copied without writing")
    args = ap.parse_args()

    from pathlib import Path
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"error: SQLite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    get_db()       # construct singleton
    init_db()      # ensure Postgres schema exists
    pg = get_connection()

    # Refuse to clobber a populated Postgres DB unless --force.
    existing_n = pg.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"]
    if existing_n and not args.force:
        print(f"error: Postgres entries table already has {existing_n} rows. "
              f"Re-run with --force to TRUNCATE and reload.", file=sys.stderr)
        return 1
    if existing_n and args.force and not args.dry_run:
        print(f"--force: truncating {existing_n} existing rows across {TABLES}")
        with pg.transaction(), pg.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY")

    sq = sqlite3.connect(str(sqlite_path))
    sq.row_factory = sqlite3.Row

    print(f"copying from {sqlite_path} -> Postgres:")
    totals = {}
    for table in TABLES:
        totals[table] = _copy_table(sq, pg, table, args.dry_run)

    if not args.no_jsonl_backfill:
        totals["jsonl_backfill"] = _backfill_from_jsonl(pg, Path(args.jsonl), args.dry_run)

    # Verify
    print("\nfinal Postgres counts:")
    for table in TABLES:
        n = pg.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        print(f"  {table}: {n}")

    print("\ndry-run: no writes" if args.dry_run else "\nmigration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
