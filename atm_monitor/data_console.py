"""Модуль данных (dashboard/data-console.html): просмотр всех таблиц, проверка
полноты данных BTech по датам и догрузка недостающего.

Просмотр базы только читает: транзакция READ ONLY, имена таблиц и столбцов берутся из
information_schema. Столбцы с паролями и токенами не отдаются. Доступ проверяет
зависимость, с которой роутер подключён в api/main.py."""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg import sql
from pydantic import BaseModel

from . import encashments, scheduler
from .auth import LOGIN_REQUIRED, token_manager
from .config import settings
from .db import connect

router = APIRouter(prefix="/api/data-console", tags=["data-console"])

SCHEMAS = ("public", "atm_monitor")
HIDDEN_COLUMN_RE = re.compile(r"pass|secret|token|salt|hash|otp|credential", re.I)
MAX_PAGE = 200
MAX_DAYS = 1100
# Загрузка считается достаточной, если есть 90 % опросов дня (сеть и сон машины случаются).
POLLS_OK_SHARE = 0.9

_PULL_LOCK = threading.Lock()
_PULL_LAST: Dict[str, float] = {}
_PULL_MIN_INTERVAL_SEC = 30


def check_request(request: Request) -> None:
    """Same-origin only: чужой Origin/Referer → 403 (защита от CSRF с другого сайта)."""
    host = (request.headers.get("host") or "").split(",")[0].strip().lower()
    if not host:
        return

    def _host_ok(url: str) -> bool:
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return False
        return bool(netloc) and netloc == host

    origin = request.headers.get("origin")
    if origin and not _host_ok(origin):
        raise HTTPException(403, "Запрос с другого сайта запрещён")

    # Для POST (pull/stop) дополнительно сверяем Referer, если Origin нет.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        referer = request.headers.get("referer") or request.headers.get("referrer")
        if referer and not _host_ok(referer):
            raise HTTPException(403, "Запрос с другого сайта запрещён")


# ── Просмотр базы ────────────────────────────────────────────

def _cell(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} байт>"
    return v


def _columns(conn, schema: str, table: str) -> List[Dict[str, Any]]:
    return conn.execute(
        """SELECT column_name AS name, data_type AS type, is_nullable = 'YES' AS nullable
             FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position""",
        (schema, table),
    ).fetchall()


@router.get("/tables", summary="Все таблицы: примерное число строк и размер")
def list_tables():
    with connect() as conn:
        return conn.execute(
            """SELECT t.table_schema AS schema, t.table_name AS name,
                      COALESCE(s.n_live_tup, 0) AS rows_estimate,
                      pg_total_relation_size(format('%%I.%%I', t.table_schema, t.table_name)::regclass) AS bytes
                 FROM information_schema.tables t
                 LEFT JOIN pg_stat_user_tables s ON s.schemaname = t.table_schema AND s.relname = t.table_name
                WHERE t.table_schema = ANY(%s) AND t.table_type = 'BASE TABLE'
                ORDER BY t.table_schema, t.table_name""",
            (list(SCHEMAS),),
        ).fetchall()


@router.get("/tables/{schema}/{table}", summary="Строки таблицы")
def table_rows(
    schema: str,
    table: str,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    sort: Optional[str] = None,
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    q: Optional[str] = None,
):
    if schema not in SCHEMAS:
        raise HTTPException(404, "Таблица не найдена")
    with connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '30s'")
        columns = _columns(conn, schema, table)
        if not columns:
            raise HTTPException(404, "Таблица не найдена")
        visible = [c for c in columns if not HIDDEN_COLUMN_RE.search(c["name"])]
        names = [c["name"] for c in visible]
        if sort is not None and sort not in names:
            raise HTTPException(400, "Нет такого столбца")

        source = sql.Identifier(schema, table)
        where, params = sql.SQL(""), []
        if q:
            # Только по видимым столбцам: иначе поиск выдавал бы содержимое скрытых.
            text = sql.SQL(", ").join(sql.SQL("{}::text").format(sql.Identifier(n)) for n in names)
            where = sql.SQL(" WHERE concat_ws(' ', {}) ILIKE %s").format(text)
            params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        total = conn.execute(
            sql.SQL("SELECT COUNT(*) AS n FROM {}{}").format(source, where), params
        ).fetchone()["n"]
        order = sql.SQL("{} {} NULLS LAST").format(
            sql.Identifier(sort or names[0]), sql.SQL("ASC" if direction == "asc" else "DESC")
        )
        rows = conn.execute(
            sql.SQL("SELECT {} FROM {}{} ORDER BY {} LIMIT %s OFFSET %s").format(
                sql.SQL(", ").join(sql.Identifier(n) for n in names), source, where, order
            ),
            params + [limit, offset],
        ).fetchall()
    return {
        "columns": visible,
        "hidden_columns": [c["name"] for c in columns if c not in visible],
        "total": total,
        "rows": [{k: _cell(v) for k, v in r.items()} for r in rows],
    }


