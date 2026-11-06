"""Инкассации банкоматов из BTech (вкладка «Инкассации» карточки банкомата).

Для каждого банкомата берутся циклы за последние ENCASHMENT_HISTORY_DAYS дней:
суммы, кассеты и действия инкассатора. Повторный сбор обновляет те же строки,
поэтому открытый цикл догоняет свои суммы и закрывается без дублей.

Один прогон для одного банкомата:  python -m atm_monitor.encashments 18906161
Для всех:                          python -m atm_monitor.encashments"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from . import daily_history
from .api_client import fetch_balance_history, fetch_encashments, fetch_turnover_history
from .config import settings
from .db import connect
from .utils import parse_dt, parse_float, parse_int

logger = logging.getLogger("atm_monitor.encashments")

# Подряд столько ошибок — проблема не в банкомате, а в доступе или адресе запроса.
MAX_CONSECUTIVE_ERRORS = 5
SYNC_MARK = "encashments_synced_at"

_lock = threading.Lock()
_stop = threading.Event()
_job: Dict[str, Any] = {"running": False}


# ── Чистые функции (тестируются без базы) ────────────────────

def strip_nul(value: Any) -> Any:
    """В текстах ошибок устройств BTech бывает \\u0000, а PostgreSQL такой символ не хранит."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    return value


def uzs_total(block: Optional[Dict[str, Any]]) -> Optional[float]:
    """Сумма UZS из блока вида {"currencies": {"UZS": {"total": N}}}. Нет данных — None, не 0."""
    uzs = ((block or {}).get("currencies") or {}).get("UZS") or {}
    return parse_float(uzs.get("total"))


def in_window(item: Dict[str, Any], since: datetime) -> bool:
    """Цикл попадает в период, если он ещё идёт или закончился не раньше `since`."""
    finished = parse_dt(item.get("finishedAt"))
    return finished is None or finished >= since


def encashment_row(item: Dict[str, Any], atm_id: int) -> Dict[str, Any]:
    return {
        "id": item["id"],
        "atm_id": atm_id,
        "started_at": parse_dt(item.get("startedAt")),
        "finished_at": parse_dt(item.get("finishedAt")),
        "loaded_uzs": uzs_total(item.get("loaded")),
        "unloaded_uzs": uzs_total(item.get("unloaded")),
        "left_uzs": uzs_total(item.get("left")),
        "dispensed_uzs": uzs_total(item.get("dispensed")),
        "presented_uzs": uzs_total(item.get("presented")),
        "rejected_uzs": uzs_total(item.get("rejected")),
        "accepted_uzs": uzs_total(item.get("accepted")),
        "retracted_total": parse_float((item.get("retracted") or {}).get("total")),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


def cassette_rows(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "encashment_id": item["id"],
            "number": c["number"],
            "unit_id": c.get("id"),
            "cassette_type": c.get("type"),
            "status": c.get("status"),
            "currency": c.get("currency"),
            "nominal": parse_int(c.get("values")),
            "count": parse_int(c.get("count")),
            "loaded": parse_int(c.get("loaded")),
            "dispensed_count": parse_int(c.get("dispensedCount")),
            "presented_count": parse_int(c.get("presentedCount")),
            "rejected_count": parse_int(c.get("rejectedCount")),
            "retracted_count": parse_int(c.get("retractedCount")),
        }
        for c in item.get("cashOutTotals") or []
    ]


def action_rows(item: Dict[str, Any], atm_id: int) -> List[Dict[str, Any]]:
    return [
        {
            "id": a["id"],
            "encashment_id": item["id"],
            "atm_id": atm_id,
            "created_at": parse_dt(a.get("createdAt")),
            "evtno": parse_int(a.get("evtno")),
            "data": a.get("data"),
        }
        for a in item.get("actions") or []
    ]


# ── Запись ───────────────────────────────────────────────────

def _upsert_many(conn, table: str, key: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    keys = [k.strip() for k in key.split(",")]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in keys)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)}) "
        f"ON CONFLICT ({key}) DO UPDATE SET {updates}"
    )
    with conn.cursor() as cur:
        cur.executemany(sql, rows)


