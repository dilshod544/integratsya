"""История состояний банкоматов из снимков BTech.

После каждого опроса (и при старте сервера — для уже накопленных снимков)
обрабатываются новые снимки atm_snapshots:

  * доступный остаток cdm_available_uzs — деньги только в исправных кассетах
    (INOP/MISSING не выдают, хотя BTech включает их в cdm_total_uzs);
  * cdm_initial_uzs — сумма последней загрузки (cdmInitialAmount из JSON BTech);
  * atm_state_events — только переходы состояния (агент, обслуживание,
    устройства, статус каждой кассеты, «нет денег»);
  * atm_incassations — фактические загрузки: смена cdmInitialAmount или
    резкий рост остатка (с учётом снятия кассет: остаток падает почти в ноль,
    а на следующем опросе возвращается — «до» берётся до падения);
  * atm_daily — суточные итоги (снятия, средний/минимальный остаток, часы без денег).

Обработка идемпотентна: отметка history_state('snapshots') хранит последний
обработанный id снимка. Сырые JSON старше RAW_JSON_KEEP_DAYS обнуляются —
все нужные поля уже лежат в колонках."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import settings
from .db import connect

logger = logging.getLogger("atm_monitor.history")

# Кассеты, деньги из которых банкомат выдать не может.
BAD_CASSETTE_STATUSES = {"INOP", "MISSING", "FATAL", "MANIP"}
MIN_REAL_NOMINAL = 1000
MAX_REAL_NOMINAL = 200_000
UZS_CODES = {None, "", "UZS", "860"}

# Рост остатка между опросами, который считаем инкассацией, а не депозитом клиента.
JUMP_MIN_UZS = 20_000_000
JUMP_MIN_SHARE = 0.10
# Снятие кассет: остаток падает до ≤10 % и возвращается в пределах этого окна.
SWAP_WINDOW = timedelta(hours=6)

BATCH = 20_000
RAW_JSON_KEEP_DAYS = int(os.getenv("RAW_JSON_KEEP_DAYS", "45"))

_DEVICE_COLS = (
    "dispenser_status", "acceptor_status", "card_reader_status", "contactless_status",
    "epp_status", "printer_status", "jprinter_status", "barcode_reader_status",
)

_lock = threading.Lock()


# ── Чистые функции (тестируются без базы) ────────────────────

def is_uzs(currency: Any) -> bool:
    return (str(currency).strip().upper() if currency is not None else None) in UZS_CODES


def available_from_cassettes(cassettes: Iterable[Dict[str, Any]], total: Optional[float]) -> Optional[float]:
    """Сумма в исправных кассетах с купюрами UZS. Нет данных по кассетам — общий остаток."""
    bills = [
        c for c in cassettes
        if c.get("cassette_type") == "BILLCASSETTE"
        and MIN_REAL_NOMINAL <= int(c.get("nominal") or 0) <= MAX_REAL_NOMINAL
        and is_uzs(c.get("currency"))
    ]
    if not bills:
        return total
    return float(sum(
        int(c.get("count") or 0) * int(c["nominal"])
        for c in bills if (c.get("status") or "").upper() not in BAD_CASSETTE_STATUSES
    ))


def notes_by_nominal(cassettes: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c in cassettes:
        nominal = int(c.get("nominal") or 0)
        if c.get("cassette_type") != "BILLCASSETTE" or not (MIN_REAL_NOMINAL <= nominal <= MAX_REAL_NOMINAL):
            continue
        if not is_uzs(c.get("currency")):
            continue
        out[str(nominal)] = out.get(str(nominal), 0) + int(c.get("count") or 0)
    return out


def service_state(snap: Dict[str, Any]) -> str:
    """offline / in_service / <причина вне обслуживания>. CustomerSession — тоже обслуживает
    (иначе каждый клиент давал бы событие)."""
    if (snap.get("agent_status") or "").lower() != "online":
        return "offline"
    if snap.get("service_status") == "InService" and snap.get("app_status") in ("InService", "CustomerSession"):
        return "in_service"
    return snap.get("app_status") or snap.get("service_status") or "unknown"


def _device_value(v: Any) -> Optional[str]:
    v = (v or "").upper() or None
    return "ONLINE" if v == "BUSY" else v   # BUSY — клиент у банкомата, не состояние устройства


def state_of(snap: Dict[str, Any], cassettes: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    st: Dict[str, Optional[str]] = {"agent": (snap.get("agent_status") or "").lower() or None,
                                    "service": service_state(snap)}
    for col in _DEVICE_COLS:
        st["device:" + col.removesuffix("_status")] = _device_value(snap.get(col))
    for c in cassettes:
        if c.get("cassette_index") is not None:
            st[f"cassette:{c['cassette_index']}"] = (c.get("status") or None)
    avail = snap.get("cdm_available_uzs")
    if avail is not None:
        st["cash"] = "empty" if avail <= 0 else "has_cash"
    return st


def diff_states(prev: Dict[str, Optional[str]], cur: Dict[str, Optional[str]]) -> List[Tuple[str, Optional[str], Optional[str]]]:
    out = []
    for key in sorted(set(prev) | set(cur)):
        if key not in cur:      # поле пропало из ответа — не считаем это изменением
            continue
        if key in prev and prev[key] != cur[key]:
            out.append((key, prev[key], cur[key]))
    return out


def detect_incassation(
    before: Dict[str, Any], cur: Dict[str, Any]
) -> Optional[str]:
    """Метод, которым найдена загрузка между снимками before → cur, или None.

    initial_amount — BTech сменил cdmInitialAmount и остаток вырос (или стоит у новой суммы);
    balance_jump   — остаток вырос не меньше чем на 20 млн и либо больше чем на 10 %,
                     либо между опросами не было клиентских внесений (время последнего
                     cash-in не менялось) — тогда рост не может быть депозитом."""
    b0, b1 = before.get("cdm_total_uzs"), cur.get("cdm_total_uzs")
    if b0 is None or b1 is None:
        return None
    i0, i1 = before.get("cdm_initial_uzs"), cur.get("cdm_initial_uzs")
    rise = b1 - b0
    if i1 and i0 is not None and abs(i1 - i0) >= 1 and (rise > 0 or b1 >= 0.9 * i1):
        return "initial_amount"
    no_deposits = before.get("last_transaction_cash_in") == cur.get("last_transaction_cash_in")
    if rise >= JUMP_MIN_UZS and (rise >= JUMP_MIN_SHARE * b0 or no_deposits):
        return "balance_jump"
    return None


# ── Обработка ────────────────────────────────────────────────

def _get_mark(conn, key: str) -> int:
    row = conn.execute("SELECT value FROM history_state WHERE key = %s", (key,)).fetchone()
    return int(row["value"]) if row and row["value"] is not None else 0


def _set_mark(conn, key: str, value: int) -> None:
    conn.execute(
        """INSERT INTO history_state (key, value, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (key, value),
    )