# ── Проверка полноты ─────────────────────────────────────────

def _tz() -> ZoneInfo:
    return ZoneInfo(settings.TIMEZONE)


def _period_start(days: int, now: datetime) -> datetime:
    """Начало первого дня периода по местному времени."""
    first = now.astimezone(_tz()).date() - timedelta(days=days - 1)
    return datetime(first.year, first.month, first.day, tzinfo=_tz())


def _as_dt(v: Any) -> datetime:
    """Соединения платформы могут отдавать время строкой."""
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def atm_status(row: Dict[str, Any], period_start: datetime, now: datetime) -> str:
    """never — не проверялся, error — последняя попытка неудачна, shallow — циклы или история
    остатка загружены не на весь период, stale — проверен давно (открытый цикл мог измениться), ok — данные полные."""
    if row.get("last_error"):
        return "error"
    if not row.get("last_success_at"):
        return "never"
    if _as_dt(row["synced_since"]) > period_start:
        return "shallow"
    if not row.get("balance_since") or _as_dt(row["balance_since"]) > period_start:
        return "shallow"
    if now - _as_dt(row["last_success_at"]) > timedelta(hours=2 * settings.ENCASHMENT_SYNC_HOURS):
        return "stale"
    return "ok"


def expected_polls(day: Any, now: datetime) -> int:
    """Сколько опросов должно быть за день; сегодняшний день считается только до текущего часа."""
    per_day = 24 * 60 / settings.POLL_INTERVAL_MINUTES
    local = now.astimezone(_tz())
    if str(day)[:10] == local.date().isoformat():
        return int((local.hour * 60 + local.minute) / settings.POLL_INTERVAL_MINUTES)
    return int(per_day)


def polls_status(ok: int, expected: int) -> str:
    if expected == 0 or ok >= POLLS_OK_SHARE * expected:
        return "ok"
    return "partial" if ok else "none"


_LOCAL = "AT TIME ZONE %(tz)s"
# Цикл покрывает все местные даты от начала до конца; открытый — до сегодня.
_CYCLE_ON_DAY = f"""
    (e.started_at {_LOCAL})::date <= d.day
    AND COALESCE((e.finished_at {_LOCAL})::date, %(today)s) >= d.day
"""
_UNVERIFIED = f"""
    (s.last_success_at IS NULL OR s.synced_since > (d.day::timestamp {_LOCAL}))
    AND NOT EXISTS (SELECT 1 FROM btech_encashments e WHERE e.atm_id = a.id AND {_CYCLE_ON_DAY})
"""


def _params(days: int, now: datetime) -> Dict[str, Any]:
    start = _period_start(days, now)
    return {"tz": settings.TIMEZONE, "start": start, "start_day": start.date(),
            "today": now.astimezone(_tz()).date()}