def save_encashments(conn, atm_id: int, items: Iterable[Dict[str, Any]]) -> int:
    saved = 0
    for item in map(strip_nul, items):
        _upsert_many(conn, "btech_encashments", "id", [{**encashment_row(item, atm_id), "synced_at": datetime.now(timezone.utc)}])
        _upsert_many(conn, "btech_encashment_cassettes", "encashment_id, number", cassette_rows(item))
        _upsert_many(conn, "btech_encashment_actions", "id", action_rows(item, atm_id))
        saved += 1
    return saved


# ── Сбор ─────────────────────────────────────────────────────

def _all_atm_ids() -> List[int]:
    with connect() as conn:
        return [int(r["id"]) for r in conn.execute("SELECT id FROM atms ORDER BY id").fetchall()]


def _record_success(conn, atm_id: int, since: datetime, source_cycles: int) -> None:
    """Банкомат проверен до `since`: цикла за дату в этом периоде нет — значит, его нет и в BTech."""
    conn.execute(
        """INSERT INTO btech_encashment_sync (atm_id, last_attempt_at, last_success_at, synced_since, balance_since, source_cycles)
           VALUES (%s, now(), now(), %s, %s, %s)
           ON CONFLICT (atm_id) DO UPDATE SET
               last_attempt_at = now(), last_success_at = now(), last_error = NULL,
               synced_since = LEAST(btech_encashment_sync.synced_since, EXCLUDED.synced_since),
               balance_since = LEAST(btech_encashment_sync.balance_since, EXCLUDED.balance_since),
               source_cycles = EXCLUDED.source_cycles""",
        (atm_id, since, since, source_cycles),
    )


