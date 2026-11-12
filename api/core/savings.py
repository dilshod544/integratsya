"""Сколько денег система экономит банку — по реальным циклам инкассации.

Цикл — время между двумя фактическими загрузками банкомата (atm_incassations).
Для каждого завершённого цикла по снимкам BTech известно:

  * D — длина цикла, дней; W — реально снятые деньги; w = W / D — спрос;
  * средний доступный остаток в банкомате (сколько денег банк держал);
  * часы, когда банкомат был онлайн, но без денег (cash-out).

Политика банка (факт): 1 выезд за D дней, средний остаток — измеренный.

Политика системы: загрузка L из рекомендации, записанной ДО этой загрузки
(method = recorded). Для циклов до запуска системы — бэктест той же формулой,
но только по спросу за 14 дней ДО начала цикла (method = backtest), без
заглядывания в будущее. Затем L проверяется на РЕАЛЬНОМ спросе цикла: деньги
расходуются со скоростью w до страхового запаса S, то есть хватает на
T = (L − S) / w дней, средний остаток ≈ S + (L − S) / 2.

Затраты в день (классическая модель запаса):  K / T + r · средний остаток,
K — стоимость выезда, r — дневная стоимость денег. Пока банк не задал K и r,
считаются только натуральные величины: сколько денег можно освободить и
сколько выездов в месяц сэкономить — они не зависят от параметров.

«Реализовано» — циклы, начатые загрузкой по принятому плану системы:
их фактические затраты сравниваются с затратами банка на этом же банкомате
в циклах без плана (медиана), а если таких нет — с медианой сети."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from atm_monitor.db import connect as atm_connect
from atm_monitor.history import capacity_by_atm

from .economics import get_settings
from .incassation_plan import cycle_days as optimal_cycle

MIN_CYCLE_DAYS = 0.5
MIN_COVERAGE = 0.5        # меньше половины цикла под наблюдением — цикл не оцениваем
MAX_GAP_H = 3
BACKTEST_DAYS = 14


def evaluate_cycle(
    *,
    days: float,
    withdrawn: float,
    covered_hours: float,
    avg_available: float,
    cashout_hours: float,
    system_load: Optional[float],
    system_safety_days: float,
    econ: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """Сравнение одного цикла: факт банка vs загрузка системы на реальном спросе."""
    out: Dict[str, Any] = {"days": days, "valid": False}
    hours = days * 24.0
    coverage = covered_hours / hours if hours else 0.0
    out["coverage"] = round(min(1.0, coverage), 3)
    if days < MIN_CYCLE_DAYS or coverage < MIN_COVERAGE:
        out["skip_reason"] = "цикл короче 12 ч" if days < MIN_CYCLE_DAYS else "мало опросов за цикл"
        return out
    w = withdrawn * min(1.0 / coverage, 2.0) / days    # спрос/сутки, приведённый к полному циклу
    out["demand_per_day"] = w
    if w <= 0:
        out["skip_reason"] = "снятий не было"
        return out
    if not system_load:
        out["skip_reason"] = "нет рекомендации"
        return out
    safety = system_safety_days * w
    t_sys = (system_load - safety) / w
    if t_sys <= 0.1:
        out["skip_reason"] = "рекомендация меньше страхового запаса"
        return out
    avg_sys = safety + (system_load - safety) / 2.0

    k, pct, inc_pct = econ.get("trip_cost_uzs"), econ.get("cost_of_funds_pct"), econ.get("withdrawal_income_pct")
    r = pct / 100.0 / 365.0 if pct is not None else None
    out.update({
        "valid": True,
        "bank_avg_cash": avg_available,
        "system_avg_cash": avg_sys,
        "freed_cash": avg_available - avg_sys,            # сколько меньше денег держала бы система
        "bank_trips_per_30d": 30.0 / days,
        "system_trips_per_30d": 30.0 / t_sys,
        "system_cycle_days": t_sys,
        "cashout_hours": cashout_hours,
        "lost_withdrawals": cashout_hours * w / 24.0,
    })
    out["lost_income"] = out["lost_withdrawals"] * inc_pct / 100.0 if inc_pct is not None else None
    if k is not None and r is not None:
        bank_day = k / days + r * avg_available
        sys_day = k / t_sys + r * avg_sys
        out.update({"bank_cost": bank_day * days, "system_cost": sys_day * days,
                    "saving": (bank_day - sys_day) * days})
    else:
        out.update({"bank_cost": None, "system_cost": None, "saving": None})
    # Стоимость хранения денег отдельно — её можно посчитать, даже если не задана стоимость выезда.
    out["carrying_saving"] = (avg_available - avg_sys) * r * days if r is not None else None
    out["trip_saving"] = (30.0 / days - 30.0 / t_sys) * days / 30.0 * k if k is not None else None
    return out


_CYCLES_SQL = """
WITH inc AS (
    SELECT i.*, LEAD(i.at) OVER (PARTITION BY i.atm_id ORDER BY i.at, i.id) AS next_at,
           LEAD(i.available_before) OVER (PARTITION BY i.atm_id ORDER BY i.at, i.id) AS left_at_end,
           LEAD(i.before_snapshot_id) OVER (PARTITION BY i.atm_id ORDER BY i.at, i.id) AS next_before_id
      FROM atm_incassations i
     WHERE i.atm_id = ANY(%(atms)s) OR %(all)s
), cyc AS (
    -- Опросы цикла — до снимка «до» следующей загрузки: снятие кассет бригадой
    -- (остаток падает в ноль) — не снятия клиентов и не простой без денег.
    SELECT inc.*, COALESCE(b.polled_at, inc.next_at) AS cut_at
      FROM inc LEFT JOIN atm_snapshots b ON b.id = inc.next_before_id
     WHERE inc.next_at IS NOT NULL AND inc.next_at >= %(since)s
), s AS (
    SELECT atm_id, polled_at,
           COALESCE(cdm_available_uzs, cdm_total_uzs) AS avail, cdm_total_uzs AS bal,
           LOWER(COALESCE(agent_status, '')) = 'online' AS online,
           LAG(cdm_total_uzs) OVER w AS prev_bal,
           LAG(COALESCE(cdm_available_uzs, cdm_total_uzs)) OVER w AS prev_avail,
           LAG(LOWER(COALESCE(agent_status, '')) = 'online') OVER w AS prev_online,
           LAG(polled_at) OVER w AS prev_at
      FROM atm_snapshots
     WHERE polled_at >= %(snap_since)s AND cdm_total_uzs IS NOT NULL AND cdm_total_uzs <= 3e9
    WINDOW w AS (PARTITION BY atm_id ORDER BY polled_at, id)
)
SELECT c.id, c.atm_id, c.at, c.next_at, c.available_after, c.balance_after, c.left_at_end,
       c.recommendation_id, c.followed_plan,
       COALESCE(SUM(CASE WHEN EXTRACT(EPOCH FROM s.polled_at - s.prev_at) / 3600 <= %(gap)s AND s.prev_bal > s.bal
                    THEN s.prev_bal - s.bal END), 0) AS withdrawn,
       COALESCE(SUM(EXTRACT(EPOCH FROM s.polled_at - s.prev_at) / 3600)
                FILTER (WHERE EXTRACT(EPOCH FROM s.polled_at - s.prev_at) / 3600 <= %(gap)s), 0) AS covered_hours,
       SUM(s.prev_avail * EXTRACT(EPOCH FROM s.polled_at - s.prev_at))
           FILTER (WHERE EXTRACT(EPOCH FROM s.polled_at - s.prev_at) / 3600 <= %(gap)s)
         / NULLIF(SUM(EXTRACT(EPOCH FROM s.polled_at - s.prev_at))
           FILTER (WHERE EXTRACT(EPOCH FROM s.polled_at - s.prev_at) / 3600 <= %(gap)s), 0) AS avg_available,
       COALESCE(SUM(EXTRACT(EPOCH FROM s.polled_at - s.prev_at) / 3600)
                FILTER (WHERE EXTRACT(EPOCH FROM s.polled_at - s.prev_at) / 3600 <= %(gap)s
                          AND s.prev_online AND s.prev_avail <= 0), 0) AS cashout_hours
  FROM cyc c
  LEFT JOIN s ON s.atm_id = c.atm_id AND s.polled_at > c.at AND s.polled_at <= c.cut_at AND s.prev_at >= c.at
 GROUP BY c.id, c.atm_id, c.at, c.next_at, c.available_after, c.balance_after, c.left_at_end,
          c.recommendation_id, c.followed_plan
 ORDER BY c.at
