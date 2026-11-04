"""Рекомендации инкассации: сколько, когда и насколько срочно — по реальным данным BTech.

Для каждого банкомата по последнему снимку:

  * доступный остаток A — только исправные кассеты (atm_monitor/history.py);
  * спрос D (сум/сутки): прогноз XGBoost/запасной метод последнего прогона →
    фактические суточные снятия atm_daily за 14 дней → скорость BTech;
  * страховой запас S = safety_days × D, крайний срок due_at — когда A опустится до S;
  * цикл T: оптимальный (формула Уилсона) T* = √(2·K / (D·r)), где K — стоимость
    выезда, r — дневная стоимость денег; без этих параметров — default_cycle_days.
    Ограничен min/max_cycle_days;
  * загрузка L = min(ёмкость, D·T + S) — сколько должно быть в банкомате после
    инкассации, по номиналам в доле реальных выдач за 30 дней, пачками по 100 купюр;
  * срочность 1..10 — по часам до опустошения, с поправками на популярность
    банкомата и пиковые дни (выходные, дни зарплаты, праздники) до опустошения.

Каждая рекомендация пишется в журнал atm_monitor.recommendations и больше не
меняется (только статус) — с ней сравнивается фактическая загрузка
(api/core/savings.py). Новая строка появляется, только если план заметно
изменился, иначе журнал разрастался бы каждые 15 минут."""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from atm_monitor import forecast as atm_forecast
from atm_monitor.config import settings as atm_settings
from atm_monitor.db import connect as atm_connect
from atm_monitor.history import BAD_CASSETTE_STATUSES, MAX_REAL_NOMINAL, MIN_REAL_NOMINAL, capacity_by_atm, is_uzs

from .config import DEFAULT_CAPACITY
from .economics import get_settings

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=6)       # снимок старше — данных нет, рекомендацию не строим
FORECAST_FRESH = timedelta(hours=36)
NOTE_PACK = 100                        # купюры грузят пачками по 100
CHANGE_SHARE = 0.10                    # новая запись в журнал, если загрузка изменилась больше чем на 10 %
REPEAT_AFTER = timedelta(hours=24)     # …или прошло больше суток
ACCEPTED_TTL = timedelta(hours=48)     # принятый план держится до инкассации или 48 ч
_DEVICE_OK = {None, "", "ONLINE", "BUSY", "NODEVICE"}

_lock = threading.Lock()


# ── Чистые функции ───────────────────────────────────────────

def base_score(hours_to_empty: Optional[float]) -> int:
    if hours_to_empty is None:
        return 1
    for limit, score in ((0, 10), (6, 9), (12, 8), (24, 7), (36, 6), (48, 5), (72, 4), (120, 3), (168, 2)):
        if hours_to_empty <= limit:
            return score
    return 1


def peak_days(now: datetime, until: datetime) -> List[str]:
    """Выходные, дни зарплаты и праздники между now и until (не дальше 7 дней)."""
    out = []
    day = now.date()
    last = min(until, now + timedelta(days=7)).date()
    while day <= last:
        if (day.month, day.day) in atm_forecast.HOLIDAYS:
            out.append(f"{day:%d.%m} праздник")
        elif day.day in atm_forecast.SALARY_DAYS:
            out.append(f"{day:%d.%m} день зарплаты")
        elif day.weekday() >= 5:
            out.append(f"{day:%d.%m} выходной")
        day += timedelta(days=1)
    return out


def urgency(
    hours_to_empty: Optional[float],
    demand_rank: Optional[float],
    peaks: Sequence[str],
) -> Tuple[int, List[str]]:
    """Срочность 1..10 и объяснение, из чего она сложилась."""
    score = base_score(hours_to_empty)
    reasons = []
    if hours_to_empty is None:
        reasons.append("снятий не ожидается — денег хватит надолго")
    elif hours_to_empty <= 0:
        reasons.append("денег для выдачи уже нет")
    else:
        reasons.append(f"деньги закончатся примерно через {_fmt_hours(hours_to_empty)}")
    if score >= 3 and score < 10 and demand_rank is not None and demand_rank >= 0.8:
        score += 1
        reasons.append(f"популярный банкомат: снятия выше, чем у {round(demand_rank * 100)} % сети (+1)")
    if score >= 3 and score < 10 and peaks:
        score += 1
        reasons.append("до опустошения пиковые дни: " + ", ".join(peaks[:3]) + " (+1)")
    return min(10, score), reasons


