"""Database table initialization and persistence functions for cashier data (PostgreSQL)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..db import _connect, dict_cursor
from .helpers import detect_period, normalize_period, period_label

_tables_initialized = False
_init_lock = threading.Lock()


def init_cashier_tables() -> None:
    """Initialize PostgreSQL tables and indexes for cashier analytics (runs only once per process)."""
    global _tables_initialized
    if _tables_initialized:
        return
    with _init_lock:
        if _tables_initialized:
            return
        with _connect() as c:
            cur = dict_cursor(c)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS cashier_imports (
                    id          SERIAL PRIMARY KEY,
                    filename    TEXT,
                    imported_at TEXT NOT NULL,
                    rows_count  INTEGER NOT NULL,
                    report_label TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS cashier_reports (
                    id                   SERIAL PRIMARY KEY,
                    import_id            INTEGER NOT NULL REFERENCES cashier_imports(id) ON DELETE CASCADE,
                    tab_number           TEXT,
                    employee_number      TEXT,
                    full_name            TEXT NOT NULL,
                    position             TEXT,
                    days_worked          REAL,
                    std_days             REAL NOT NULL DEFAULT 0,
                    operations_count     REAL NOT NULL DEFAULT 0,
                    operations_minutes   REAL NOT NULL DEFAULT 0,
                    bek_count            REAL NOT NULL DEFAULT 0,
                    bek_minutes          REAL NOT NULL DEFAULT 0,
                    front_count          REAL NOT NULL DEFAULT 0,
                    front_minutes        REAL NOT NULL DEFAULT 0,
                    load_percent         REAL NOT NULL DEFAULT 0,
                    load_difference      REAL NOT NULL DEFAULT 0,
                    branch_name          TEXT,
                    raw_note             TEXT,
                    hr_status_code       TEXT,
                    hr_status_label      TEXT,
                    replacing_full_name  TEXT,
                    replaced_by_full_name TEXT,
                    has_replacement      INTEGER DEFAULT 1,
                    metrics_json         TEXT NOT NULL DEFAULT '{}',
                    discipline_type      TEXT,
                    discipline_date_order TEXT,
                    discipline_reason    TEXT
                )
            """)

            # Migrations for existing databases
            cur.execute("ALTER TABLE cashier_reports ADD COLUMN IF NOT EXISTS discipline_type TEXT")
            cur.execute("ALTER TABLE cashier_reports ADD COLUMN IF NOT EXISTS discipline_date_order TEXT")
            cur.execute("ALTER TABLE cashier_reports ADD COLUMN IF NOT EXISTS discipline_reason TEXT")
            cur.execute("ALTER TABLE cashier_reports ADD COLUMN IF NOT EXISTS employee_number TEXT")
            cur.execute("ALTER TABLE cashier_reports ADD COLUMN IF NOT EXISTS std_days REAL NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE cashier_reports ADD COLUMN IF NOT EXISTS load_percent REAL NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE cashier_reports ADD COLUMN IF NOT EXISTS load_difference REAL NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE cashier_imports ADD COLUMN IF NOT EXISTS period TEXT")
            _backfill_import_periods(cur)
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_cashier_imports_period ON cashier_imports(period)"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS cashier_status_imports (
                    id          SERIAL PRIMARY KEY,
                    filename    TEXT,
                    imported_at TEXT NOT NULL,
                    rows_count  INTEGER NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS cashier_statuses (
                    id                    SERIAL PRIMARY KEY,
                    import_id             INTEGER NOT NULL REFERENCES cashier_status_imports(id) ON DELETE CASCADE,
                    branch_name           TEXT,
                    position              TEXT,
                    full_name             TEXT NOT NULL,
                    status_code           TEXT NOT NULL,
                    status_label          TEXT NOT NULL,
                    raw_note              TEXT,
                    replacing_full_name   TEXT,
                    replaced_by_full_name TEXT,
                    has_replacement       INTEGER NOT NULL DEFAULT 0
                )
            """)

            # Performance indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cashier_reports_import   ON cashier_reports(import_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cashier_reports_status   ON cashier_reports(hr_status_code)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cashier_reports_position ON cashier_reports(position)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cashier_statuses_import  ON cashier_statuses(import_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cashier_statuses_status  ON cashier_statuses(status_code)")

            cur.close()

        _tables_initialized = True


def _previous_month(iso_ts: str) -> Optional[str]:
    try:
        d = datetime.fromisoformat(str(iso_ts).replace('Z', '+00:00'))
    except ValueError:
        return None
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return f"{y}-{m:02d}"


def _backfill_import_periods(cur) -> None:
    """Give legacy imports (saved before periods existed) a reporting month.

    Uses the month named in the filename/label, otherwise the month before the
    import date (monthly KPI reports are uploaded after the month closes).
    """
    cur.execute('SELECT id, filename, report_label, imported_at FROM cashier_imports WHERE period IS NULL')
    legacy = [dict(r) for r in cur.fetchall()]
    cur.execute('SELECT period FROM cashier_imports WHERE period IS NOT NULL')
    taken = {r['period'] for r in cur.fetchall()}
    for row in sorted(legacy, key=lambda r: r['imported_at'] or '', reverse=True):
        period = (
            detect_period(f"{row.get('filename') or ''} {row.get('report_label') or ''}")
            or _previous_month(row.get('imported_at') or '')
        )
        if period and period not in taken:
            cur.execute('UPDATE cashier_imports SET period=%s WHERE id=%s', (period, row['id']))
            taken.add(period)


def _import_public(row: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(row)
    d['period_label'] = period_label(d.get('period'))
    return d


def list_cashier_imports() -> List[Dict[str, Any]]:
    """All KPI imports, newest reporting month first."""
    init_cashier_tables()
    with _connect() as c:
        cur = dict_cursor(c)
        cur.execute(
            'SELECT id, filename, imported_at, rows_count, report_label, period FROM cashier_imports '
            'ORDER BY period IS NULL, period DESC, imported_at DESC, id DESC'
        )
        rows = [_import_public(r) for r in cur.fetchall()]
        cur.close()
    return rows


def get_cashier_import(import_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """One KPI import by id, or the latest reporting month when id is None."""
    imports = list_cashier_imports()
    if import_id is None:
        return imports[0] if imports else None
    return next((i for i in imports if i['id'] == import_id), None)


def delete_cashier_import(import_id: int) -> bool:
    """Delete one month's KPI import together with its cashier rows."""
    init_cashier_tables()
    with _connect() as c:
        cur = dict_cursor(c)
        cur.execute('DELETE FROM cashier_reports WHERE import_id=%s', (import_id,))
        cur.execute('DELETE FROM cashier_imports WHERE id=%s', (import_id,))
        deleted = cur.rowcount > 0
        cur.close()
    return deleted