@router.get("/coverage", summary="Полнота данных BTech по датам и банкоматам")
def coverage(days: int = Query(92, ge=1, le=MAX_DAYS)):
    now = datetime.now(timezone.utc)
    p = _params(days, now)
    with connect() as conn:
        atm_total = conn.execute("SELECT COUNT(*) AS n FROM atms").fetchone()["n"]
        day_rows = conn.execute(
            f"""
            WITH days AS (
                SELECT generate_series(%(start_day)s::date, %(today)s::date, interval '1 day')::date AS day
            ), polls AS (
                SELECT (started_at {_LOCAL})::date AS day,
                       COUNT(*) FILTER (WHERE status = 'success') AS ok,
                       COUNT(*) FILTER (WHERE status = 'failed') AS failed
                  FROM poll_runs WHERE started_at >= %(start)s GROUP BY 1
            ), snaps AS (
                SELECT (polled_at {_LOCAL})::date AS day, COUNT(DISTINCT atm_id) AS atms
                  FROM atm_snapshots WHERE polled_at >= %(start)s GROUP BY 1
            ), daily AS (
                SELECT day, COUNT(*) AS atms FROM atm_daily WHERE day >= %(start_day)s GROUP BY 1
            ), balances AS (
                SELECT day, COUNT(DISTINCT atm_id) AS atms FROM btech_balance_daily WHERE day >= %(start_day)s GROUP BY 1
            ), covered AS (
                SELECT d.day, COUNT(DISTINCT e.atm_id) AS atms
                  FROM days d JOIN btech_encashments e ON {_CYCLE_ON_DAY} GROUP BY 1
            ), unverified AS (
                SELECT d.day, COUNT(*) AS atms
                  FROM days d CROSS JOIN atms a
                  LEFT JOIN btech_encashment_sync s ON s.atm_id = a.id
                 WHERE {_UNVERIFIED} GROUP BY 1
            )
            SELECT d.day, COALESCE(polls.ok, 0) AS polls_ok, COALESCE(polls.failed, 0) AS polls_failed,
                   COALESCE(snaps.atms, 0) AS snapshot_atms, COALESCE(daily.atms, 0) AS daily_atms, COALESCE(balances.atms, 0) AS balance_atms,
                   COALESCE(covered.atms, 0) AS encash_covered, COALESCE(unverified.atms, 0) AS encash_unverified
              FROM days d
              LEFT JOIN polls USING (day) LEFT JOIN snaps USING (day) LEFT JOIN daily USING (day) LEFT JOIN balances USING (day)
              LEFT JOIN covered USING (day) LEFT JOIN unverified USING (day)
             ORDER BY d.day DESC
            """,
            p,
        ).fetchall()
        atm_rows = conn.execute(
            f"""
            WITH chain AS (
                SELECT atm_id, started_at, finished_at,
                       LAG(finished_at) OVER (PARTITION BY atm_id ORDER BY started_at) AS prev_finished
                  FROM btech_encashments WHERE COALESCE(finished_at, now()) >= %(start)s
            ), stats AS (
                SELECT atm_id, COUNT(*) AS cycles, MIN(started_at) AS first_start, MAX(started_at) AS last_start,
                       COUNT(*) FILTER (WHERE finished_at IS NULL) AS open_cycles,
                       COUNT(*) FILTER (WHERE prev_finished IS NOT NULL AND prev_finished <> started_at) AS chain_gaps
                  FROM chain GROUP BY atm_id
            )
            SELECT a.id, a.tid, a.address, s.last_attempt_at, s.last_success_at, s.synced_since, s.balance_since, s.source_cycles,
                   s.last_error, COALESCE(c.cycles, 0) AS cycles, c.first_start, c.last_start,
                   COALESCE(c.open_cycles, 0) AS open_cycles, COALESCE(c.chain_gaps, 0) AS chain_gaps
              FROM atms a
              LEFT JOIN btech_encashment_sync s ON s.atm_id = a.id
              LEFT JOIN stats c ON c.atm_id = a.id
             ORDER BY a.tid
            """,
            p,
        ).fetchall()

    calendar = []
    for r in day_rows:
        expected = expected_polls(r["day"], now)
        calendar.append({
            **r,
            "polls_expected": expected,
            "polls_status": polls_status(r["polls_ok"], expected),
            "encash_empty": atm_total - r["encash_covered"] - r["encash_unverified"],
        })
    for a in atm_rows:
        a["status"] = atm_status(a, p["start"], now)
    counts: Dict[str, int] = {}
    for a in atm_rows:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    return {
        "days": days,
        "period_start": p["start_day"],
        "atm_total": atm_total,
        "atm_status_counts": counts,
        "days_with_encash_gaps": sum(1 for c in calendar if c["encash_unverified"]),
        "days_with_poll_gaps": sum(1 for c in calendar if c["polls_status"] != "ok"),
        "calendar": calendar,
        "atms": atm_rows,
    }


@router.get("/coverage/day", summary="Банкоматы без проверенных инкассаций за дату")
def coverage_day(day: date):
    now = datetime.now(timezone.utc)
    p = {"tz": settings.TIMEZONE, "today": now.astimezone(_tz()).date(), "day": day}
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT a.id FROM (SELECT %(day)s::date AS day) d CROSS JOIN atms a
                LEFT JOIN btech_encashment_sync s ON s.atm_id = a.id
                WHERE {_UNVERIFIED} ORDER BY a.id""",
            p,
        ).fetchall()
    return {"day": day, "atm_ids": [r["id"] for r in rows]}


# ── Сбор ─────────────────────────────────────────────────────

class PullRequest(BaseModel):
    scope: str = "missing"              # missing / all / atms
    days: int = 92
    atm_ids: Optional[List[int]] = None


def _ids_for_pull(scope: str, days: int, atm_ids: Optional[List[int]]) -> List[int]:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.id, s.last_success_at, s.synced_since, s.balance_since, s.last_error
                 FROM atms a LEFT JOIN btech_encashment_sync s ON s.atm_id = a.id ORDER BY a.id"""
        ).fetchall()
    if scope == "all":
        return [r["id"] for r in rows]
    if scope == "atms":
        known = {r["id"] for r in rows}
        return [a for a in (atm_ids or []) if a in known]
    if scope == "missing":
        start = _period_start(days, now)
        return [r["id"] for r in rows if atm_status(r, start, now) != "ok"]
    raise HTTPException(400, "Неизвестный scope")