_SNAP_COLS = (
    "s.id, s.atm_id, s.polled_at, s.agent_status, s.service_status, s.app_status, "
    "s.cdm_total_uzs, s.cdm_available_uzs, s.cdm_initial_uzs, s.last_transaction_cash_in, "
    + ", ".join("s." + c for c in _DEVICE_COLS)
)


def _fill_derived(conn, lo: int, hi: int) -> None:
    """cdm_initial_uzs из сырого JSON и cdm_available_uzs из кассет — для снимков (lo, hi]."""
    conn.execute(
        """
        UPDATE atm_snapshots s
           SET cdm_initial_uzs = (s.raw_json -> 'cdmInitialAmount' ->> 'totalUzs')::double precision
         WHERE s.id > %s AND s.id <= %s AND s.cdm_initial_uzs IS NULL AND s.raw_json IS NOT NULL
           AND (s.raw_json -> 'cdmInitialAmount' ->> 'totalUzs') ~ '^-?[0-9]+([.][0-9]+)?$'
        """,
        (lo, hi),
    )
    conn.execute(
        """
        UPDATE atm_snapshots s
           SET cdm_available_uzs = COALESCE(c.available, s.cdm_total_uzs)
          FROM (
              SELECT s2.id,
                     (SELECT COALESCE(SUM(COALESCE(c.count, 0)::double precision * c.nominal)
                               FILTER (WHERE UPPER(COALESCE(c.status, '')) <> ALL(%s)), 0)
                        FROM cassette_snapshots c
                       WHERE c.atm_snapshot_id = s2.id AND c.cassette_type = 'BILLCASSETTE'
                         AND c.nominal BETWEEN %s AND %s
                         AND (c.currency IS NULL OR UPPER(c.currency) IN ('', 'UZS', '860'))
                      HAVING COUNT(*) > 0) AS available
                FROM atm_snapshots s2
               WHERE s2.id > %s AND s2.id <= %s AND s2.cdm_available_uzs IS NULL
          ) c
         WHERE s.id = c.id
        """,
        (list(BAD_CASSETTE_STATUSES), MIN_REAL_NOMINAL, MAX_REAL_NOMINAL, lo, hi),
    )


