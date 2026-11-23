"""
Категории банкоматов A / B / C по спросу — снятиям наличных в месяц
(вкладка «Категории ABC» страницы /dashboard/analytics.html).

Источник — дневные отчёты BTech (вкладка «Статистика» банкомата, сбор
atm_monitor/encashments.py → daily_history):
  * btech_turnover_daily — выдано клиентам по номиналам за день;
  * btech_balance_daily  — остаток на конец дня; строка за день есть, даже если
    снятий не было, поэтому по ней считаем, за какие дни у банкомата есть данные.

Снятия в месяц = снято за период / дней с данными × 30. Так банкоматы с неполной
историей сравниваются честно; если данных меньше MIN_COVERAGE периода — помечаем как оценку.
Категория — только по UZS; USD показываем отдельно.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from atm_monitor.db import connect

from .atm_analytics import _cached, _registry_by_tid

MONTH_DAYS = 30
A_MIN = 900_000_000
B_MIN = 600_000_000
MIN_COVERAGE = 0.8

CATEGORIES = {
    "A": {"label": "A · высокий спрос", "rule": "≥ 900 млн в месяц"},
    "B": {"label": "B · средний спрос", "rule": "600–899 млн в месяц"},
    "C": {"label": "C · низкий спрос", "rule": "< 600 млн в месяц"},
}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def category(monthly: float) -> str:
    if monthly >= A_MIN:
        return "A"
    if monthly >= B_MIN:
        return "B"
    return "C"


def _day(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _daily(conn, since: date, until: date, atm_id: Optional[int] = None) -> Dict[int, Dict[date, Dict[str, float]]]:
    """{atm_id: {день: {"uzs": снято, "usd": снято}}}; дни без снятий, но с данными — нули."""
    where = "WHERE day BETWEEN %(since)s AND %(until)s" + (" AND atm_id = %(atm)s" if atm_id else "")
    params = {"since": since, "until": until, "atm": atm_id}
    out: Dict[int, Dict[date, Dict[str, float]]] = {}
    for r in conn.execute(f"SELECT DISTINCT atm_id, day FROM btech_balance_daily {where}", params).fetchall():
        out.setdefault(int(r["atm_id"]), {})[_day(r["day"])] = {"uzs": 0.0, "usd": 0.0}
    for r in conn.execute(
        f"""SELECT atm_id, day, UPPER(currency) AS cur, SUM(presented_count::numeric * nominal) AS amount
            FROM btech_turnover_daily {where} GROUP BY 1, 2, 3""",
        params,
    ).fetchall():
        cur = "usd" if r["cur"] == "USD" else "uzs" if r["cur"] in ("UZS", "") else None
        if cur:
            day = out.setdefault(int(r["atm_id"]), {}).setdefault(_day(r["day"]), {"uzs": 0.0, "usd": 0.0})
            day[cur] += float(r["amount"] or 0)
    return out


def _last_day(conn) -> Optional[date]:
    row = conn.execute("SELECT MAX(day) AS d FROM btech_turnover_daily").fetchone()
    return _day(row["d"]) if row and row["d"] else None


def _report(days: int) -> Dict[str, Any]:
    with connect() as conn:
        until = _last_day(conn)
        if until is None:
            return {"generated_at": datetime.now(timezone.utc).isoformat(), "from": None, "to": None,
                    "days": days, "categories": CATEGORIES, "atms": [], "summary": {}}
        since = until - timedelta(days=days - 1)
        daily = _daily(conn, since, until)
        meta = {int(r["id"]): r for r in conn.execute(
            "SELECT id, tid, serial, address, place, vendor_name, model_name FROM atms"
        ).fetchall()}

    registry = _registry_by_tid()
    calendar = [since + timedelta(days=i) for i in range(days)]
    atms = []
    for atm_id, per_day in daily.items():
        m = meta.get(atm_id)
        if not m or not per_day:
            continue
        covered = len(per_day)
        total = sum(v["uzs"] for v in per_day.values())
        total_usd = sum(v["usd"] for v in per_day.values())
        per_day_avg = total / covered
        monthly = per_day_avg * MONTH_DAYS
        reg = registry.get(str(m["tid"])) or {}
        atms.append({
            "atm_id": atm_id,
            "tid": m["tid"],
            "serial": m["serial"],
            "address": m["address"] or reg.get("registry_address"),
            "place": m["place"],
            "model": " ".join(x for x in (m["vendor_name"], m["model_name"]) if x) or None,
            "region": reg.get("region"),
            "branch": reg.get("branch"),
            "category": category(monthly),
            "monthly": monthly,
            "per_day": per_day_avg,
            "max_day": max(v["uzs"] for v in per_day.values()),
            "zero_days": sum(1 for v in per_day.values() if v["uzs"] <= 0),
            "total": total,
            "monthly_usd": total_usd / covered * MONTH_DAYS if total_usd else 0,
            "covered_days": covered,
            "estimate": covered < days * MIN_COVERAGE,
            # null — дня нет в отчётах BTech (разрыв на графике), 0 — данные есть, снятий нет
            "spark": [per_day[d]["uzs"] if d in per_day else None for d in calendar],
        })
    atms.sort(key=lambda a: -a["monthly"])

    total_monthly = sum(a["monthly"] for a in atms) or 1
    summary = {}
    for key in CATEGORIES:
        rows = [a for a in atms if a["category"] == key]
        summary[key] = {
            "count": len(rows),
            "monthly": sum(a["monthly"] for a in rows),
            "share_demand_pct": round(sum(a["monthly"] for a in rows) / total_monthly * 100, 1),
            "avg_monthly": sum(a["monthly"] for a in rows) / len(rows) if rows else 0,
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "from": since.isoformat(),
        "to": until.isoformat(),
        "days": days,
        "month_days": MONTH_DAYS,
        "thresholds": {"A": A_MIN, "B": B_MIN},
        "categories": CATEGORIES,
        "summary": summary,
        "atms": atms,
    }


def abc_report(days: int = 30) -> Dict[str, Any]:
    return _cached(f"abc:{days}", lambda: _report(days))


def abc_atm_detail(atm_id: int) -> Dict[str, Any]:
    """Суточный и месячный спрос одного банкомата за всю историю отчётов BTech."""
    with connect() as conn:
        until = _last_day(conn)
        first = conn.execute("SELECT MIN(day) AS d FROM btech_balance_daily WHERE atm_id = %s", (atm_id,)).fetchone()
        if until is None or not first or not first["d"]:
            return {"atm_id": atm_id, "daily": [], "months": [], "weekdays": [], "nominals": [], "stats": None}
        since = _day(first["d"])
        per_day = _daily(conn, since, until, atm_id).get(atm_id, {})
        last30 = until - timedelta(days=MONTH_DAYS - 1)
        nominals = conn.execute(
            """SELECT UPPER(currency) AS cur, nominal, SUM(presented_count) AS cnt,
                      SUM(presented_count::numeric * nominal) AS amount
               FROM btech_turnover_daily
               WHERE atm_id = %s AND day >= %s AND presented_count > 0
               GROUP BY 1, 2 ORDER BY 1 DESC, 2""",
            (atm_id, last30),
        ).fetchall()

    days = sorted(per_day)
    daily = [{"date": d.isoformat(), "uzs": per_day[d]["uzs"], "usd": per_day[d]["usd"],
              "weekday": WEEKDAYS[d.weekday()]} for d in days]

    months: Dict[str, Dict[str, Any]] = {}
    for d in days:
        m = months.setdefault(d.strftime("%Y-%m"), {"month": d.strftime("%Y-%m"), "uzs": 0.0, "usd": 0.0, "days": 0,
                                                    "calendar_days": _month_len(d)})
        m["uzs"] += per_day[d]["uzs"]
        m["usd"] += per_day[d]["usd"]
        m["days"] += 1
    for m in months.values():
        m["per_day"] = m["uzs"] / m["days"]
        m["monthly_30"] = m["per_day"] * MONTH_DAYS
        m["complete"] = m["days"] >= m["calendar_days"]
        m["category"] = category(m["monthly_30"])

    by_wd: Dict[int, List[float]] = {}
    for d in days:
        by_wd.setdefault(d.weekday(), []).append(per_day[d]["uzs"])
    weekdays = [{"weekday": WEEKDAYS[i], "avg": sum(v) / len(v), "days": len(v)}
                for i, v in sorted(by_wd.items())]

    recent = [per_day[d]["uzs"] for d in days if d >= last30]
    values = [per_day[d]["uzs"] for d in days]
    stats = None
    if values:
        best = max(days, key=lambda d: per_day[d]["uzs"])
        monthly = (sum(recent) / len(recent) * MONTH_DAYS) if recent else 0
        srt = sorted(values)
        stats = {
            "from": days[0].isoformat(),
            "to": days[-1].isoformat(),
            "covered_days": len(days),
            "last30_total": sum(recent),
            "last30_days": len(recent),
            "monthly": monthly,
            "category": category(monthly),
            "per_day": sum(recent) / len(recent) if recent else 0,
            "median_day": srt[len(srt) // 2],
            "max_day": per_day[best]["uzs"],
            "max_day_date": best.isoformat(),
            "zero_days": sum(1 for v in values if v <= 0),
            "avg_all": sum(values) / len(values),
        }
    return {
        "atm_id": atm_id,
        "daily": daily,
        "months": sorted(months.values(), key=lambda m: m["month"]),
        "weekdays": weekdays,
        "nominals": [{"currency": r["cur"] or "UZS", "nominal": int(r["nominal"]), "count": int(r["cnt"] or 0),
                      "amount": float(r["amount"] or 0)} for r in nominals],
        "stats": stats,
    }


def _month_len(d: date) -> int:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt - timedelta(days=1)).day
