"""
Единый слой доступа к PostgreSQL для Bank Intelligence Platform.

Подключение берётся из DATABASE_URL (см. core/config.py), соединения
выдаются из пула psycopg_pool. Строки возвращаются словарями (dict_row).

Схема создаётся одной транзакцией в init_db(): ядро (atms, branches,
incassation_trips) + таблицы доменов (кассиры, касса филиалов, техника,
курсы SQB, авторизация) + схема atm_monitor сборщика BTech.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import psycopg
from psycopg import sql
from psycopg.adapt import Loader
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.datetime import TimestamptzLoader
from psycopg_pool import ConnectionPool, PoolTimeout

from . import atm_live
from . import config
from .config import DEFAULT_CAPACITY, LOW_CASH_PCT, WARNING_CASH_PCT

log = logging.getLogger(__name__)


class DatabaseUnavailableError(RuntimeError):
    """PostgreSQL недоступен (сервер не запущен, неверный DATABASE_URL, нет прав)."""


# ═══════════════════════════════════════════════════════════
# ТИПЫ: даты и время отдаём приложению ISO-строками
# ═══════════════════════════════════════════════════════════
# В базе — настоящие DATE / TIMESTAMPTZ (сортировка, сравнения, now()), а
# в Python и JSON-ответах API — ISO-строки, как и раньше: фронтенд и бизнес-
# логика сравнивают даты строками ('2026-08-21') и делают new Date(iso).

class _IsoTimestamptzLoader(TimestamptzLoader):
    def load(self, data):
        return super().load(data).isoformat()


class _IsoDateLoader(Loader):
    def load(self, data):
        return bytes(data).decode()


psycopg.adapters.register_loader("timestamptz", _IsoTimestamptzLoader)
psycopg.adapters.register_loader("date", _IsoDateLoader)

_SESSION_OPTIONS = "-c timezone=UTC -c datestyle=ISO,YMD"


# ═══════════════════════════════════════════════════════════
# ПОДКЛЮЧЕНИЕ
# ═══════════════════════════════════════════════════════════

_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def _unavailable(reason: object) -> DatabaseUnavailableError:
    return DatabaseUnavailableError(
        f"\n❌ PostgreSQL недоступен: {config.DATABASE_URL}\n"
        f"   Причина: {reason}\n"
        "   Запустите сервер (macOS: brew services start postgresql@15) и проверьте DATABASE_URL в .env.\n"
    )


def _create_database_if_missing(error: psycopg.Error) -> bool:
    """Новый компьютер: сервер есть, а базы ещё нет — создаём её сами."""
    name = conninfo_to_dict(config.DATABASE_URL).get("dbname")
    if not name or f'database "{name}" does not exist' not in str(error):
        return False
    with psycopg.connect(make_conninfo(config.DATABASE_URL, dbname="postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(name)))
    log.info("Создана база данных %s", name)
    return True


def _probe_connection() -> None:
    """Пробное подключение даёт понятную причину сразу, а не PoolTimeout через 30 с."""
    try:
        psycopg.connect(config.DATABASE_URL, connect_timeout=5).close()
    except psycopg.OperationalError as e:
        try:
            created = _create_database_if_missing(e)
        except psycopg.Error as cause:
            raise _unavailable(cause) from None
        if not created:
            raise _unavailable(e) from None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _probe_connection()
                _pool = ConnectionPool(
                    config.DATABASE_URL,
                    min_size=config.DB_POOL_MIN,
                    max_size=config.DB_POOL_MAX,
                    timeout=config.DB_POOL_TIMEOUT,
                    kwargs={"row_factory": dict_row, "options": _SESSION_OPTIONS},
                    name="bank",
                    open=True,
                )
    return _pool


def close_pool() -> None:
    """Закрывает пул (остановка сервера, смена DATABASE_URL в тестах)."""
    global _pool, _schema_ready
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _schema_ready = False


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Соединение из пула в рамках одной транзакции: commit при успехе, rollback при ошибке."""
    try:
        with _get_pool().connection() as conn:
            yield conn
    except PoolTimeout as e:
        raise _unavailable(e) from None


# ═══════════════════════════════════════════════════════════
# СХЕМА
# ═══════════════════════════════════════════════════════════

_SCHEMA_LOCK_ID = 7_340_001          # pg_advisory_xact_lock: параллельные воркеры не создают схему дважды
_schema_ready = False
_schema_lock = threading.Lock()