def _cassettes_for(conn, snapshot_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    out: Dict[int, List[Dict[str, Any]]] = {}
    if not snapshot_ids:
        return out
    for c in conn.execute(
        """SELECT atm_snapshot_id, cassette_index, cassette_type, status, count, currency, nominal
             FROM cassette_snapshots WHERE atm_snapshot_id = ANY(%s) ORDER BY cassette_index""",
        (snapshot_ids,),
    ).fetchall():
        out.setdefault(int(c["atm_snapshot_id"]), []).append(c)
    return out


def _previous_snapshots(conn, atm_ids: List[int], before_id: int) -> Dict[int, Dict[str, Any]]:
    """Последний уже обработанный снимок каждого ATM (база для сравнения с новыми)."""
    if not atm_ids or before_id <= 0:
        return {}
    rows = conn.execute(
        f"""
        SELECT DISTINCT ON (s.atm_id) {_SNAP_COLS}
          FROM atm_snapshots s
         WHERE s.atm_id = ANY(%s) AND s.id <= %s
         ORDER BY s.atm_id, s.polled_at DESC, s.id DESC
        """,
        (atm_ids, before_id),
    ).fetchall()
    return {int(r["atm_id"]): r for r in rows}


def _ts(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v))


def _process_batch(conn, lo: int, hi: int) -> Dict[str, int]:
    _fill_derived(conn, lo, hi)
    snaps = conn.execute(
        f"SELECT {_SNAP_COLS} FROM atm_snapshots s WHERE s.id > %s AND s.id <= %s ORDER BY s.atm_id, s.polled_at, s.id",
        (lo, hi),
    ).fetchall()
    if not snaps:
        return {"events": 0, "incassations": 0}
    atm_ids = sorted({int(s["atm_id"]) for s in snaps})
    prev_snap = _previous_snapshots(conn, atm_ids, lo)
    cass = _cassettes_for(conn, [int(s["id"]) for s in snaps] + [int(p["id"]) for p in prev_snap.values()])

    events: List[tuple] = []
    incs: List[tuple] = []
    pre_drop: Dict[int, Dict[str, Any]] = {}
    for s in snaps:
        atm_id = int(s["atm_id"])
        cur_state = state_of(s, cass.get(int(s["id"]), []))
        prev = prev_snap.get(atm_id)
        if prev is not None:
            prev_state = state_of(prev, cass.get(int(prev["id"]), []))
            for kind, old, new in diff_states(prev_state, cur_state):
                events.append((atm_id, s["polled_at"], kind, old, new, s["id"]))

            # Снятие кассет: остаток падает почти в ноль, «до» — снимок перед падением.
            b0, b1 = prev.get("cdm_total_uzs"), s.get("cdm_total_uzs")
            before = prev
            dropped = pre_drop.get(atm_id)
            if dropped is not None and _ts(s["polled_at"]) - _ts(dropped["polled_at"]) <= SWAP_WINDOW:
                before = dropped
            method = detect_incassation(before, s)
            if method:
                incs.append((
                    atm_id, s["polled_at"], s["id"], before["id"],
                    before.get("cdm_total_uzs"), before.get("cdm_available_uzs"),
                    s.get("cdm_total_uzs"), s.get("cdm_available_uzs"), s.get("cdm_initial_uzs"),
                    method, json.dumps(notes_by_nominal(cass.get(int(s["id"]), []))),
                ))
                pre_drop.pop(atm_id, None)
            elif b0 and b1 is not None and b1 <= 0.1 * b0:
                pre_drop[atm_id] = prev
            elif dropped is not None and _ts(s["polled_at"]) - _ts(dropped["polled_at"]) > SWAP_WINDOW:
                pre_drop.pop(atm_id, None)
        prev_snap[atm_id] = s

    with conn.cursor() as cur:
        if events:
            cur.executemany(
                "INSERT INTO atm_state_events (atm_id, at, kind, old_value, new_value, snapshot_id) VALUES (%s,%s,%s,%s,%s,%s)",
                events,
            )
        if incs:
            cur.executemany(
                """INSERT INTO atm_incassations (atm_id, at, snapshot_id, before_snapshot_id,
                       balance_before, available_before, balance_after, available_after, initial_uzs,
                       method, notes_after)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (snapshot_id) DO NOTHING""",
                incs,
            )
    _refresh_daily(conn, min(_ts(s["polled_at"]) for s in snaps), atm_ids)
    return {"events": len(events), "incassations": len(incs)}


