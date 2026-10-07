"""One-off transfer of the old SQLite files (data/bank.db, data/atm_monitor.db) into PostgreSQL.

    python3 migrations/sqlite_to_postgres.py --replace [--reset-admin-password]

Creates the schema (core.db.init_db), copies every table that exists on both sides
keeping the original ids, converts values to the PostgreSQL column types, moves the
identity sequences past the copied ids and prints a row-count comparison.
--replace empties the target tables first (init_db seeds roles, admin and SQB rates).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psycopg.types.json import Jsonb  # noqa: E402

from api.core import auth, db  # noqa: E402

# Parents before children (foreign keys).
BANK_TABLES = [
    "atms", "branches", "incassation_trips",
    "cashier_imports", "cashier_reports",
    "branch_balances", "branch_balance_history", "cash_equipment", "sqb_rates",
    "roles", "users",
]
ATM_MONITOR_TABLES = ["atms", "poll_runs", "atm_snapshots", "cassette_snapshots", "turnover_snapshots"]


def _timestamp(v):
    s = str(v).strip()
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    # SQLite datetime('now') wrote UTC without an offset.
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _convert(value, data_type: str):
    if value is None:
        return None
    if data_type == "timestamp with time zone":
        return _timestamp(value)
    if data_type == "date":
        s = str(value).strip()
        return s[:10] or None
    if data_type == "jsonb":
        s = str(value).strip()
        return Jsonb(json.loads(s)) if s else None
    if data_type in ("smallint", "integer", "bigint"):
        s = str(value).strip()
        return round(float(s)) if s else None
    if data_type == "double precision":
        s = str(value).strip()
        return float(s) if s else None
    return str(value)


def _pg_columns(conn, schema: str, table: str) -> dict:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchall()
    return {r["column_name"]: r["data_type"] for r in rows}


def _copy_table(src: sqlite3.Connection, conn, schema: str, table: str) -> tuple[int, int]:
    exists = src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return 0, 0
    pg_cols = _pg_columns(conn, schema, table)
    src_cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
    cols = [c for c in src_cols if c in pg_cols]
    rows = src.execute(f'SELECT {", ".join(cols)} FROM "{table}" ORDER BY rowid').fetchall()

    records, skipped = [], 0
    for row in rows:
        rec = [_convert(v, pg_cols[c]) for c, v in zip(cols, row)]
        if table == "cashier_imports" and rec[cols.index("period")] is None:
            skipped += 1
            continue
        records.append(rec)

    if records:
        placeholders = ", ".join(["%s"] * len(cols))
        with conn.cursor() as cur:
            cur.executemany(
                f'INSERT INTO {schema}."{table}" ({", ".join(cols)}) VALUES ({placeholders})',
                records,
            )
    seq = conn.execute(
        "SELECT pg_get_serial_sequence(%s, 'id') AS seq", (f'{schema}."{table}"',)
    ).fetchone()["seq"] if "id" in pg_cols else None
    if seq:
        conn.execute(
            f'SELECT setval(%s, COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {schema}."{table}"', (seq,)
        )
    return len(rows), skipped


def _count(conn, schema: str, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) AS n FROM {schema}."{table}"').fetchone()["n"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", default=str(ROOT / "data" / "bank.db"))
    ap.add_argument("--atm-monitor-sqlite", default=str(ROOT / "data" / "atm_monitor.db"))
    ap.add_argument("--replace", action="store_true", help="empty the target tables before copying")
    ap.add_argument("--reset-admin-password", action="store_true",
                    help="give user 'admin' a new password and write it to data/admin_credentials.txt")
    args = ap.parse_args()

    db.init_db()
    plan = [(args.sqlite, "public", BANK_TABLES), (args.atm_monitor_sqlite, "atm_monitor", ATM_MONITOR_TABLES)]
    plan = [(Path(f), schema, tables) for f, schema, tables in plan if Path(f).exists()]

    report = []
    with db.connect() as conn:
        targets = [f'{schema}."{t}"' for _, schema, tables in plan for t in tables]
        busy = [t for t in targets if conn.execute(f"SELECT EXISTS (SELECT 1 FROM {t}) AS b").fetchone()["b"]]
        if busy and not args.replace:
            print("Target tables are not empty: " + ", ".join(busy) + "\nRun again with --replace.")
            return 1
        if targets:
            conn.execute(f"TRUNCATE {', '.join(targets)} RESTART IDENTITY CASCADE")

        for path, schema, tables in plan:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            for table in tables:
                read, skipped = _copy_table(src, conn, schema, table)
                report.append((f"{schema}.{table}", read, skipped, _count(conn, schema, table)))
            src.close()

    print(f"{'table':<36}{'sqlite':>8}{'skipped':>9}{'postgres':>10}")
    ok = True
    for name, read, skipped, written in report:
        mark = "" if read - skipped == written else "  ← MISMATCH"
        ok = ok and not mark
        print(f"{name:<36}{read:>8}{skipped:>9}{written:>10}{mark}")

    if args.reset_admin_password:
        admin = next((u for u in auth.list_users() if u["username"] == "admin"), None)
        if admin:
            password = auth.generate_password()
            auth.update_user(admin["id"], new_password=password)
            auth.write_admin_credentials(password)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
