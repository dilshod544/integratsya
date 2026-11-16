"""
Реальные данные банкоматов от сборщика atm_monitor (пакет `atm_monitor/` в
этом же проекте: опрашивает monitoring.btech.uz раз в час и пишет в схему
`atm_monitor` базы PostgreSQL; стартует вместе с сервером, см. atm_monitor/scheduler.py).

Этот модуль — read-only клиент той базы. Если данных ещё нет (сборщик не
настроен или первый опрос не завершён) — функции возвращают пустой результат,
дашборд продолжает работать на данных из реестра, просто без «живого» баланса.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from atm_monitor.db import connect

from .config import DEFAULT_CAPACITY

log = logging.getLogger(__name__)

# Реальный номинал купюры в Узбекистане: 1000 и выше. Платформа иногда отдаёт
# "кассеты" с nominal 5/6/7/50/100 и т.п. — это служебные/пустые слоты, а не
# заряженные купюры, их не показываем как деньги.
MIN_REAL_NOMINAL = 1000

_CACHE_TTL_SEC = 60
_states_cache: Dict[str, Any] = {"at": 0.0, "data": {}}
_cassettes_cache: Dict[str, Any] = {"at": 0.0, "data": {}}

# Узбекистан и ближайшие границы — BTech иногда отдаёт 0,0 или мусор вместо GPS.
_UZ_LAT = (37.0, 46.0)
_UZ_LON = (55.0, 74.0)

# id последнего снимка банкомата `a` (коррелированный подзапрос вместо LATERAL)
_LAST_SNAPSHOT_ID = (
    "SELECT s.id FROM atm_snapshots s WHERE s.atm_id = a.id "
    "ORDER BY s.polled_at DESC, s.id DESC LIMIT 1"
)


def _valid_gps(lat: Any, lon: Any) -> Optional[tuple]:
    """None / 0,0 / вне региона — невалидно (не ставим точку на карту)."""
    try:
        if lat is None or lon is None:
            return None
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(la) or not math.isfinite(lo):
        return None
    if la == 0.0 and lo == 0.0:
        return None
    if not (_UZ_LAT[0] <= la <= _UZ_LAT[1] and _UZ_LON[0] <= lo <= _UZ_LON[1]):
        return None
    return la, lo


def _query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    try:
        with connect() as conn:
            return conn.execute(sql, params).fetchall()
    except Exception as e:  # noqa: BLE001 — база сборщика может быть ещё пустой
        log.warning("База atm_monitor недоступна: %s", e)
        return []


def latest_states_by_tid(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """tid -> {balance, agent_status, agent_last_online, forecast_hours, polled_at}.

    Кэшируется на _CACHE_TTL_SEC, чтобы не бить базу на каждый list_atms().
    """
    now = time.time()
    if not force and (now - _states_cache["at"]) < _CACHE_TTL_SEC:
        return _states_cache["data"]

    rows = _query(
        f"""
        SELECT
            a.tid AS tid,
            a.terminal_id AS btech_terminal_id,
            a.serial AS serial,
            a.address AS address,
            a.place AS place,
            a.latitude AS latitude,
            a.longitude AS longitude,
            a.id AS atm_id,
            snap.cdm_total_uzs AS balance,
            snap.cdm_available_uzs AS available,
            snap.agent_status AS agent_status,
            snap.agent_last_online AS agent_last_online,
            snap.polled_at AS polled_at,
            snap.last_transaction_cash_in AS last_incassation,
            (SELECT t.forecast_hours FROM turnover_snapshots t
              WHERE t.atm_id = a.id
                AND (t.currency IS NULL OR UPPER(t.currency) = 'UZS')
              ORDER BY t.polled_at DESC, t.id DESC LIMIT 1) AS forecast_hours,
            f.method AS ml_method,
            f.withdrawal_horizon / NULLIF(fr.horizon_days, 0) AS ml_withdrawal_per_day
        FROM atms a
        JOIN atm_snapshots snap ON snap.id = ({_LAST_SNAPSHOT_ID})
        LEFT JOIN forecast_runs fr
               ON fr.id = (SELECT MAX(id) FROM forecast_runs WHERE status = 'success')
        LEFT JOIN atm_forecast_summary f ON f.run_id = fr.id AND f.atm_id = a.id
        WHERE a.tid IS NOT NULL
        """
    )

    capacities: Dict[int, float] = {}
    if rows:
        try:
            from atm_monitor.history import capacity_by_atm

            with connect() as conn:
                capacities = capacity_by_atm(conn, 0)
        except Exception as e:  # noqa: BLE001
            log.warning("Ёмкость банкоматов по истории недоступна: %s", e)

    data = {}
    for r in rows:
        tid = str(r["tid"])
        gps = _valid_gps(r.get("latitude"), r.get("longitude"))
        data[tid] = {
            "lat": gps[0] if gps else None,
            "lon": gps[1] if gps else None,
            "address": (str(r["address"]).strip() if r.get("address") else None) or None,
            "place": (str(r["place"]).strip() if r.get("place") else None) or None,
            "balance": float(r["balance"]) if r.get("balance") is not None else None,
            "available": float(r["available"]) if r.get("available") is not None else None,
            "capacity": capacities.get(int(r["atm_id"])) if r.get("atm_id") is not None else None,
            "btech_atm_id": int(r["atm_id"]) if r.get("atm_id") is not None else None,
            "agent_status": r.get("agent_status"),
            "agent_last_online": r.get("agent_last_online"),
            "forecast_hours": float(r["forecast_hours"]) if r.get("forecast_hours") is not None else None,
            "polled_at": r.get("polled_at"),
            "last_incassation": r.get("last_incassation"),
            "btech_terminal_id": str(r["btech_terminal_id"]) if r.get("btech_terminal_id") else None,
            "serial": str(r["serial"]) if r.get("serial") else None,
            "ml_method": r.get("ml_method"),
            "ml_withdrawal_per_day": (
                float(r["ml_withdrawal_per_day"]) if r.get("ml_withdrawal_per_day") is not None else None
            ),
        }

    _states_cache["at"] = now
    _states_cache["data"] = data
    return data


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _fill_live(
    row: Dict[str, Any],
    tid: str,
    method: str,
    live: Dict[str, Any],
    cassettes_by_tid: Optional[Dict[str, List[Dict[str, Any]]]],
) -> None:
    row["live"] = True
    row["live_match"] = method
    row["live_tid"] = tid
    if live.get("balance") is not None:
        row["balance"] = live["balance"]
    # Деньги только в исправных кассетах — по ним статус и срок до опустошения.
    row["available_balance"] = live.get("available") if live.get("available") is not None else live.get("balance")
    row["btech_atm_id"] = live.get("btech_atm_id")
    # Реальная ёмкость (максимум загрузки по BTech) — вместо значения по умолчанию из реестра.
    if live.get("capacity") and (not row.get("capacity") or row.get("capacity") == DEFAULT_CAPACITY):
        row["capacity"] = live["capacity"]
    # Локация с BTech, но только если GPS валидный (не 0,0). Иначе оставляем реестр.
    live_gps = _valid_gps(live.get("lat"), live.get("lon"))
    if live_gps:
        row["registry_lat"] = row.get("lat")
        row["registry_lon"] = row.get("lon")
        row["lat"], row["lon"] = live_gps
        row["location_source"] = "btech"
    else:
        row["location_source"] = "registry" if _valid_gps(row.get("lat"), row.get("lon")) else None
    if live.get("address"):
        row["registry_address"] = row.get("address")
        row["address"] = live["address"]
    if live.get("place"):
        row["place"] = live["place"]
    row["agent_status"] = live.get("agent_status")
    row["forecast_hours"] = live.get("forecast_hours")
    row["balance_polled_at"] = live.get("polled_at")
    row["last_incassation"] = live.get("last_incassation")
    # Прогноз снятий (atm_monitor/forecast.py) — его использует hours_to_low_cash.
    row["ml_method"] = live.get("ml_method")
    row["ml_withdrawal_per_day"] = live.get("ml_withdrawal_per_day")
    if cassettes_by_tid is not None:
        row["cassettes"] = cassettes_by_tid.get(tid) or []


def match_live_to_rows(
    rows: List[Dict[str, Any]],
    live_by_tid: Dict[str, Dict[str, Any]],
    cassettes_by_tid: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    max_distance_m: float = 100,
) -> None:
    """Накладывает живые данные BTech на строки реестра.

    1) Точное совпадение terminal_id / atm_number / serial с tid, extra terminalId
       или serial BTech. Ключи, которые встречаются у двух разных ATM, игнорируются.
    2) Остаток — ближайшая точка в радиусе max_distance_m, каждый live-tid
       отдаётся только одному банкомату (иначе соседние ATM делили бы один остаток).
    """
    if not live_by_tid:
        for row in rows:
            row["live"] = False
        return

    by_key: Dict[str, str] = {}
    collisions: set = set()
    for tid, state in live_by_tid.items():
        for raw in (tid, state.get("btech_terminal_id"), state.get("serial")):
            if not raw:
                continue
            key = str(raw).strip()
            if not key:
                continue
            prev = by_key.get(key)
            if prev and prev != tid:
                collisions.add(key)
            else:
                by_key[key] = tid
    for key in collisions:
        by_key.pop(key, None)

    used: set = set()
    unmatched: List[Dict[str, Any]] = []
    for row in rows:
        hit = None
        for raw in (row.get("terminal_id"), row.get("atm_number"), row.get("serial")):
            if raw is None:
                continue
            tid = by_key.get(str(raw).strip())
            if tid and tid not in used:
                hit = tid
                break
        if hit:
            used.add(hit)
            _fill_live(row, hit, "terminal_id", live_by_tid[hit], cassettes_by_tid)
        else:
            unmatched.append(row)

    pairs = []
    for row in unmatched:
        try:
            lat = float(row["lat"]) if row.get("lat") is not None else None
            lon = float(row["lon"]) if row.get("lon") is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        if lat is None or lon is None:
            row["live"] = False
            continue
        for tid, state in live_by_tid.items():
            if tid in used:
                continue
            slat, slon = state.get("lat"), state.get("lon")
            if slat is None or slon is None:
                continue
            dist = _haversine_m(lat, lon, slat, slon)
            if dist <= max_distance_m:
                pairs.append((dist, row, tid))
    pairs.sort(key=lambda item: item[0])
    claimed = set()
    for _dist, row, tid in pairs:
        rid = id(row)
        if rid in claimed or tid in used:
            continue
        used.add(tid)
        claimed.add(rid)
        _fill_live(row, tid, "location", live_by_tid[tid], cassettes_by_tid)
    for row in unmatched:
        if "live" not in row:
            row["live"] = False


def attach_live_to_rows(rows: List[Dict[str, Any]]) -> None:
    match_live_to_rows(rows, latest_states_by_tid(), all_latest_cassettes())


def find_nearest_tid(lat: Optional[float], lon: Optional[float], max_distance_m: float = 100) -> Optional[str]:
    """Резерв на случай, когда terminal_id в реестре не совпадает с tid BTech
    (разные системы нумерации у разных сетей — WAY4/UzCard и т.п.), но это
    физически тот же банкомат: реестр и BTech почти всегда указывают на один
    и тот же адрес с точностью в единицы-десятки метров.

    Матчит только координаты — при плотной застройке (несколько ATM в одном
    здании/на одной улице) может по ошибке зацепить банкомат-сосед, а не
    буквально тот же физический аппарат; это компромисс, а не гарантия."""
    if lat is None or lon is None:
        return None
    states = latest_states_by_tid()
    deg_margin = max_distance_m / 111_000  # грубая оценка градуса в метрах, с запасом
    best_tid, best_dist = None, max_distance_m
    for tid, s in states.items():
        slat, slon = s.get("lat"), s.get("lon")
        if slat is None or slon is None:
            continue
        if abs(slat - lat) > deg_margin or abs(slon - lon) > deg_margin:
            continue
        d = _haversine_m(lat, lon, slat, slon)
        if d < best_dist:
            best_dist, best_tid = d, tid
    return best_tid


def _row_to_cassette(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "index": r.get("cassette_index"),
        "type": r.get("cassette_type"),
        "status": r.get("status"),
        "count": r.get("count"),
        "currency": r.get("currency"),
        "nominal": r.get("nominal"),
        "balance": (r["count"] * r["nominal"]) if r.get("count") is not None and r.get("nominal") is not None else None,
    }


# Только "живые" кассеты купюр: REJECTCASSETTE/RETRACTCASSETTE/RECYCLING — это
# отбракованные/принятые при депозите купюры, а не выдаваемый остаток, и мы их
# не показываем как деньги, которые можно снять.
_CASSETTE_FILTER_SQL = "c.cassette_type = 'BILLCASSETTE' AND c.nominal >= %s"


def cassettes_by_tid(tid: str) -> List[Dict[str, Any]]:
    """Реальные кассеты банкомата на момент последнего опроса (пусто, если ATM не в BTech).

    Список, а не словарь по номиналу: у части моделей встречается несколько
    кассет с одним и тем же номиналом (например, две по 200 000 — одна LOW,
    другая INOP) — группировка по номиналу потеряла бы одну из них."""
    rows = _query(
        f"""
        SELECT c.cassette_index, c.cassette_type, c.status, c.count, c.currency, c.nominal
        FROM atms a
        JOIN cassette_snapshots c ON c.atm_snapshot_id = ({_LAST_SNAPSHOT_ID})
        WHERE a.tid = %s AND {_CASSETTE_FILTER_SQL}
        ORDER BY c.cassette_index
        """,
        (str(tid), MIN_REAL_NOMINAL),
    )
    return [_row_to_cassette(r) for r in rows]


def all_latest_cassettes(force: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """tid -> [кассеты] для ВСЕХ банкоматов одним запросом (для списка ATM без
    N+1 запроса на каждый). Кэшируется так же, как latest_states_by_tid."""
    now = time.time()
    if not force and (now - _cassettes_cache["at"]) < _CACHE_TTL_SEC:
        return _cassettes_cache["data"]

    rows = _query(
        f"""
        SELECT a.tid AS tid, c.cassette_index, c.cassette_type, c.status, c.count, c.currency, c.nominal
        FROM atms a
        JOIN cassette_snapshots c ON c.atm_snapshot_id = ({_LAST_SNAPSHOT_ID})
        WHERE a.tid IS NOT NULL AND {_CASSETTE_FILTER_SQL}
        ORDER BY a.tid, c.cassette_index
        """,
        (MIN_REAL_NOMINAL,),
    )

    data: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        data.setdefault(str(r["tid"]), []).append(_row_to_cassette(r))

    _cassettes_cache["at"] = now
    _cassettes_cache["data"] = data
    return data


def balance_history_by_tid(tid: str, days: int = 30) -> List[Dict[str, Any]]:
    """Реальный ряд общего остатка UZS по времени — задел под прогноз."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _query(
        """
        SELECT s.polled_at, s.cdm_total_uzs
        FROM atms a
        JOIN atm_snapshots s ON s.atm_id = a.id
        WHERE a.tid = %s AND s.polled_at >= %s
        ORDER BY s.polled_at
        """,
        (str(tid), since),
    )
    return [
        {
            "polled_at": r.get("polled_at"),
            "balance": float(r["cdm_total_uzs"]) if r.get("cdm_total_uzs") is not None else None,
        }
        for r in rows
    ]


def collector_status() -> Dict[str, Any]:
    """Статус самого сборщика atm_monitor (последний прогон опроса BTech),
    а не отдельного банкомата — для виджета на главной странице дашборда."""
    rows = _query(
        """
        SELECT id, started_at, finished_at, status, atm_count, error_message
        FROM poll_runs
        ORDER BY id DESC
        LIMIT 1
        """
    )
    if not rows:
        return {
            "connected": False,
            "last_poll_id": None,
            "started_at": None,
            "finished_at": None,
            "status": None,
            "atm_count": None,
            "error": None,
        }
    r = rows[0]
    return {
        "connected": True,
        "last_poll_id": r.get("id"),
        "started_at": r.get("started_at"),
        "finished_at": r.get("finished_at"),
        "status": r.get("status"),
        "atm_count": r.get("atm_count"),
        "error": r.get("error_message"),
    }
