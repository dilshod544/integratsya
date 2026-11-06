"""Сборщик данных atm_monitor: опрашивает monitoring.btech.uz раз в POLL_INTERVAL_MINUTES.

Работает двумя способами:
  * фоновым потоком внутри сервера (start_background) — так его запускает ./run.sh;
  * отдельно, один опрос и выход:  python -m atm_monitor  (для cron/launchd).

Цикл считает интервал по настенным часам и просыпается раз в минуту, поэтому
после сна/пробуждения машины пропущенный опрос выполняется сразу (в отличие
от APScheduler внутри процесса, который после sleep/wake переставал срабатывать)."""
import logging
import threading
import time

from .api_client import fetch_all_atms
from .config import settings
from .db import connect, init_db
from .encashments import start_sync_if_due
from .history import safe_process_history
from .ingest import ingest

logger = logging.getLogger("atm_monitor.scheduler")

# После сбоя (например, кратковременный обрыв DNS/сети) не ждём полный час —
# пробуем снова через минуту, иначе временный сбой на минуту выглядит на
# дашборде как «не обновлялось» ещё почти час.
RETRY_DELAY_SEC = 60

_thread = None
_stop = threading.Event()
_run_lock = threading.Lock()


# Хук «после опроса» — платформа (api/main.py) регистрирует пересчёт рекомендаций.
_after_poll_hooks = []


def on_after_poll(fn) -> None:
    if fn not in _after_poll_hooks:
        _after_poll_hooks.append(fn)


def _after_poll() -> None:
    for fn in _after_poll_hooks:
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка обработчика после опроса")


def run_once() -> bool:
    """Один опрос. Ошибки логируются, но не пробрасываются (сборщик не должен
    ронять сервер). Возвращает True при успехе."""
    if not _run_lock.acquire(blocking=False):
        logger.info("Опрос уже выполняется — пропускаем.")
        return False
    try:
        logger.info("Старт опроса банкоматов...")
        items = fetch_all_atms()
        logger.info("Получено %d записей, сохраняем в БД...", len(items))
        poll_run_id = ingest(items)
        logger.info("Готово, poll_run_id=%s", poll_run_id)
        safe_process_history()          # события, инкассации, суточные итоги
        _after_poll()                   # рекомендации инкассаций по свежим данным
        start_sync_if_due()             # инкассации из карточек банкоматов
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Опрос завершился с ошибкой (%s), следующая попытка по расписанию", e)
        return False
    finally:
        _run_lock.release()


def _last_success_ts() -> float:
    """Unix-время последнего успешного опроса (0 — ещё не было)."""
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT EXTRACT(EPOCH FROM MAX(finished_at)) AS ts FROM poll_runs WHERE status='success'"
            ).fetchone()
        return float(row["ts"]) if row and row["ts"] else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _loop():
    interval = settings.POLL_INTERVAL_MINUTES * 60
    # Опрашиваем сразу, только если данные устарели (или опроса ещё не было) —
    # частые перезапуски сервера (--reload) не должны долбить платформу.
    stale = time.time() - _last_success_ts() >= interval
    if not (settings.RUN_IMMEDIATELY_ON_START and stale):
        next_run = _last_success_ts() + interval if _last_success_ts() else time.time() + interval
    else:
        next_run = 0.0
    while not _stop.is_set():
        if time.time() >= next_run:
            ok = run_once()
            next_run = time.time() + (interval if ok else RETRY_DELAY_SEC)
        _stop.wait(60)


def start_background():
    """Запускает сборщик в фоне. Без AUTH_USERNAME/AUTH_PASSWORD не стартует —
    сервер продолжает работать без «живых» данных."""
    global _thread
    if not settings.COLLECTOR_ENABLED:
        logger.info("Сборщик atm_monitor отключён (ATM_COLLECTOR=0).")
        return
    if not settings.AUTH_USERNAME or not settings.AUTH_PASSWORD:
        logger.warning(
            "Сборщик atm_monitor не запущен: задайте AUTH_USERNAME и AUTH_PASSWORD в .env "
            "(логин на monitoring.btech.uz). Остальная платформа работает без живых данных."
        )
        return
    if _thread and _thread.is_alive():
        return
    init_db()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="atm-monitor-collector", daemon=True)
    _thread.start()
    logger.info("Сборщик atm_monitor запущен (интервал %d мин).", settings.POLL_INTERVAL_MINUTES)


def stop_background():
    _stop.set()


def main():
    """Один опрос и выход (для внешнего планировщика cron/launchd)."""
    init_db()
    run_once()