_DAILY_SQL = """
WITH s AS (
    SELECT atm_id, polled_at, cdm_total_uzs AS bal,
           COALESCE(cdm_available_uzs, cdm_total_uzs) AS avail,
           LOWER(COALESCE(agent_status, '')) = 'online' AS online,
           LAG(cdm_total_uzs) OVER w AS prev_bal,
           LAG(COALESCE(cdm_available_uzs, cdm_total_uzs)) OVER w AS prev_avail,
           LAG(LOWER(COALESCE(agent_status, '')) = 'online') OVER w AS prev_online,
           LAG(polled_at) OVER w AS prev_at,
           LEAD(cdm_total_uzs) OVER w AS next_bal
      FROM atm_snapshots
     WHERE polled_at >= %(since)s AND atm_id = ANY(%(atms)s)
       AND (cdm_total_uzs IS NULL OR cdm_total_uzs <= %(max_bal)s)
    WINDOW w AS (PARTITION BY atm_id ORDER BY polled_at, id)
), p AS (
    SELECT *, EXTRACT(EPOCH FROM polled_at - prev_at) / 3600.0 AS gap_h,
           (polled_at AT TIME ZONE %(tz)s)::date AS day
      FROM s
)
SELECT atm_id, day,
       COALESCE(SUM(CASE WHEN gap_h <= %(max_gap)s AND prev_bal > bal
                          AND NOT (bal <= prev_bal * 0.1 AND COALESCE(next_bal, 0) >= prev_bal * 0.5)
                         THEN prev_bal - bal END), 0) AS withdrawn,
       COALESCE(SUM(gap_h) FILTER (WHERE gap_h <= %(max_gap)s), 0) AS covered_hours,
       AVG(bal) AS avg_balance, AVG(avail) AS avg_available, MIN(avail) AS min_available,
       COALESCE(SUM(gap_h) FILTER (WHERE gap_h <= %(max_gap)s AND prev_online AND prev_avail <= 0), 0) AS cashout_hours,
       COALESCE(SUM(gap_h) FILTER (WHERE gap_h <= %(max_gap)s AND NOT prev_online), 0) AS offline_hours,
       COUNT(*) AS samples
  FROM p
 WHERE day >= %(first_day)s
 GROUP BY atm_id, day
"""