def save_cashier_import(filename: str, parsed: Dict[str, Any], period: str) -> Dict[str, Any]:
    """Save a KPI report for one reporting month, replacing that month if it was loaded before.

    Other months are kept, so July, August, September… live side by side.
    """
    period = normalize_period(period)
    if not period:
        raise ValueError('Укажите отчётный месяц в формате ГГГГ-ММ.')
    init_cashier_tables()
    with _connect() as c:
        cur = dict_cursor(c)
        cur.execute('SELECT id FROM cashier_imports WHERE period=%s', (period,))
        previous = cur.fetchone()
        if previous:
            cur.execute('DELETE FROM cashier_reports WHERE import_id=%s', (previous['id'],))
            cur.execute('DELETE FROM cashier_imports WHERE id=%s', (previous['id'],))

        cur.execute(
            'INSERT INTO cashier_imports(filename, imported_at, rows_count, period) '
            'VALUES(%s, %s, %s, %s) RETURNING id',
            (filename, datetime.now(timezone.utc).isoformat(), len(parsed['records']), period)
        )
        iid = cur.fetchone()['id']

        for r in parsed['records']:
            cur.execute('''
                INSERT INTO cashier_reports(
                    import_id, tab_number, employee_number, full_name, position,
                    days_worked, std_days,
                    operations_count, operations_minutes, bek_count, bek_minutes,
                    front_count, front_minutes, load_percent, load_difference,
                    branch_name, raw_note,
                    hr_status_code, hr_status_label, replacing_full_name, replaced_by_full_name,
                    has_replacement, metrics_json,
                    discipline_type, discipline_date_order, discipline_reason
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ''', (
                iid,
                r.get('tab_number'),
                r.get('employee_number') or '',
                r['full_name'],
                r.get('position'),
                r.get('days_worked', 0),
                r.get('std_days', 0) or 0,
                r.get('operations_count', 0),
                r.get('operations_minutes', 0),
                r.get('bek_count', 0),
                r.get('bek_minutes', 0),
                r.get('front_count', 0),
                r.get('front_minutes', 0),
                r.get('load_percent', 0) or 0,
                r.get('load_difference', 0) or 0,
                r.get('branch_name', ''),
                r.get('raw_note', ''),
                r.get('hr_status_code', 'active'),
                r.get('hr_status_label', '🟢 Работает'),
                r.get('replacing_full_name'),
                r.get('replaced_by_full_name'),
                r.get('has_replacement', 1),
                json.dumps(r.get('metrics', {}), ensure_ascii=False),
                r.get('discipline_type') or None,
                r.get('discipline_date_order') or None,
                r.get('discipline_reason') or None,
            ))

        cur.execute(
            'UPDATE cashier_imports SET report_label=%s WHERE id=%s',
            (parsed.get('report_label', filename), iid)
        )

        # The same file uploaded under another month gives identical totals.
        cur.execute('''
            SELECT i.period, COUNT(r.id) AS n,
                   ROUND(SUM(r.operations_count)) AS ops, ROUND(SUM(r.operations_minutes)) AS mins
            FROM cashier_imports i JOIN cashier_reports r ON r.import_id = i.id
            GROUP BY i.id, i.period
        ''')
        stats = {r['period']: (r['n'], r['ops'], r['mins']) for r in cur.fetchall()}
        mine = stats.pop(period, None)
        same_data_as = sorted(p for p, s in stats.items() if s == mine and mine and mine[1])

        cur.close()

    return {
        'import_id': iid,
        'period': period,
        'period_label': period_label(period),
        'same_data_as': [period_label(p) for p in same_data_as],
        'replaced_previous': bool(previous),
        'rows_saved': len(parsed['records']),
        'header_rows': parsed.get('header_rows', []),
        'columns': parsed.get('columns', []),
    }