def _create_core_tables(conn: psycopg.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS atms (
            id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            terminal_id     TEXT NOT NULL UNIQUE,
            atm_number      TEXT,
            branch_code     TEXT,
            local_code      TEXT,
            region          TEXT,
            branch          TEXT,
            model           TEXT,
            network_type    TEXT,
            serial          TEXT,
            merchant_id     TEXT,
            address         TEXT,
            lat             DOUBLE PRECISION,
            lon             DOUBLE PRECISION,
            capacity        BIGINT NOT NULL DEFAULT 400000000,
            balance         BIGINT,
            last_balance_at TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_atms_region ON atms(region)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_atms_branch ON atms(branch)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_atms_local  ON atms(local_code)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            number      TEXT,
            local_code  TEXT UNIQUE,
            region      TEXT,
            address     TEXT,
            lat         DOUBLE PRECISION,
            lon         DOUBLE PRECISION,
            incassation SMALLINT NOT NULL DEFAULT 0 CHECK (incassation IN (0, 1)),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_region ON branches(region)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_inc    ON branches(incassation)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS incassation_trips (
            id                BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            planned_date      DATE NOT NULL,
            region            TEXT,
            label             TEXT,
            priority          TEXT,
            branch_local_code TEXT,
            branch_address    TEXT,
            branch_lat        DOUBLE PRECISION,
            branch_lon        DOUBLE PRECISION,
            stops_json        JSONB NOT NULL DEFAULT '[]',
            geometry_json     JSONB NOT NULL DEFAULT '[]',
            distance_km       DOUBLE PRECISION,
            est_time_min      INTEGER,
            refill_total      BIGINT,
            generated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inc_trips_date ON incassation_trips(planned_date)")


def init_db() -> None:
    """Создаёт все таблицы (идемпотентно) и сеет справочники при первом запуске."""
    global _schema_ready
    from .auth import create_auth_tables, write_admin_credentials
    from .branch_balance import create_branch_balance_tables
    from .cash_equipment import create_cash_equipment_tables
    from .economics import create_settings_tables
    from .cashier.repository import create_cashier_tables
    from .sqb_rates import create_sqb_rate_tables
    from .ledger_vash import create_ledger_vash_tables
    from .vash import create_vash_tables
    from atm_monitor.db import create_schema as create_atm_monitor_schema

    with _schema_lock:
        with connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_ID,))
            _create_core_tables(conn)
            create_cashier_tables(conn)
            create_branch_balance_tables(conn)
            create_cash_equipment_tables(conn)
            create_sqb_rate_tables(conn)
            create_vash_tables(conn)
            create_ledger_vash_tables(conn)
            create_settings_tables(conn)
            seeded_password = create_auth_tables(conn)
            create_atm_monitor_schema(conn)
        # Файл с паролем пишется только после commit — иначе при откате он не совпал бы с базой.
        if seeded_password:
            write_admin_credentials(seeded_password)
        _schema_ready = True
    log.info("База данных инициализирована: %s", _safe_url())


def ensure_schema() -> None:
    """Ленивая инициализация для кода, вызываемого без старта сервера (скрипты, тесты)."""
    if not _schema_ready:
        init_db()


def _safe_url() -> str:
    """DATABASE_URL без пароля — для логов."""
    info = conninfo_to_dict(config.DATABASE_URL)
    host = info.get("host") or "localhost"
    return f"postgresql://{host}:{info.get('port') or 5432}/{info.get('dbname') or ''}"


# ═══════════════════════════════════════════════════════════
# ATM — ЧТЕНИЕ
# ═══════════════════════════════════════════════════════════

def _compute_status(balance: Optional[int], capacity: int) -> str:
    if balance is None:
        return "unknown"
    if capacity <= 0:
        return "ok"
    pct = balance / capacity
    if pct < LOW_CASH_PCT:
        return "critical"
    if pct < WARNING_CASH_PCT:
        return "warning"
    return "ok"


def _apply_live_state(
    d: Dict[str, Any],
    live_by_tid: Dict[str, Dict[str, Any]],
    cassettes_by_tid: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> None:
    """Накладывает живые данные BTech на одну строку реестра (см. atm_live.match_live_to_rows)."""
    atm_live.match_live_to_rows([d], live_by_tid, cassettes_by_tid)


def _decorate_atm(d: Dict[str, Any]) -> Dict[str, Any]:
    cap = d.get("capacity") or DEFAULT_CAPACITY
    bal = d.get("balance")
    avail = d.get("available_balance", bal)
    d["balance_pct"] = round((bal / cap) * 100, 1) if bal is not None and cap else None
    d["available_pct"] = round((avail / cap) * 100, 1) if avail is not None and cap else None
    # Статус — по доступным деньгам: неисправная кассета с купюрами не выдаёт их клиенту.
    d["status"] = _compute_status(avail, cap)
    return d


def count_atms() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM atms").fetchone()["c"])


def list_atms(
    region: Optional[str] = None,
    branch: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM atms WHERE TRUE"
    params: list[Any] = []
    if region:
        sql += " AND region = %s"
        params.append(region)
    if branch:
        sql += " AND branch = %s"
        params.append(branch)
    sql += " ORDER BY id LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    with connect() as conn:
        raw_rows = conn.execute(sql, params).fetchall()

    atm_live.attach_live_to_rows(raw_rows)
    rows = [_decorate_atm(r) for r in raw_rows]

    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows


def get_atm(terminal_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        d = conn.execute("SELECT * FROM atms WHERE terminal_id = %s", (terminal_id,)).fetchone()
    if not d:
        return None
    atm_live.attach_live_to_rows([d])
    return _decorate_atm(d)


def list_regions() -> List[Dict[str, Any]]:
    """Список областей с количеством ATM."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT region, COUNT(*) AS cnt
              FROM atms
             WHERE region IS NOT NULL AND region <> ''
             GROUP BY region
             ORDER BY cnt DESC
        """).fetchall()
    return [{"region": r["region"], "count": int(r["cnt"])} for r in rows]


def list_branches(region: Optional[str] = None) -> List[Dict[str, Any]]:
    """Список филиалов с количеством ATM."""
    sql = """
        SELECT branch, region, COUNT(*) AS cnt
          FROM atms
         WHERE branch IS NOT NULL AND branch <> ''
    """
    params: list[Any] = []
    if region:
        sql += " AND region = %s"
        params.append(region)
    sql += " GROUP BY branch, region ORDER BY cnt DESC"

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"branch": r["branch"], "region": r["region"], "count": int(r["cnt"])} for r in rows]


# ═══════════════════════════════════════════════════════════
# ATM — ЗАПИСЬ
# ═══════════════════════════════════════════════════════════

def truncate_atms() -> int:
    """Удаляет все ATM, возвращает количество удалённых строк."""
    with connect() as conn:
        n = int(conn.execute("SELECT COUNT(*) AS c FROM atms").fetchone()["c"])
        conn.execute("TRUNCATE atms RESTART IDENTITY")
    return n


def bulk_insert_atms(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Массовая вставка/обновление банкоматов по terminal_id."""
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    with connect() as conn:
        for rec in records:
            terminal_id = (rec.get("terminal_id") or "").strip()
            if not terminal_id:
                stats["skipped"] += 1
                continue

            row = conn.execute("""
                INSERT INTO atms (
                    terminal_id, atm_number, branch_code, local_code, region, branch, model,
                    network_type, serial, merchant_id, address, lat, lon, capacity
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (terminal_id) DO UPDATE SET
                    atm_number = EXCLUDED.atm_number, branch_code = EXCLUDED.branch_code,
                    local_code = EXCLUDED.local_code, region = EXCLUDED.region,
                    branch = EXCLUDED.branch, model = EXCLUDED.model,
                    network_type = EXCLUDED.network_type, serial = EXCLUDED.serial,
                    merchant_id = EXCLUDED.merchant_id, address = EXCLUDED.address,
                    lat = EXCLUDED.lat, lon = EXCLUDED.lon, capacity = EXCLUDED.capacity,
                    updated_at = now()
                RETURNING (xmax = 0) AS inserted
            """, (
                terminal_id,
                rec.get("atm_number"), rec.get("branch_code"), rec.get("local_code"),
                rec.get("region"), rec.get("branch"), rec.get("model"),
                rec.get("network_type"), rec.get("serial"), rec.get("merchant_id"),
                rec.get("address"), rec.get("lat"), rec.get("lon"),
                rec.get("capacity") or DEFAULT_CAPACITY,
            )).fetchone()
            stats["inserted" if row["inserted"] else "updated"] += 1
    return stats


def update_balance(terminal_id: str, balance: int) -> bool:
    """Обновляет остаток одного ATM."""
    with connect() as conn:
        cur = conn.execute("""
            UPDATE atms
               SET balance = %s, last_balance_at = now(), updated_at = now()
             WHERE terminal_id = %s
        """, (int(balance), terminal_id))
        return cur.rowcount > 0


def bulk_update_balances(updates: List[Dict[str, Any]]) -> Dict[str, int]:
    """Массовое обновление остатков: [{terminal_id, balance}, ...]"""
    ok, fail = 0, 0
    with connect() as conn:
        for u in updates:
            tid, bal = u.get("terminal_id"), u.get("balance")
            if not tid or bal is None:
                fail += 1
                continue
            cur = conn.execute("""
                UPDATE atms
                   SET balance = %s, last_balance_at = now(), updated_at = now()
                 WHERE terminal_id = %s
            """, (int(bal), tid))
            if cur.rowcount:
                ok += 1
            else:
                fail += 1
    return {"updated": ok, "failed": fail}


# ═══════════════════════════════════════════════════════════
# ФИЛИАЛЫ
# ═══════════════════════════════════════════════════════════

def count_branches() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM branches").fetchone()["c"])


def list_branches_full(
    region: Optional[str] = None,
    incassation: Optional[int] = None,
    limit: int = 5000,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM branches WHERE TRUE"
    params: list[Any] = []
    if region:
        sql += " AND region = %s"
        params.append(region)
    if incassation is not None:
        sql += " AND incassation = %s"
        params.append(int(incassation))
    sql += " ORDER BY id LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def get_branch(local_code: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        return conn.execute("SELECT * FROM branches WHERE local_code = %s", (local_code,)).fetchone()


def truncate_branches() -> int:
    with connect() as conn:
        n = int(conn.execute("SELECT COUNT(*) AS c FROM branches").fetchone()["c"])
        conn.execute("TRUNCATE branches RESTART IDENTITY")
    return n


def bulk_insert_branches(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Массовая вставка/обновление филиалов по local_code."""
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    with connect() as conn:
        for rec in records:
            local_code = (rec.get("local_code") or "").strip() or None
            values = (
                rec.get("number"), rec.get("region"), rec.get("address"),
                rec.get("lat"), rec.get("lon"), 1 if int(rec.get("incassation") or 0) else 0,
            )
            if local_code is None:
                conn.execute("""
                    INSERT INTO branches (number, region, address, lat, lon, incassation)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, values)
                stats["inserted"] += 1
                continue

            row = conn.execute("""
                INSERT INTO branches (number, region, address, lat, lon, incassation, local_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (local_code) DO UPDATE SET
                    number = EXCLUDED.number, region = EXCLUDED.region,
                    address = EXCLUDED.address, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                    incassation = EXCLUDED.incassation, updated_at = now()
                RETURNING (xmax = 0) AS inserted
            """, (*values, local_code)).fetchone()
            stats["inserted" if row["inserted"] else "updated"] += 1
    return stats


# ═══════════════════════════════════════════════════════════
# КАЛЕНДАРЬ ИНКАССАЦИИ
# ═══════════════════════════════════════════════════════════

def replace_incassation_trips(cars: List[Dict[str, Any]]) -> int:
    """Replace the saved calendar with the newly built trips."""
    with connect() as conn:
        conn.execute("TRUNCATE incassation_trips RESTART IDENTITY")
        for car in cars:
            dep = car.get("departure_branch") or {}
            conn.execute(
                """
                INSERT INTO incassation_trips (
                    planned_date, region, label, priority,
                    branch_local_code, branch_address, branch_lat, branch_lon,
                    stops_json, geometry_json, distance_km, est_time_min, refill_total
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    car.get("planned_date"),
                    car.get("region"),
                    car.get("label"),
                    car.get("priority") or "planned",
                    dep.get("local_code"),
                    dep.get("address"),
                    dep.get("lat"),
                    dep.get("lon"),
                    json.dumps(car.get("stops") or [], ensure_ascii=False),
                    json.dumps(car.get("geometry") or [], ensure_ascii=False),
                    car.get("distance_km") or 0,
                    car.get("est_time_min") or 0,
                    round(car.get("refill_total") or 0),
                ),
            )
        return len(cars)


def list_incassation_trips() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM incassation_trips ORDER BY planned_date, region, id"
        ).fetchall()
    for item in rows:
        item["stops"] = item.pop("stops_json") or []
        item["geometry"] = item.pop("geometry_json") or []
    return rows
