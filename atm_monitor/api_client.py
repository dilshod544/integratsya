import logging

import requests

from .auth import token_manager
from .config import settings

logger = logging.getLogger("atm_monitor.api_client")


def _headers() -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {token_manager.get_token()}",
    }


def _get_items(url: str, params: dict) -> list:
    """GET списка. При 401 один раз принудительно обновляет токен и повторяет запрос."""
    resp = requests.get(url, params=params, headers=_headers(), timeout=settings.REQUEST_TIMEOUT)

    if resp.status_code == 401:
        logger.warning("Получили 401, обновляем токен и повторяем запрос")
        try:
            token_manager.force_refresh()
        except RuntimeError:
            # Сессия сброшена; без AUTH_OTP_SECRET нужен ручной вход.
            raise
        resp = requests.get(url, params=params, headers=_headers(), timeout=settings.REQUEST_TIMEOUT)

    resp.raise_for_status()
    data = resp.json()

    # API может отдавать либо голый массив, либо {"items": [...], "total": N}
    return data.get("items", []) if isinstance(data, dict) else data


def fetch_encashments(atm_id: int) -> list:
    """Все циклы инкассации банкомата (вкладка «Инкассации»).
    Без limit=-1 BTech отдаёт только первые 10 циклов."""
    url = settings.API_BASE_URL.rstrip("/") + settings.ATM_ENCASHMENT_PATH.format(atm_id=atm_id)
    return _get_items(url, {"brief": "true", "raw": "true", "limit": -1, "lang": settings.QUERY_PARAMS["lang"]})


def _fetch_report(atm_id: int, name: str, start: str, end: str, **extra) -> list:
    url = settings.API_BASE_URL.rstrip("/") + settings.ATM_REPORT_PATH.format(atm_id=atm_id) + "/" + name
    return _get_items(url, {"dateRange": f"{start},{end}", "lang": settings.QUERY_PARAMS["lang"], **extra})


def fetch_balance_history(atm_id: int, start: str, end: str) -> list:
    """Остаток каждой кассеты на конец дня."""
    return _fetch_report(atm_id, "balanceHistory", start, end)


def fetch_turnover_history(atm_id: int, start: str, end: str) -> list:
    """Выдачи по дням и номиналам."""
    return _fetch_report(atm_id, "turnoverHistory", start, end, dateTrunc="daily")


def fetch_all_atms() -> list:
    """Тянет все банкоматы постранично (offset/limit), пока не кончатся записи."""

    url = settings.API_BASE_URL.rstrip("/") + settings.ATM_LIST_PATH
    items: list = []
    offset = 0

    while True:
        params = dict(settings.QUERY_PARAMS)
        params["offset"] = offset
        params["limit"] = settings.PAGE_LIMIT

        batch = _get_items(url, params)
        if not batch:
            break

        items.extend(batch)
        logger.info("Загружено %d записей (offset=%d)", len(batch), offset)

        if len(batch) < settings.PAGE_LIMIT:
            break
        offset += settings.PAGE_LIMIT

    return items