def _refresh_daily(conn, since: datetime, atm_ids: List[int]) -> None:
    """Пересчитывает atm_daily с местной даты `since` (день целиком) для этих ATM."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.TIMEZONE)
    first_day = since.astimezone(tz).date()
    day_start = datetime.combine(first_day, datetime.min.time(), tz)
    rows = conn.execute(
        _DAILY_SQL,
        {"since": day_start - timedelta(hours=6), "atms": atm_ids, "tz": settings.TIMEZONE,
         "max_gap": 3, "max_bal": 3_000_000_000, "first_day": first_day},
    ).fetchall()
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO atm_daily (atm_id, day, withdrawn, covered_hours, avg_balance, avg_available,
                   min_available, cashout_hours, offline_hours, samples)
               VALUES (%(atm_id)s, %(day)s, %(withdrawn)s, %(covered_hours)s, %(avg_balance)s, %(avg_available)s,
                   %(min_available)s, %(cashout_hours)s, %(offline_hours)s, %(samples)s)
               ON CONFLICT (atm_id, day) DO UPDATE SET
                   withdrawn = EXCLUDED.withdrawn, covered_hours = EXCLUDED.covered_hours,
                   avg_balance = EXCLUDED.avg_balance, avg_available = EXCLUDED.avg_available,
                   min_available = EXCLUDED.min_available, cashout_hours = EXCLUDED.cashout_hours,
                   offline_hours = EXCLUDED.offline_hours, samples = EXCLUDED.samples""",
            rows,
        )


def _prune_raw_json(conn) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RAW_JSON_KEEP_DAYS)
    return conn.execute(
        "UPDATE atm_snapshots SET raw_json = NULL WHERE polled_at < %s AND raw_json IS NOT NULL", (cutoff,)
    ).rowcount


def process_history() -> Dict[str, int]:
    """Обрабатывает все новые снимки. Возвращает {snapshots, events, incassations}."""
    if not _lock.acquire(blocking=False):
        return {"snapshots": 0, "events": 0, "incassations": 0, "busy": 1}
    total = {"snapshots": 0, "events": 0, "incassations": 0}
    try:
        while True:
            with connect() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(7340101)")
                mark = _get_mark(conn, "snapshots")
                # Только снимки завершённых опросов: идущий опрос ещё не закоммичен и не виден.
                hi_row = conn.execute(
                    "SELECT MAX(id) AS id FROM (SELECT id FROM atm_snapshots WHERE id > %s ORDER BY id LIMIT %s) t",
                    (mark, BATCH),
                ).fetchone()
                hi = int(hi_row["id"]) if hi_row and hi_row["id"] else 0
                if hi <= mark:
                    break
                stats = _process_batch(conn, mark, hi)
                _set_mark(conn, "snapshots", hi)
                total["snapshots"] += 1
                total["events"] += stats["events"]
                total["incassations"] += stats["incassations"]
        with connect() as conn:
            day = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
            if _get_mark(conn, "raw_json_pruned") != day:
                pruned = _prune_raw_json(conn)
                _set_mark(conn, "raw_json_pruned", day)
                if pruned:
                    logger.info("Сырые JSON старше %d дн. обнулены: %d снимков", RAW_JSON_KEEP_DAYS, pruned)
        if total["events"] or total["incassations"]:
            logger.info("История обновлена: событий %d, инкассаций %d", total["events"], total["incassations"])
        return total
    finally:
        _lock.release()


def safe_process_history() -> None:
    """Для фоновых потоков: ошибки логируются, не пробрасываются."""
    try:
        process_history()
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обработать историю снимков")


# ── Справочные выборки ───────────────────────────────────────

def capacity_by_atm(conn, default: float) -> Dict[int, float]:
    """Сколько реально помещается/загружается в банкомат: максимум загрузки по BTech
    (cdmInitialAmount) или остатка сразу после инкассации за 180 дней; иначе default."""
    rows = conn.execute(
        """
        SELECT atm_id, MAX(v) AS cap FROM (
            SELECT atm_id, cdm_initial_uzs AS v FROM atm_snapshots
             WHERE cdm_initial_uzs > 0 AND cdm_initial_uzs <= 3e9 AND polled_at >= now() - interval '180 days'
            UNION ALL
            SELECT atm_id, balance_after FROM atm_incassations
             WHERE balance_after > 0 AND balance_after <= 3e9 AND at >= now() - interval '180 days'
        ) t GROUP BY atm_id
        """
    ).fetchall()
    out = {int(r["atm_id"]): float(r["cap"]) for r in rows if r["cap"]}
    return out