def cycle_days(
    demand_per_day: float,
    trip_cost: Optional[float],
    cost_of_funds_pct: Optional[float],
    default_days: float,
    min_days: float,
    max_days: float,
) -> Tuple[float, str]:
    """Оптимальный цикл T* = √(2K / (D·r)): чаще ездить — дороже выезды, реже —
    больше денег лежит без дела. Без K и r — default_days."""
    if trip_cost and cost_of_funds_pct and demand_per_day > 0:
        r = cost_of_funds_pct / 100.0 / 365.0
        t = math.sqrt(2.0 * trip_cost / (demand_per_day * r))
        return max(min_days, min(max_days, t)), "optimal"
    return max(min_days, min(max_days, default_days)), "default"


def split_notes(load: float, shares: Dict[int, float], capacity: float) -> Dict[str, int]:
    """Загрузка по номиналам в пропорции реальных выдач, пачками по NOTE_PACK купюр."""
    if load <= 0 or not shares:
        return {}
    total = sum(shares.values()) or 1.0
    up = {n: math.ceil(load * s / total / n / NOTE_PACK) * NOTE_PACK for n, s in shares.items()}
    if sum(c * n for n, c in up.items()) <= capacity:
        return {str(n): int(c) for n, c in sorted(up.items()) if c}
    down = {n: math.floor(load * s / total / n / NOTE_PACK) * NOTE_PACK for n, s in shares.items()}
    return {str(n): int(c) for n, c in sorted(down.items()) if c}


def notes_value(notes: Dict[str, int]) -> float:
    return float(sum(int(n) * c for n, c in notes.items()))


def _fmt_hours(h: float) -> str:
    if h < 24:
        return f"{max(1, round(h))} ч"
    return f"{int(h // 24)} д {int(h % 24)} ч"


def build_recommendation(
    *,
    available: float,
    total: Optional[float],
    capacity: float,
    demand_per_day: Optional[float],
    demand_rank: Optional[float],
    shares: Dict[int, float],
    econ: Dict[str, Optional[float]],
    now: datetime,
) -> Dict[str, Any]:
    safety_days = econ.get("safety_days") or 0.0
    if not demand_per_day or demand_per_day <= 0:
        hours = 0.0 if available <= 0 else None
        score, reasons = urgency(hours, demand_rank, [])
        return {"hours_to_empty": hours, "due_at": now if available <= 0 else None, "cycle_days": None,
                "cycle_source": None, "load_uzs": None, "notes": {}, "amount_to_bring": None,
                "score": score, "reasons": reasons + ["нет данных о спросе — сумму загрузки не рассчитать"]}

    per_hour = demand_per_day / 24.0
    hours = max(0.0, available / per_hour)
    safety = safety_days * demand_per_day
    due_at = now + timedelta(hours=max(0.0, (available - safety) / per_hour))
    peaks = peak_days(now, now + timedelta(hours=hours))
    score, reasons = urgency(hours, demand_rank, peaks)

    t, source = cycle_days(
        demand_per_day, econ.get("trip_cost_uzs"), econ.get("cost_of_funds_pct"),
        econ.get("default_cycle_days") or 7.0, econ.get("min_cycle_days") or 1.0, econ.get("max_cycle_days") or 30.0,
    )
    want = demand_per_day * t + safety
    load = min(capacity, want)
    if want > capacity:
        reasons.append(f"ёмкость ограничивает загрузку: хватит на {load / demand_per_day:.1f} дн. вместо {t:.1f}")
    notes = split_notes(load, shares, capacity)
    load_rounded = notes_value(notes) or load
    if total is not None and total - available >= 1_000_000:
        reasons.append(f"{(total - available) / 1e6:,.0f} млн в неисправных кассетах — не выдаются".replace(",", " "))
    return {
        "hours_to_empty": round(hours, 1),
        "due_at": due_at,
        "cycle_days": round(t, 2),
        "cycle_source": source,
        "load_uzs": load_rounded,
        "notes": notes,
        "amount_to_bring": max(0.0, load_rounded - available),
        "score": score,
        "reasons": reasons,
    }