"""


def _ts(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v))


def _backtest_demand(conn, atm_id: int, before: datetime) -> Optional[float]:
    row = conn.execute(
        """SELECT AVG(withdrawn * 24.0 / LEAST(covered_hours, 24)) AS per_day
             FROM atm_daily WHERE atm_id = %s AND covered_hours >= 12
              AND day < (%s::timestamptz)::date AND day >= (%s::timestamptz)::date - %s""",
        (atm_id, before, before, BACKTEST_DAYS),
    ).fetchone()
    return float(row["per_day"]) if row and row["per_day"] else None


def compute_cycles(days: int = 90, atm_id: Optional[int] = None) -> List[Dict[str, Any]]:
    econ = get_settings()["values"]
    safety_days = econ.get("safety_days") or 0.0
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with atm_connect() as conn:
        rows = conn.execute(_CYCLES_SQL, {
            "atms": [atm_id] if atm_id else [], "all": atm_id is None, "since": since,
            "snap_since": since - timedelta(days=60), "gap": MAX_GAP_H,
        }).fetchall()
        recs = {}
        rec_ids = [int(r["recommendation_id"]) for r in rows if r["recommendation_id"]]
        if rec_ids:
            recs = {int(r["id"]): r for r in conn.execute(
                "SELECT id, load_uzs, cycle_days, demand_per_day, created_at, status FROM recommendations WHERE id = ANY(%s)",
                (rec_ids,),
            ).fetchall()}
        tids = {int(r["id"]): r for r in conn.execute("SELECT id, tid, address FROM atms").fetchall()}
        caps = capacity_by_atm(conn, 0)

        out = []
        for r in rows:
            t0, t1 = _ts(r["at"]), _ts(r["next_at"])
            d = (t1 - t0).total_seconds() / 86400
            rec = recs.get(int(r["recommendation_id"] or 0))
            if rec and rec["load_uzs"]:
                method, load = "recorded", float(rec["load_uzs"])
            else:
                w_prev = _backtest_demand(conn, int(r["atm_id"]), t0)
                load, method = None, "backtest"
                if w_prev:
                    t, _ = optimal_cycle(w_prev, econ.get("trip_cost_uzs"), econ.get("cost_of_funds_pct"),
                                         econ.get("default_cycle_days") or 7, econ.get("min_cycle_days") or 7,
                                         econ.get("max_cycle_days") or 20)
                    load = w_prev * t + safety_days * w_prev
                    if caps.get(int(r["atm_id"])):
                        load = min(load, caps[int(r["atm_id"])])
            ev = evaluate_cycle(
                days=d, withdrawn=float(r["withdrawn"] or 0), covered_hours=float(r["covered_hours"] or 0),
                avg_available=float(r["avg_available"] if r["avg_available"] is not None else (r["available_after"] or 0)),
                cashout_hours=float(r["cashout_hours"] or 0), system_load=load,
                system_safety_days=safety_days, econ=econ,
            )
            a = tids.get(int(r["atm_id"])) or {}
            out.append({
                "incassation_id": r["id"], "atm_id": r["atm_id"], "tid": a.get("tid"), "address": a.get("address"),
                "start": t0.isoformat(), "end": t1.isoformat(), "method": method,
                "followed_plan": bool(r["followed_plan"]),
                "bank_loaded": r["available_after"], "left_at_end": r["left_at_end"],
                "system_load": load, "withdrawn": float(r["withdrawn"] or 0), **ev,
            })
    return out


def _median(xs: List[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def savings_summary(days: int = 90) -> Dict[str, Any]:
    settings = get_settings()
    econ = settings["values"]
    cycles = compute_cycles(days)
    valid = [c for c in cycles if c["valid"]]
    money = econ.get("trip_cost_uzs") is not None and econ.get("cost_of_funds_pct") is not None

    # Освобождаемые деньги — средневзвешенно по времени для каждого банкомата, затем сумма по сети.
    per_atm: Dict[int, Dict[str, Any]] = {}
    for c in valid:
        p = per_atm.setdefault(int(c["atm_id"]), {"atm_id": c["atm_id"], "tid": c["tid"], "address": c["address"],
                                                    "days": 0.0, "bank_cash_d": 0.0, "sys_cash_d": 0.0,
                                                    "bank_trips": 0, "sys_trips": 0.0, "cycles": 0,
                                                    "cashout_hours": 0.0, "lost_withdrawals": 0.0,
                                                    "saving": 0.0, "carrying_saving": 0.0})
        p["days"] += c["days"]
        p["cycles"] += 1
        p["bank_cash_d"] += c["bank_avg_cash"] * c["days"]
        p["sys_cash_d"] += c["system_avg_cash"] * c["days"]
        p["bank_trips"] += 1
        p["sys_trips"] += c["days"] / c["system_cycle_days"]
        p["cashout_hours"] += c["cashout_hours"]
        p["lost_withdrawals"] += c["lost_withdrawals"]
        p["saving"] += c["saving"] or 0.0
        p["carrying_saving"] += c["carrying_saving"] or 0.0
    atms = []
    for p in per_atm.values():
        p["bank_avg_cash"] = p.pop("bank_cash_d") / p["days"]
        p["system_avg_cash"] = p.pop("sys_cash_d") / p["days"]
        p["freed_cash"] = p["bank_avg_cash"] - p["system_avg_cash"]
        atms.append(p)
    atms.sort(key=lambda p: -(p["saving"] if money else p["freed_cash"]))

    total_days = sum(c["days"] for c in valid)
    inc_pct = econ.get("withdrawal_income_pct")
    lost_w = sum(c["lost_withdrawals"] for c in valid)
    potential = {
        "cycles": len(valid),
        "atms": len(per_atm),
        "atm_days": round(total_days, 1),
        "bank_avg_cash": sum(p["bank_avg_cash"] for p in atms),
        "system_avg_cash": sum(p["system_avg_cash"] for p in atms),
        "freed_cash": sum(p["freed_cash"] for p in atms),
        "bank_trips": sum(p["bank_trips"] for p in atms),
        "system_trips": round(sum(p["sys_trips"] for p in atms), 1),
        "cashout_hours": round(sum(c["cashout_hours"] for c in valid), 1),
        "lost_withdrawals": lost_w,
        "lost_income": lost_w * inc_pct / 100.0 if inc_pct is not None else None,
        "saving": sum(c["saving"] for c in valid) if money else None,
        "carrying_saving": sum(c["carrying_saving"] for c in valid) if econ.get("cost_of_funds_pct") is not None else None,
        "bank_cost": sum(c["bank_cost"] for c in valid) if money else None,
        "system_cost": sum(c["system_cost"] for c in valid) if money else None,
        "by_method": {m: sum(1 for c in valid if c["method"] == m) for m in ("recorded", "backtest")},
    }
    # «в месяц» по сети: экономия за наблюдаемые банкомато-дни, приведённая к 30 дням каждого банкомата
    if potential["saving"] is not None and total_days:
        potential["saving_per_month"] = potential["saving"] / total_days * 30 * len(per_atm)

    realized = _realized(valid, money)

    months: Dict[str, Dict[str, Any]] = {}
    for c in valid:
        key = c["end"][:7]
        m = months.setdefault(key, {"month": key, "cycles": 0, "saving": 0.0, "freed_cash_days": 0.0, "days": 0.0,
                                     "cashout_hours": 0.0, "bank_trips": 0, "system_trips": 0.0})
        m["cycles"] += 1
        m["saving"] += c["saving"] or 0.0
        m["freed_cash_days"] += c["freed_cash"] * c["days"]
        m["days"] += c["days"]
        m["cashout_hours"] += c["cashout_hours"]
        m["bank_trips"] += 1
        m["system_trips"] += c["days"] / c["system_cycle_days"]
    by_month = []
    for m in sorted(months.values(), key=lambda x: x["month"]):
        m["avg_freed_cash_per_atm"] = m.pop("freed_cash_days") / m["days"] if m["days"] else 0.0
        if not money:
            m["saving"] = None
        by_month.append(m)

    skipped: Dict[str, int] = {}
    for c in cycles:
        if not c["valid"]:
            skipped[c.get("skip_reason") or "—"] = skipped.get(c.get("skip_reason") or "—", 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": days,
        "settings": settings,
        "money_available": money,
        "potential": potential,
        "realized": realized,
        "by_month": by_month,
        "atms": atms[:500],
        "cycles_total": len(cycles),
        "skipped": skipped,
        "now": _current_cash_vs_plan(),
    }


def _realized(valid: List[Dict[str, Any]], money: bool) -> Dict[str, Any]:
    followed = [c for c in valid if c["followed_plan"]]
    if not followed:
        return {"cycles": 0, "saving": None, "freed_cash": None, "note": "Ещё нет инкассаций, выполненных по принятому плану."}

    def per_day(c):  # затраты банка в день: деньги, иначе — средний остаток
        return (c["bank_cost"] / c["days"]) if money else c["bank_avg_cash"]

    baseline_atm: Dict[int, List[float]] = {}
    baseline_all: List[float] = []
    for c in valid:
        if not c["followed_plan"]:
            baseline_atm.setdefault(int(c["atm_id"]), []).append(per_day(c))
            baseline_all.append(per_day(c))
    total = 0.0
    used = 0
    days = 0.0
    for c in followed:
        base = _median(baseline_atm.get(int(c["atm_id"]), [])) or _median(baseline_all)
        if base is None:
            continue
        total += (base - per_day(c)) * c["days"]
        days += c["days"]
        used += 1
    return {
        "cycles": len(followed),
        "compared": used,
        "saving": total if money else None,
        # без денежных параметров — на сколько меньше наличных в среднем держали банкоматы по плану
        "freed_cash": (total / days if days else None) if not money else None,
        "note": "Сравнение с медианой циклов этого же банкомата без плана (или медианой сети).",
    }


def _current_cash_vs_plan() -> Dict[str, Any]:
    """Сейчас в банкоматах vs средний остаток, который держала бы политика системы."""
    with atm_connect() as conn:
        row = conn.execute(
            """
            WITH last AS (
                SELECT DISTINCT ON (atm_id) atm_id, COALESCE(cdm_available_uzs, cdm_total_uzs) AS avail
                  FROM atm_snapshots WHERE polled_at >= now() - interval '6 hours' AND cdm_total_uzs <= 3e9
                 ORDER BY atm_id, polled_at DESC, id DESC
            ), rec AS (
                SELECT DISTINCT ON (atm_id) atm_id, load_uzs, demand_per_day, cycle_days
                  FROM recommendations WHERE status IN ('open', 'accepted')
                 ORDER BY atm_id, created_at DESC
            )
            SELECT COUNT(*) AS atms, SUM(l.avail) AS cash_now,
                   SUM(CASE WHEN r.load_uzs IS NOT NULL AND r.demand_per_day > 0
                            THEN r.load_uzs - r.demand_per_day * r.cycle_days / 2.0 END) AS plan_avg_cash,
                   COUNT(r.atm_id) FILTER (WHERE r.load_uzs IS NOT NULL) AS with_plan
              FROM last l LEFT JOIN rec r ON r.atm_id = l.atm_id
            """
        ).fetchone()
    return {"atms": row["atms"], "cash_now": row["cash_now"], "plan_avg_cash": row["plan_avg_cash"],
            "with_plan": row["with_plan"]}