def save_cashier_status_import(filename: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Replace previous HR status import with this file (single active registry)."""
    init_cashier_tables()
    with _connect() as c:
        cur = dict_cursor(c)
        cur.execute('TRUNCATE TABLE cashier_statuses, cashier_status_imports RESTART IDENTITY CASCADE')

        cur.execute(
            'INSERT INTO cashier_status_imports(filename, imported_at, rows_count) VALUES(%s, %s, %s) RETURNING id',
            (filename, datetime.now(timezone.utc).isoformat(), len(parsed['records']))
        )
        iid = cur.fetchone()['id']

        for r in parsed['records']:
            cur.execute('''
                INSERT INTO cashier_statuses(
                    import_id, branch_name, position, full_name, status_code, status_label,
                    raw_note, replacing_full_name, replaced_by_full_name, has_replacement
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ''', (
                iid,
                r.get('branch_name', ''),
                r.get('position', ''),
                r['full_name'],
                r.get('status_code', 'active'),
                r.get('status_label', '🟢 Ишлаяпти'),
                r.get('raw_note', ''),
                r.get('replacing_full_name'),
                r.get('replaced_by_full_name'),
                r.get('has_replacement', 0),
            ))

        cur.close()

    return {
        'status_import_id': iid,
        'rows_saved': len(parsed['records']),
    }