# ── Данные ───────────────────────────────────────────────────

def _latest_snapshots(conn) -> List[Dict[str, Any]]:
    return conn.execute(
        """
        SELECT DISTINCT ON (s.atm_id) s.id AS snapshot_id, s.atm_id, s.polled_at, s.agent_status,
               s.service_status, s.app_status, s.cdm_total_uzs, s.cdm_available_uzs, s.dispenser_status,
               a.tid, a.address, a.place, a.latitude, a.longitude, a.vendor_name, a.model_name
          FROM atm_snapshots s JOIN atms a ON a.id = s.atm_id
         WHERE s.polled_at >= now() - interval '2 days'
         ORDER BY s.atm_id, s.polled_at DESC, s.id DESC
        """
    ).fetchall()


def _forecast_demand(conn) -> Tuple[Optional[int], Dict[int, Tuple[float, str]]]:
    run = conn.execute(
        "SELECT id, started_at, horizon_days FROM forecast_runs WHERE status = 'success' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not run or not run["horizon_days"]:
        return None, {}
    started = run["started_at"]
    if isinstance(started, str):
        started = datetime.fromisoformat(started)
    if datetime.now(timezone.utc) - started > FORECAST_FRESH:
        return None, {}
    rows = conn.execute(
        "SELECT atm_id, method, withdrawal_horizon FROM atm_forecast_summary WHERE run_id = %s", (run["id"],)
    ).fetchall()
    out = {int(r["atm_id"]): (float(r["withdrawal_horizon"]) / float(run["horizon_days"]), r["method"])
           for r in rows if r["withdrawal_horizon"] is not None}
    return int(run["id"]), out


def _history_demand(conn) -> Dict[int, float]:
    rows = conn.execute(
        """SELECT atm_id, AVG(withdrawn * 24.0 / LEAST(covered_hours, 24)) AS per_day
             FROM atm_daily WHERE day >= current_date - 14 AND covered_hours >= 12
            GROUP BY atm_id"""
    ).fetchall()
    return {int(r["atm_id"]): float(r["per_day"]) for r in rows if r["per_day"] is not None}


def _btech_demand(conn) -> Dict[int, float]:
    rows = conn.execute(
        """SELECT DISTINCT ON (atm_id) atm_id, dispense_amount_rate FROM turnover_snapshots
            WHERE polled_at >= now() - interval '2 days' AND (currency IS NULL OR UPPER(currency) IN ('UZS', '860'))
            ORDER BY atm_id, polled_at DESC, id DESC"""
    ).fetchall()
    return {int(r["atm_id"]): float(r["dispense_amount_rate"]) * 24.0
            for r in rows if r["dispense_amount_rate"] and r["dispense_amount_rate"] > 0}


def _note_shares(conn) -> Dict[int, Dict[int, float]]:
    """atm → {номинал: сумма, реально выданная за 30 дней} по уменьшению купюр в кассетах."""
    rows = conn.execute(
        """
        WITH c AS (
            SELECT atm_id, cassette_index, nominal, currency, polled_at, count,
                   LAG(count) OVER (PARTITION BY atm_id, cassette_index ORDER BY polled_at, id) AS prev_count,
                   LAG(nominal) OVER (PARTITION BY atm_id, cassette_index ORDER BY polled_at, id) AS prev_nominal
              FROM cassette_snapshots
             WHERE polled_at >= now() - interval '30 days' AND cassette_type = 'BILLCASSETTE'
               AND nominal BETWEEN %s AND %s
        )
        SELECT atm_id, nominal, SUM((prev_count - count)::double precision * nominal) AS dispensed
          FROM c
         WHERE prev_count > count AND prev_nominal = nominal
           AND (currency IS NULL OR UPPER(currency) IN ('', 'UZS', '860'))
         GROUP BY atm_id, nominal
        """,
        (MIN_REAL_NOMINAL, MAX_REAL_NOMINAL),
    ).fetchall()
    out: Dict[int, Dict[int, float]] = {}
    for r in rows:
        out.setdefault(int(r["atm_id"]), {})[int(r["nominal"])] = float(r["dispensed"])
    return out


def _current_cassettes(conn, snapshot_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    out: Dict[int, List[Dict[str, Any]]] = {}
    if not snapshot_ids:
        return out
    for c in conn.execute(
        """SELECT atm_snapshot_id, cassette_index, cassette_type, status, count, nominal, currency
             FROM cassette_snapshots WHERE atm_snapshot_id = ANY(%s) ORDER BY cassette_index""",
        (snapshot_ids,),
    ).fetchall():
        out.setdefault(int(c["atm_snapshot_id"]), []).append(c)
    return out


def _shares_for(history_shares: Dict[int, float], cassettes: List[Dict[str, Any]]) -> Dict[int, float]:
    """Номиналы, которые стоят в банкомате; доли — по выдачам, иначе поровну по сумме."""
    nominals = sorted({
        int(c["nominal"]) for c in cassettes
        if c.get("cassette_type") == "BILLCASSETTE" and c.get("nominal")
        and MIN_REAL_NOMINAL <= int(c["nominal"]) <= MAX_REAL_NOMINAL and is_uzs(c.get("currency"))
        and (c.get("status") or "").upper() != "MISSING"
    }) or sorted(history_shares)
    if not nominals:
        return {}
    shares = {n: history_shares.get(n, 0.0) for n in nominals}
    if sum(shares.values()) <= 0:
        shares = {n: 1.0 for n in nominals}
    return shares


def _tech_issues(snap: Dict[str, Any], cassettes: List[Dict[str, Any]]) -> List[str]:
    issues = []
    if (snap.get("agent_status") or "").lower() != "online":
        issues.append("банкомат офлайн")
    elif not (snap.get("service_status") == "InService" and snap.get("app_status") in ("InService", "CustomerSession")):
        issues.append(f"не обслуживает клиентов ({snap.get('app_status') or snap.get('service_status')})")
    if (snap.get("dispenser_status") or "").upper() not in _DEVICE_OK:
        issues.append(f"диспенсер: {snap.get('dispenser_status')}")
    bad = [c for c in cassettes if c.get("cassette_type") == "BILLCASSETTE"
           and (c.get("status") or "").upper() in BAD_CASSETTE_STATUSES - {"MISSING"} and (c.get("count") or 0) > 0]
    if bad:
        issues.append("неисправные кассеты: " + ", ".join(f"№{c['cassette_index']}" for c in bad))
    return issues


def _rank(values: Dict[int, float]) -> Dict[int, float]:
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    return {k: (i / (n - 1) if n > 1 else 0.5) for i, (k, _) in enumerate(ordered)}


# ── Пересчёт и журнал ────────────────────────────────────────

def _link_incassations(conn) -> int:
    """Фактическая загрузка ↔ рекомендация, действовавшая на тот момент.
    Принятые до загрузки планы становятся done, остальные открытые — superseded."""
    incs = conn.execute(
        "SELECT id, atm_id, at FROM atm_incassations WHERE recommendation_id IS NULL ORDER BY at"
    ).fetchall()
    for inc in incs:
        rec = conn.execute(
            """SELECT id, status, accepted_at FROM recommendations
                WHERE atm_id = %s AND created_at <= %s AND created_at >= %s::timestamptz - interval '7 days'
                ORDER BY (status = 'accepted') DESC, created_at DESC LIMIT 1""",
            (inc["atm_id"], inc["at"], inc["at"]),
        ).fetchone()
        followed = bool(rec and rec["status"] == "accepted" and rec["accepted_at"] is not None)
        # 0 — «рекомендации не было» (загрузка до запуска системы), чтобы не искать её снова.
        conn.execute(
            "UPDATE atm_incassations SET recommendation_id = %s, followed_plan = %s WHERE id = %s",
            (rec["id"] if rec else 0, followed, inc["id"]),
        )
        conn.execute(
            """UPDATE recommendations SET status = CASE WHEN status = 'accepted' THEN 'done' ELSE 'superseded' END,
                      superseded_at = %s
                WHERE atm_id = %s AND created_at <= %s AND status IN ('open', 'accepted')""",
            (inc["at"], inc["atm_id"], inc["at"]),
        )
    return len(incs)


def refresh_plan(now: Optional[datetime] = None) -> Dict[str, int]:
    """Пересчитывает рекомендации по свежим снимкам и пишет изменившиеся в журнал."""
    if not _lock.acquire(blocking=False):
        return {"written": 0, "busy": 1}
    try:
        now = now or datetime.now(timezone.utc)
        econ = get_settings()["values"]
        written = 0
        with atm_connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7340102)")
            linked = _link_incassations(conn)
            snaps = _latest_snapshots(conn)
            if not snaps:
                return {"written": 0, "linked": linked}
            latest_poll = max(_as_dt(s["polled_at"]) for s in snaps)
            run_id, fc = _forecast_demand(conn)
            hist = _history_demand(conn)
            btech = _btech_demand(conn)
            shares_hist = _note_shares(conn)
            caps = capacity_by_atm(conn, 0)
            cass = _current_cassettes(conn, [int(s["snapshot_id"]) for s in snaps])
            last = {int(r["atm_id"]): r for r in conn.execute(
                """SELECT DISTINCT ON (atm_id) * FROM recommendations
                    WHERE status IN ('open', 'accepted') ORDER BY atm_id, created_at DESC, id DESC"""
            ).fetchall()}

            demand: Dict[int, Tuple[float, str]] = {}
            for s in snaps:
                a = int(s["atm_id"])
                if a in fc:
                    demand[a] = fc[a]
                elif a in hist:
                    demand[a] = (hist[a], "history")
                elif a in btech:
                    demand[a] = (btech[a], "btech")
            ranks = _rank({a: d for a, (d, _) in demand.items()})

            for s in snaps:
                a = int(s["atm_id"])
                if _as_dt(s["polled_at"]) < latest_poll - STALE_AFTER:
                    continue
                total = s["cdm_total_uzs"]
                available = s["cdm_available_uzs"] if s["cdm_available_uzs"] is not None else total
                if available is None or (total or 0) > atm_forecast.MAX_PLAUSIBLE_UZS:
                    continue
                cs = cass.get(int(s["snapshot_id"]), [])
                d, method = demand.get(a, (None, None))
                capacity = caps.get(a) or float(DEFAULT_CAPACITY)
                rec = build_recommendation(
                    available=float(available), total=total, capacity=capacity,
                    demand_per_day=d, demand_rank=ranks.get(a),
                    shares=_shares_for(shares_hist.get(a, {}), cs), econ=econ, now=now,
                )
                issues = _tech_issues(s, cs)
                if not caps.get(a):
                    rec["reasons"].append("ёмкость не известна по истории — взята по умолчанию")
                if method in ("btech", "mean_7d") or method is None:
                    rec["reasons"].append("прогноз спроса приблизительный (мало истории)" if d else "")
                rec["reasons"] = [r for r in rec["reasons"] if r]

                prev = last.get(a)
                if prev is not None and not _changed(prev, rec, now):
                    continue
                if prev is not None and prev["status"] == "accepted" and now - _as_dt(prev["accepted_at"]) < ACCEPTED_TTL:
                    continue   # принятый план не перебиваем, пока его не выполнили
                conn.execute(
                    """INSERT INTO recommendations (atm_id, created_at, snapshot_id, forecast_run_id, method,
                           available_uzs, capacity_uzs, demand_per_day, hours_to_empty, due_at, cycle_days,
                           load_uzs, notes, score, reasons, needs_technician)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (a, now, s["snapshot_id"], run_id if a in fc else None,
                     method, available, capacity, d, rec["hours_to_empty"], rec["due_at"], rec["cycle_days"],
                     rec["load_uzs"], json.dumps(rec["notes"]), rec["score"],
                     json.dumps(rec["reasons"] + issues, ensure_ascii=False), bool(issues)),
                )
                if prev is not None:
                    conn.execute(
                        "UPDATE recommendations SET status = 'superseded', superseded_at = %s WHERE id = %s",
                        (now, prev["id"]),
                    )
                written += 1
        return {"written": written, "linked": linked}
    finally:
        _lock.release()


def safe_refresh_plan() -> None:
    try:
        refresh_plan()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось пересчитать план инкассаций")


def _as_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v))


def _changed(prev: Dict[str, Any], rec: Dict[str, Any], now: datetime) -> bool:
    if now - _as_dt(prev["created_at"]) >= REPEAT_AFTER:
        return True
    if int(prev["score"]) != int(rec["score"]):
        return True
    a, b = prev.get("load_uzs"), rec.get("load_uzs")
    if (a is None) != (b is None):
        return True
    if a and b and abs(a - b) / max(a, b) > CHANGE_SHARE:
        return True
    return False


# ── Очередь для страницы ─────────────────────────────────────

def set_status(rec_id: int, status: str, user: Optional[str]) -> Optional[Dict[str, Any]]:
    """accepted — бригада берёт план в работу; open — отменить принятие."""
    if status not in ("accepted", "open"):
        raise ValueError("status должен быть accepted или open")
    with atm_connect() as conn:
        row = conn.execute(
            """UPDATE recommendations
                  SET status = %s,
                      accepted_at = CASE WHEN %s = 'accepted' THEN now() ELSE NULL END,
                      accepted_by = CASE WHEN %s = 'accepted' THEN %s ELSE NULL END
                WHERE id = %s AND status IN ('open', 'accepted')
            RETURNING id, status, accepted_at, accepted_by""",
            (status, status, status, user, rec_id),
        ).fetchone()
    return row


def queue(min_score: int = 1) -> Dict[str, Any]:
    """Текущие рекомендации (последняя открытая/принятая по каждому банкомату)."""
    with atm_connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (r.atm_id) r.*, a.tid, a.address, a.place, a.latitude, a.longitude,
                   a.vendor_name, a.model_name, s.polled_at, s.cdm_total_uzs AS balance_now,
                   COALESCE(s.cdm_available_uzs, s.cdm_total_uzs) AS available_now
              FROM recommendations r
              JOIN atms a ON a.id = r.atm_id
              LEFT JOIN atm_snapshots s ON s.id = r.snapshot_id
             WHERE r.status IN ('open', 'accepted')
             ORDER BY r.atm_id, r.created_at DESC, r.id DESC
            """
        ).fetchall()
    items = []
    for r in rows:
        if int(r["score"]) < min_score:
            continue
        load = r["load_uzs"]
        avail = r["available_uzs"]
        items.append({
            "id": r["id"], "atm_id": r["atm_id"], "tid": r["tid"],
            "address": r["address"], "place": r["place"],
            "lat": r["latitude"], "lon": r["longitude"],
            "model": " ".join(x for x in (r["vendor_name"], r["model_name"]) if x) or None,
            "created_at": r["created_at"], "polled_at": r["polled_at"],
            "score": int(r["score"]), "reasons": r["reasons"] or [],
            "needs_technician": bool(r["needs_technician"]),
            "status": r["status"], "accepted_at": r["accepted_at"], "accepted_by": r["accepted_by"],
            "method": r["method"], "available_uzs": avail, "balance_uzs": r["balance_now"],
            "capacity_uzs": r["capacity_uzs"], "demand_per_day": r["demand_per_day"],
            "hours_to_empty": r["hours_to_empty"], "due_at": r["due_at"], "cycle_days": r["cycle_days"],
            "load_uzs": load, "notes": r["notes"] or {},
            "amount_to_bring": max(0.0, float(load) - float(avail or 0)) if load is not None else None,
        })
    items.sort(key=lambda x: (-x["score"], x["hours_to_empty"] if x["hours_to_empty"] is not None else 1e9))
    return {"items": items, "count": len(items), "generated_at": datetime.now(timezone.utc).isoformat(),
            "timezone": atm_settings.TIMEZONE}


def local_tz() -> ZoneInfo:
    return ZoneInfo(atm_settings.TIMEZONE)


def queue_with_routes(
    min_score: int = 1,
    route_min_score: int = 5,
    max_stops: int = 12,
    roads: bool = False,
) -> Dict[str, Any]:
    """Очередь + рейсы: банкоматы со срочностью ≥ route_min_score (кроме тех, где нужен
    техник, а не инкассатор: офлайн / не обслуживает) → свой вилоят → ближайший филиал
    с «Инкассация = 1». Для каждого рейса — сколько везти и хватает ли наличных у филиала."""
    from .branch_balance import balances_by_local_code_map
    from .db import list_atms, list_branches_full
    from .incassation_router import build_regional_routes, canonical_region
    from .ledger_vash import apply_ledger_to_branches

    data = queue(min_score)
    registry = {str(r["live_tid"]): r for r in list_atms(limit=5000) if r.get("live_tid")}
    for it in data["items"]:
        reg = registry.get(str(it["tid"])) or {}
        it["region"] = reg.get("region") or canonical_region(it.get("address"))
        it["branch"] = reg.get("branch")
        it["registry_terminal_id"] = reg.get("terminal_id")
        if it.get("lat") is None and reg.get("lat") is not None:
            it["lat"], it["lon"] = reg.get("lat"), reg.get("lon")

    def blocked(it):  # инкассация не поможет, пока банкомат не починят
        return any(r.startswith(("банкомат офлайн", "не обслуживает", "диспенсер")) for r in it["reasons"])

    targets = [it for it in data["items"]
               if it["score"] >= route_min_score and it.get("lat") is not None and it.get("load_uzs") and not blocked(it)]
    by_tid = {str(it["tid"]): it for it in targets}
    pseudo = [{
        "terminal_id": str(it["tid"]), "atm_number": str(it["tid"]), "branch": it.get("branch") or "",
        "region": it.get("region"), "address": it.get("address") or "", "lat": it["lat"], "lon": it["lon"],
        "status": "critical" if it["score"] >= 8 else "warning", "capacity": it.get("capacity_uzs"),
        "balance": it.get("available_uzs"), "available_balance": it.get("available_uzs"),
    } for it in targets]

    branches = list_branches_full(incassation=1, limit=5000)
    bal_map = balances_by_local_code_map()
    for b in branches:
        code = str(b.get("local_code") or "")
        if code in bal_map:
            b["cash"] = dict(bal_map[code])
    apply_ledger_to_branches(branches)
    cash_by_code = {str(b.get("local_code")): (b.get("cash") or {}) for b in branches}

    routes = build_regional_routes(pseudo, branches, "all", max_stops=max_stops, snap_roads=roads) if pseudo else {
        "cars": [], "unserved_regions": [], "diagnostics": {}}
    trips = []
    for car in routes.get("cars") or []:
        stops = []
        for st in car.get("stops") or []:
            it = by_tid.get(str(st.get("terminal_id")))
            if not it:
                continue
            it["trip_label"] = car.get("label")
            stops.append({
                "rec_id": it["id"], "tid": it["tid"], "address": it.get("address"), "lat": st["lat"], "lon": st["lon"],
                "score": it["score"], "amount_to_bring": it.get("amount_to_bring"), "load_uzs": it.get("load_uzs"),
                "notes": it.get("notes"), "due_at": it.get("due_at"),
            })
        dep = car.get("departure_branch") or {}
        need = sum(float(s["amount_to_bring"] or 0) for s in stops)
        need_full = sum(float(s["load_uzs"] or 0) for s in stops)
        cash = cash_by_code.get(str(dep.get("local_code"))) or {}
        branch_cash = cash.get("balance_uzs")
        trips.append({
            "label": car.get("label"), "region": car.get("region"), "planned_date": car.get("planned_date"),
            "priority": car.get("priority"), "max_score": max((s["score"] for s in stops), default=0),
            "departure_branch": dep, "stops": stops, "geometry": car.get("geometry") or [],
            "distance_km": car.get("distance_km"), "est_time_min": car.get("est_time_min"),
            "routing_provider": car.get("routing_provider", "straight"),
            "amount_to_bring": need, "load_total": need_full,
            "branch_cash_uzs": branch_cash,
            "branch_cash_source": cash.get("balance_source") or ("kassa" if branch_cash is not None else None),
            "branch_cash_short": (branch_cash is not None and float(branch_cash) < need),
        })
    trips.sort(key=lambda t: (-t["max_score"], t["planned_date"] or "", t["label"] or ""))
    data["trips"] = trips
    data["unserved_regions"] = routes.get("unserved_regions") or []
    data["route_min_score"] = route_min_score
    data["blocked"] = [it["id"] for it in data["items"] if blocked(it)]
    return data
