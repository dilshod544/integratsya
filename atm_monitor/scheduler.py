"""Сборщик данных atm_monitor: опрашивает monitoring.btech.uz раз в POLL_INTERVAL_MINUTES.

Работает двумя способами:
  * фоновым потоком внутри сервера (start_background) — так его запускает ./run.sh;
  * отдельно, один опрос и выход:  python -m atm_monitor  (для cron/launchd).

Цикл считает интервал по настенным часам и просыпается раз в минуту, поэтому
после сна/пробуждения машины пропущенный опрос выполняется сразу (в отличие
от APScheduler внутри процесса, который после sleep/wake переставал срабатывать).

Важно: lock держится только на fetch+ingest. История и пересчёт плана — после
освобождения lock, иначе долгий refresh_plan/history блокирует следующие опросы
на часы (ручной «Опросить BTech» тоже не мог взять lock).
Watchdog перезапускает мёртвый поток и форсирует опрос, если success давно не было.
"""
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

# После сбоя (например, кратковременный обрыв DNS/сети) не ждём полный интервал —
# пробуем снова через минуту.
RETRY_DELAY_SEC = 60
# Нет success дольше чем N интервалов → watchdog форсирует опрос.
STALE_INTERVALS = 2
WATCH_EVERY_SEC = 60

_thread = None
_watch_thread = None
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


def _post_poll() -> None:
    """Тяжёлая работа после сохранения — вне lock опроса."""
    safe_process_history()
    _after_poll()
    start_sync_if_due()


def run_once() -> bool:
    """Один опрос. Ошибки логируются, но не пробрасываются (сборщик не должен
    ронять сервер). Возвращает True при успехе ingest."""
    if not _run_lock.acquire(blocking=False):
        logger.info("Опрос уже выполняется — пропускаем.")
        return False
    ok = False
    try:
        logger.info("Старт опроса банкоматов...")
        items = fetch_all_atms()
        logger.info("Получено %d записей, сохраняем в БД...", len(items))
        poll_run_id = ingest(items)
        logger.info("Готово, poll_run_id=%s", poll_run_id)
        ok = True
    except Exception as e:  # noqa: BLE001
        logger.error("Опрос завершился с ошибкой (%s), следующая попытка по расписанию", e)
        ok = False
    finally:
        _run_lock.release()

    if ok:
        try:
            _post_poll()
        except Exception:  # noqa: BLE001
            # Данные уже сохранены; постобработка не должна ломать следующий цикл.
            logger.exception("Постобработка после опроса завершилась с ошибкой")
    return ok


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


def _interval_sec() -> int:
    return max(60, int(settings.POLL_INTERVAL_MINUTES) * 60)


def _loop():
    interval = _interval_sec()
    # Опрашиваем сразу, только если данные устарели (или опроса ещё не было) —
    # частые перезапуски сервера (--reload) не должны долбить платформу.
    last = _last_success_ts()
    stale = time.time() - last >= interval
    if not (settings.RUN_IMMEDIATELY_ON_START and stale):
        next_run = last + interval if last else time.time() + interval
    else:
        next_run = 0.0
    while not _stop.is_set():
        try:
            if time.time() >= next_run:
                ok = run_once()
                next_run = time.time() + (interval if ok else RETRY_DELAY_SEC)
        except Exception:  # noqa: BLE001 — цикл не должен умирать
            logger.exception("Сбой цикла сборщика")
            next_run = time.time() + RETRY_DELAY_SEC
        _stop.wait(60)


def _watch_loop():
    """Если поток сборщика умер или success давно не было — чиним без рестарта сервера."""
    while not _stop.is_set():
        try:
            if not settings.COLLECTOR_ENABLED:
                pass
            elif not settings.AUTH_USERNAME or not settings.AUTH_PASSWORD:
                pass
            elif not (_thread and _thread.is_alive()):
                logger.warning("Поток сборщика не жив — перезапуск")
                start_background()
            else:
                last = _last_success_ts()
                age = time.time() - last if last else None
                limit = _interval_sec() * STALE_INTERVALS
                if last and age is not None and age > limit and not _run_lock.locked():
                    logger.warning(
                        "Нет успешного опроса %.0f мин (лимит %d) — принудительный запуск",
                        age / 60.0, limit // 60,
                    )
                    threading.Thread(target=run_once, name="atm-monitor-watchdog", daemon=True).start()
        except Exception:  # noqa: BLE001
            logger.exception("Сбой watchdog сборщика")
        _stop.wait(WATCH_EVERY_SEC)


def start_background():
    """Запускает сборщик в фоне. Без AUTH_USERNAME/AUTH_PASSWORD не стартует —
    сервер продолжает работать без «живых» данных."""
    global _thread, _watch_thread
    if not settings.COLLECTOR_ENABLED:
        logger.info("Сборщик atm_monitor отключён (ATM_COLLECTOR=0).")
        return
    if not settings.AUTH_USERNAME or not settings.AUTH_PASSWORD:
        logger.warning(
            "Сборщик atm_monitor не запущен: задайте AUTH_USERNAME и AUTH_PASSWORD в .env "
            "(логин на monitoring.btech.uz). Остальная платформа работает без живых данных."
        )
        return
    init_db()
    _stop.clear()
    if not (_thread and _thread.is_alive()):
        _thread = threading.Thread(target=_loop, name="atm-monitor-collector", daemon=True)
        _thread.start()
        logger.info("Сборщик atm_monitor запущен (интервал %d мин).", settings.POLL_INTERVAL_MINUTES)
    if not (_watch_thread and _watch_thread.is_alive()):
        _watch_thread = threading.Thread(target=_watch_loop, name="atm-monitor-watchdog", daemon=True)
        _watch_thread.start()


def stop_background():
    _stop.set()


def main():
    """Один опрос и выход (для внешнего планировщика cron/launchd)."""
    init_db()
    run_once()