def _save_daily_history(conn, atm_id: int, since: datetime, now: datetime) -> None:
    """Если период уже загружен, обновляются только последние дни: отчёты BTech объёмные."""
    row = conn.execute("SELECT balance_since FROM btech_encashment_sync WHERE atm_id = %s", (atm_id,)).fetchone()
    covered = row and row["balance_since"] and datetime.fromisoformat(str(row["balance_since"])) <= since
    start = now - timedelta(days=settings.DAILY_HISTORY_REFRESH_DAYS) if covered else since
    tz = ZoneInfo(settings.TIMEZONE)
    # На день раньше: первый день ответа неполный и отбрасывается.
    first = (start.astimezone(tz) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    last = now.astimezone(tz).replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    _upsert_many(conn, "btech_balance_daily", "atm_id, day, cassette_index",
                 daily_history.balance_rows(fetch_balance_history(atm_id, first, last), atm_id))
    _upsert_many(conn, "btech_turnover_daily", "atm_id, day, currency, nominal",
                 daily_history.turnover_rows(fetch_turnover_history(atm_id, first, last), atm_id))


def _record_error(atm_id: int, error: Exception) -> None:
    """Сбой записи ошибки не должен останавливать обход остальных банкоматов."""
    try:
        with connect() as conn:
            conn.execute(
                """INSERT INTO btech_encashment_sync (atm_id, last_attempt_at, last_error)
                   VALUES (%s, now(), %s)
                   ON CONFLICT (atm_id) DO UPDATE SET last_attempt_at = now(), last_error = EXCLUDED.last_error""",
                (atm_id, str(error)[:500]),
            )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось записать ошибку банкомата %s", atm_id)


def sync_encashments(
    atm_ids: Optional[List[int]] = None,
    days: Optional[int] = None,
    on_progress: Optional[Callable[[Dict[str, int], int], None]] = None,
    stop: Optional[threading.Event] = None,
) -> Dict[str, int]:
    """Обновляет инкассации за `days` дней. Каждый банкомат — своя транзакция: сбой на одном
    не теряет остальные. Возвращает {atms, encashments, errors, aborted, stopped}."""
    if not settings.ATM_ENCASHMENT_PATH:
        raise RuntimeError("ATM_ENCASHMENT_PATH не задан: неизвестен адрес запроса инкассаций BTech.")

    since = datetime.now(timezone.utc) - timedelta(days=days or settings.ENCASHMENT_HISTORY_DAYS)
    stats = {"atms": 0, "encashments": 0, "errors": 0, "aborted": 0, "stopped": 0}
    consecutive = 0
    for atm_id in (_all_atm_ids() if atm_ids is None else atm_ids):
        if stop is not None and stop.is_set():
            stats["stopped"] = 1
            break
        try:
            source = fetch_encashments(atm_id)
            items = [i for i in source if in_window(i, since)]
            with connect() as conn:
                stats["encashments"] += save_encashments(conn, atm_id, items)
                _save_daily_history(conn, atm_id, since, datetime.now(timezone.utc))
                _record_success(conn, atm_id, since, len(source))
            stats["atms"] += 1
            consecutive = 0
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            consecutive += 1
            logger.warning("Инкассации банкомата %s не получены: %s", atm_id, e)
            _record_error(atm_id, e)
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                stats["aborted"] = 1
                logger.error("%d ошибок подряд, сбор инкассаций остановлен.", consecutive)
                break
        if on_progress:
            on_progress(stats, atm_id)
        time.sleep(settings.ENCASHMENT_REQUEST_PAUSE_SEC)
    logger.info("Инкассации: %s", stats)
    return stats


# ── Фоновое задание: одно на процесс, с прогрессом и остановкой ─────

def job_status() -> Dict[str, Any]:
    return dict(_job)


def request_stop() -> None:
    _stop.set()


def _run_job(atm_ids: Optional[List[int]], days: Optional[int], source: str) -> Dict[str, int]:
    """Вызывается, когда _lock уже захвачен."""
    ids = _all_atm_ids() if atm_ids is None else atm_ids
    _stop.clear()
    _job.clear()
    _job.update(running=True, source=source, days=days or settings.ENCASHMENT_HISTORY_DAYS, total=len(ids),
                done=0, encashments=0, errors=0, current_atm=None, result=None, error=None,
                started_at=datetime.now(timezone.utc).isoformat(), finished_at=None)

    def progress(stats: Dict[str, int], atm_id: int) -> None:
        _job.update(done=stats["atms"] + stats["errors"], encashments=stats["encashments"],
                    errors=stats["errors"], current_atm=atm_id)

    try:
        stats = sync_encashments(ids, days, progress, _stop)
        _job["result"] = "stopped" if stats["stopped"] else "aborted" if stats["aborted"] else "finished"
        return stats
    except Exception as e:  # noqa: BLE001 — например, база недоступна
        _job.update(result="failed", error=str(e)[:500])
        raise
    finally:
        _job.update(running=False, finished_at=datetime.now(timezone.utc).isoformat())


def start_job(atm_ids: Optional[List[int]] = None, days: Optional[int] = None, source: str = "manual") -> bool:
    """Запускает сбор в фоне. False — другой сбор уже идёт."""
    if not _lock.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            _run_job(atm_ids, days, source)
        except Exception:  # noqa: BLE001
            logger.exception("Сбор инкассаций завершился с ошибкой")
        finally:
            _lock.release()

    threading.Thread(target=work, name="atm-encashments", daemon=True).start()
    return True


def _sync_if_due() -> None:
    if not _lock.acquire(blocking=False):
        return
    try:
        with connect() as conn:
            row = conn.execute("SELECT value FROM history_state WHERE key = %s", (SYNC_MARK,)).fetchone()
        last = int(row["value"]) if row and row["value"] else 0
        if time.time() - last < settings.ENCASHMENT_SYNC_HOURS * 3600:
            return
        stats = _run_job(None, None, "auto")
        if stats["aborted"] or stats["stopped"]:     # отметку не ставим: следующий опрос попробует снова
            return
        with connect() as conn:
            conn.execute(
                """INSERT INTO history_state (key, value, updated_at) VALUES (%s, %s, now())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (SYNC_MARK, int(time.time())),
            )
    except Exception:  # noqa: BLE001 — сбор не должен ронять сервер
        logger.exception("Сбор инкассаций завершился с ошибкой")
    finally:
        _lock.release()


def start_sync_if_due() -> None:
    """Запускается после опроса. Обход всех банкоматов долгий, поэтому идёт отдельным потоком."""
    if settings.ATM_ENCASHMENT_PATH:
        threading.Thread(target=_sync_if_due, name="atm-encashments-auto", daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(sync_encashments([int(a) for a in sys.argv[1:]] or None))