class BtechLoginRequest(BaseModel):
    email: str
    password: str
    otpCode: str


@router.post("/btech/login", summary="Ручной вход в BTech (сохраняет JWT на сервере)")
def btech_login(req: BtechLoginRequest, request: Request):
    check_request(request)
    try:
        exp = token_manager.login_manual(req.email, req.password, req.otpCode)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "ok": True,
        "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc),
    }


@router.post("/btech/logout", summary="Сбросить сессию BTech")
def btech_logout(request: Request):
    check_request(request)
    token_manager.clear_session()
    return {"ok": True}


@router.post("/pull", summary="Подтянуть инкассации из BTech")
def pull(req: PullRequest, request: Request):
    if not 1 <= req.days <= MAX_DAYS:
        raise HTTPException(400, "Неверный период")
    if not token_manager.has_valid_session() and not (
        settings.AUTH_OTP_SECRET and settings.AUTH_USERNAME and settings.AUTH_PASSWORD
    ):
        raise HTTPException(
            400,
            f"{LOGIN_REQUIRED} — войдите через форму BTech на этой странице "
            "(или задайте AUTH_OTP_SECRET в .env).",
        )

    # Анти-спам: ручной pull не чаще раза в 30 сек с одного IP.
    ip = (request.client.host if request.client else "unknown") or "unknown"
    now = time.time()
    with _PULL_LOCK:
        last = _PULL_LAST.get(ip, 0.0)
        if now - last < _PULL_MIN_INTERVAL_SEC:
            raise HTTPException(429, "Слишком часто. Подождите несколько секунд.")
        _PULL_LAST[ip] = now

    ids = _ids_for_pull(req.scope, req.days, req.atm_ids)
    if not ids:
        return {"started": False, "total": 0}
    if not encashments.start_job(ids, req.days):
        raise HTTPException(409, "Сбор уже идёт")
    return {"started": True, "total": len(ids)}


@router.post("/pull/stop", summary="Остановить сбор")
def pull_stop():
    encashments.request_stop()
    return {"stopping": True}


@router.get("/status", summary="Состояние автоматического сбора")
def status():
    with connect() as conn:
        last_ok = conn.execute(
            "SELECT MAX(finished_at) AS at FROM poll_runs WHERE status = 'success'"
        ).fetchone()["at"]
        last_run = conn.execute(
            "SELECT started_at, status, atm_count, error_message FROM poll_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        mark = conn.execute(
            "SELECT value FROM history_state WHERE key = %s", (encashments.SYNC_MARK,)
        ).fetchone()
    # Секреты (пароль/OTP/JWT) никогда не отдаём — только флаги готовности.
    exp = token_manager.session_expires_at()
    session_ok = token_manager.has_valid_session()
    return {
        "credentials_set": bool(settings.AUTH_USERNAME and settings.AUTH_PASSWORD),
        "otp_configured": bool(settings.AUTH_OTP_SECRET),
        "btech_session_ok": session_ok,
        "btech_expires_at": (
            datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None
        ),
        "default_btech_email": settings.AUTH_USERNAME or "",
        "collector_enabled": settings.COLLECTOR_ENABLED,
        "collector_running": bool(scheduler._thread and scheduler._thread.is_alive()),
        "poll_interval_minutes": settings.POLL_INTERVAL_MINUTES,
        "encashment_sync_hours": settings.ENCASHMENT_SYNC_HOURS,
        "encashment_history_days": settings.ENCASHMENT_HISTORY_DAYS,
        "last_successful_poll": last_ok,
        "last_poll": last_run,
        "last_full_encashment_sync": (
            datetime.fromtimestamp(mark["value"], timezone.utc) if mark and mark["value"] else None
        ),
        "job": encashments.job_status(),
    }
