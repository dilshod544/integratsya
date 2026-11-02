"""
Аналитика сети банкоматов для страницы /dashboard/analytics.html (блок atm_analytics).

Источник — последние снимки сборщика BTech (схема atm_monitor) и прогноз
снятий (atm_monitor/forecast.py). Регион, филиал и ёмкость берутся из реестра
банка, если банкомат с ним сопоставлен (те же правила, что у карты, см.
atm_live.match_live_to_rows); пороги заполненности — те же, что у карты.

Каждый ATM получает:
  * operational — working / busy (идёт сессия клиента) / out_of_service / offline / no_data;
  * cash        — empty (не заряжен) / critical / warning / ok / unknown /
                  anomaly (BTech отдаёт невозможный остаток — тестовое/неверно настроенное устройство);
  * faults      — неисправные устройства (диспенсер, картридер, PIN-пад...);
  * idle        — онлайн, но без выдачи наличных больше суток.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from atm_monitor import forecast
from atm_monitor.db import connect

from .config import DEFAULT_CAPACITY
from .db import _compute_status, list_atms

MAX_REAL_NOMINAL = 200_000        # крупнейшая купюра UZS
STALE_AFTER = timedelta(hours=3)   # снимок старше последнего опроса на столько — «нет данных»
IDLE_AFTER = timedelta(hours=24)

DEVICES = [
    ("dispenser_status", "Диспенсер (выдача)"),
    ("acceptor_status", "Купюроприёмник"),
    ("card_reader_status", "Картридер"),
    ("contactless_status", "Бесконтактный ридер"),
    ("epp_status", "PIN-пад"),
    ("printer_status", "Чековый принтер"),
    ("jprinter_status", "Журнальный принтер"),
    ("barcode_reader_status", "Сканер QR"),
]
# BUSY — устройство занято клиентом, NODEVICE — устройства нет в этой модели.
_DEVICE_OK = {None, "", "ONLINE", "BUSY", "NODEVICE"}

OUT_OF_SERVICE_REASONS = {
    "Supervisor": "Режим обслуживания",
    "NotRunning": "Приложение не запущено",
    "OutOfService": "Не обслуживает",
}

_CACHE_TTL_SEC = 60
_cache: Dict[str, Any] = {}


def _cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_SEC:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _ts(value) -> Optional[datetime]:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    return (ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")).to_pydatetime()


def _iso(value) -> Optional[str]:
    ts = _ts(value)
    return ts.isoformat() if ts else None


def _registry_by_tid() -> Dict[str, Dict[str, Any]]:
    out = {}
    for r in list_atms(limit=5000):
        if r.get("live_tid"):
            out[str(r["live_tid"])] = {
                "region": r.get("region"),
                "branch": r.get("branch"),
                "registry_terminal_id": r.get("terminal_id"),
                "registry_address": r.get("address"),
                "capacity": r.get("capacity") or DEFAULT_CAPACITY,
            }
    return out


def _latest_forecast_by_atm(conn) -> Dict[int, Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.* FROM atm_forecast_summary f
        WHERE f.run_id = (SELECT MAX(id) FROM forecast_runs WHERE status = 'success')
        """
    ).fetchall()
    return {int(r["atm_id"]): r for r in rows}


def classify(snap: Dict[str, Any], latest_poll: Optional[datetime], capacity: float) -> Dict[str, Any]:
    """Операционный статус, состояние наличных, неисправности и простой одного ATM."""
    polled_at = _ts(snap.get("polled_at"))
    if latest_poll and polled_at and polled_at < latest_poll - STALE_AFTER:
        operational = "no_data"
    elif (snap.get("agent_status") or "").lower() != "online":
        operational = "offline"
    elif snap.get("service_status") == "InService" and snap.get("app_status") in ("InService", "CustomerSession"):
        operational = "busy" if snap.get("app_status") == "CustomerSession" else "working"
    else:
        operational = "out_of_service"

    reason = None
    if operational == "out_of_service":
        app = snap.get("app_status") or snap.get("service_status")
        reason = OUT_OF_SERVICE_REASONS.get(app, app)

    balance = snap.get("cdm_total_uzs")
    if balance is None:
        cash = "unknown"
    elif balance > forecast.MAX_PLAUSIBLE_UZS:
        cash = "anomaly"
    elif balance <= 0:
        cash = "empty"
    else:
        cash = _compute_status(balance, capacity)

    faults = []
    for col, label in DEVICES:
        status = snap.get(col)
        broken = status not in _DEVICE_OK or (col == "dispenser_status" and status == "NODEVICE")
        if broken:
            faults.append({"device": col.removesuffix("_status"), "label": label, "status": status})

    last_out = _ts(snap.get("last_transaction_cash_out"))
    idle = (
        operational in ("working", "busy")
        and polled_at is not None
        and (last_out is None or polled_at - last_out > IDLE_AFTER)
    )
    dispenser_ok = not any(f["device"] == "dispenser" for f in faults)
    return {
        "operational": operational,
        "out_of_service_reason": reason,
        "cash": cash,
        "faults": faults,
        "idle": idle,
        "can_dispense": operational in ("working", "busy") and cash in ("ok", "warning", "critical") and dispenser_ok,
    }


def _network_status() -> Dict[str, Any]:
    with connect() as conn:
        snaps = conn.execute(
            """
            SELECT a.id AS atm_id, a.tid, a.serial, a.address, a.place, a.vendor_name, a.model_name,
                   s.id AS snapshot_id, s.polled_at, s.agent_status, s.agent_last_online,
                   s.service_status, s.app_status, s.cdm_total_uzs,
                   s.last_transaction_cash_out, s.last_transaction_cash_in, s.last_transaction_last,
                   s.dispenser_status, s.acceptor_status, s.card_reader_status, s.contactless_status,
                   s.epp_status, s.printer_status, s.jprinter_status, s.barcode_reader_status
            FROM atms a
            JOIN atm_snapshots s ON s.id = (
                SELECT id FROM atm_snapshots WHERE atm_id = a.id ORDER BY polled_at DESC, id DESC LIMIT 1
            )
            ORDER BY a.tid
            """
        ).fetchall()
        cassettes: Dict[int, List[Dict[str, Any]]] = {}
        for c in conn.execute(
            """
            SELECT c.atm_snapshot_id, c.cassette_index, c.cassette_type, c.status, c.count, c.nominal, c.currency
            FROM cassette_snapshots c
            WHERE c.atm_snapshot_id IN (
                SELECT DISTINCT ON (atm_id) id FROM atm_snapshots ORDER BY atm_id, polled_at DESC, id DESC
            )
            ORDER BY c.cassette_index
            """
        ).fetchall():
            cassettes.setdefault(int(c["atm_snapshot_id"]), []).append(c)
        fc = _latest_forecast_by_atm(conn)

    registry = _registry_by_tid()
    latest_poll = max((_ts(s["polled_at"]) for s in snaps), default=None)

    atms = []
    cassette_status: Dict[str, int] = {}
    reject_full = 0
    cash_by_nominal: Dict[int, float] = {}
    for s in snaps:
        reg = registry.get(str(s["tid"])) or {}
        capacity = reg.get("capacity") or DEFAULT_CAPACITY
        cls = classify(s, latest_poll, capacity)

        bills = []
        for c in cassettes.get(int(s["snapshot_id"]), []):
            if c["cassette_type"] == "BILLCASSETTE" and (c["nominal"] or 0) >= 1000:
                cassette_status[c["status"] or "—"] = cassette_status.get(c["status"] or "—", 0) + 1
                is_uzs = (c["currency"] or "UZS").upper() == "UZS"
                if c["count"] and is_uzs and c["nominal"] <= MAX_REAL_NOMINAL and cls["cash"] != "anomaly":
                    cash_by_nominal[int(c["nominal"])] = cash_by_nominal.get(int(c["nominal"]), 0) + c["count"] * c["nominal"]
                bills.append({"nominal": c["nominal"], "count": c["count"], "status": c["status"], "currency": c["currency"]})
            elif c["cassette_type"] == "REJECTCASSETTE" and c["status"] in ("FULL", "HIGH"):
                reject_full += 1

        f = fc.get(int(s["atm_id"]))
        balance = s.get("cdm_total_uzs")
        atms.append({
            "atm_id": s["atm_id"],
            "tid": s["tid"],
            "serial": s["serial"],
            "address": s.get("address") or reg.get("registry_address"),
            "place": s.get("place"),
            "model": " ".join(x for x in (s.get("vendor_name"), s.get("model_name")) if x) or None,
            "region": reg.get("region"),
            "branch": reg.get("branch"),
            "registry_terminal_id": reg.get("registry_terminal_id"),
            "balance": balance,
            "capacity": capacity,
            "balance_pct": round(balance / capacity * 100, 1) if balance is not None and capacity else None,
            "agent_status": s.get("agent_status"),
            "service_status": s.get("service_status"),
            "app_status": s.get("app_status"),
            "polled_at": _iso(s.get("polled_at")),
            "agent_last_online": _iso(s.get("agent_last_online")),
            "last_cash_out": _iso(s.get("last_transaction_cash_out")),
            "last_cash_in": _iso(s.get("last_transaction_cash_in")),
            "cassettes": bills,
            "forecast": {
                "method": f["method"],
                "withdrawal_24h": f["withdrawal_24h"],
                "low_cash_at": _iso(f["low_cash_at"]),
                "cashout_at": _iso(f["cashout_at"]),
                "refill_uzs": f["refill_uzs"],
            } if f else None,
            **cls,
        })

    def count(pred) -> int:
        return sum(1 for a in atms if pred(a))

    summary = {
        "total": len(atms),
        "working": count(lambda a: a["operational"] == "working"),
        "busy": count(lambda a: a["operational"] == "busy"),
        "out_of_service": count(lambda a: a["operational"] == "out_of_service"),
        "offline": count(lambda a: a["operational"] == "offline"),
        "no_data": count(lambda a: a["operational"] == "no_data"),
        "cash_empty": count(lambda a: a["cash"] == "empty"),
        "cash_critical": count(lambda a: a["cash"] == "critical"),
        "cash_warning": count(lambda a: a["cash"] == "warning"),
        "cash_ok": count(lambda a: a["cash"] == "ok"),
        "cash_unknown": count(lambda a: a["cash"] == "unknown"),
        "data_anomalies": count(lambda a: a["cash"] == "anomaly"),
        "with_faults": count(lambda a: a["faults"]),
        "dispenser_faults": count(lambda a: any(f["device"] == "dispenser" for f in a["faults"])),
        "idle_24h": count(lambda a: a["idle"]),
        "can_dispense": count(lambda a: a["can_dispense"]),
        "total_cash": sum(a["balance"] or 0 for a in atms if a["cash"] != "anomaly"),
        "reject_cassettes_full": reject_full,
        "matched_to_registry": count(lambda a: a["region"]),
    }

    regions: Dict[str, Dict[str, Any]] = {}
    for a in atms:
        r = regions.setdefault(a["region"] or "Не сопоставлен с реестром", {
            "region": a["region"] or "Не сопоставлен с реестром", "total": 0, "working": 0, "busy": 0,
            "out_of_service": 0, "offline": 0, "no_data": 0, "cash_empty": 0, "cash_low": 0,
            "with_faults": 0, "total_cash": 0.0,
        })
        r["total"] += 1
        r[a["operational"]] += 1
        r["cash_empty"] += a["cash"] == "empty"
        r["cash_low"] += a["cash"] in ("critical", "warning")
        r["with_faults"] += bool(a["faults"])
        r["total_cash"] += (a["balance"] or 0) if a["cash"] != "anomaly" else 0
    for r in regions.values():
        r["availability_pct"] = round((r["working"] + r["busy"]) / r["total"] * 100, 1) if r["total"] else 0

    faults_by_device = []
    for col, label in DEVICES:
        device = col.removesuffix("_status")
        hits = [f["status"] for a in atms for f in a["faults"] if f["device"] == device]
        if hits:
            by_status: Dict[str, int] = {}
            for st in hits:
                by_status[st or "—"] = by_status.get(st or "—", 0) + 1
            faults_by_device.append({"device": device, "label": label, "count": len(hits), "by_status": by_status})
    faults_by_device.sort(key=lambda x: -x["count"])

    out_reasons: Dict[str, int] = {}
    for a in atms:
        if a["out_of_service_reason"]:
            out_reasons[a["out_of_service_reason"]] = out_reasons.get(a["out_of_service_reason"], 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest_poll_at": latest_poll.isoformat() if latest_poll else None,
        "summary": summary,
        "by_region": sorted(regions.values(), key=lambda r: -r["total"]),
        "faults_by_device": faults_by_device,
        "out_of_service_reasons": [{"reason": k, "count": v} for k, v in sorted(out_reasons.items(), key=lambda x: -x[1])],
        "cassette_status": [{"status": k, "count": v} for k, v in sorted(cassette_status.items(), key=lambda x: -x[1])],
        "cash_by_nominal": [{"nominal": k, "amount": v} for k, v in sorted(cash_by_nominal.items())],
        "atms": atms,
    }


def network_status() -> Dict[str, Any]:
    return _cached("status", _network_status)


def _forecast_overview() -> Dict[str, Any]:
    with connect() as conn:
        last = conn.execute("SELECT * FROM forecast_runs ORDER BY id DESC LIMIT 1").fetchone()
        run = conn.execute(
            "SELECT * FROM forecast_runs WHERE status = 'success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not run:
            return {"run": None, "last_attempt": _run_payload(last), "atms": [], "daily": [], "summary": None}
        rows = conn.execute(
            """
            SELECT f.*, a.tid, a.address FROM atm_forecast_summary f JOIN atms a ON a.id = f.atm_id
            WHERE f.run_id = %s
            """,
            (run["id"],),
        ).fetchall()
        daily = conn.execute(
            """
            SELECT forecast_date, SUM(withdrawal_uzs) AS withdrawal, COUNT(*) AS atms
            FROM atm_forecasts WHERE run_id = %s GROUP BY 1 ORDER BY 1
            """,
            (run["id"],),
        ).fetchall()

    registry = _registry_by_tid()
    now = datetime.now(timezone.utc)
    atms = []
    for r in rows:
        reg = registry.get(str(r["tid"])) or {}
        low_at, cash_at = _ts(r["low_cash_at"]), _ts(r["cashout_at"])
        atms.append({
            "atm_id": r["atm_id"],
            "tid": r["tid"],
            "address": r.get("address") or reg.get("registry_address"),
            "region": reg.get("region"),
            "branch": reg.get("branch"),
            "method": r["method"],
            "history_days": r["history_days"],
            "balance": r["balance_uzs"],
            "withdrawal_24h": r["withdrawal_24h"],
            "withdrawal_horizon": r["withdrawal_horizon"],
            "low_cash_at": low_at.isoformat() if low_at else None,
            "cashout_at": cash_at.isoformat() if cash_at else None,
            "hours_to_low_cash": round((low_at - now).total_seconds() / 3600, 1) if low_at else None,
            "hours_to_cashout": round((cash_at - now).total_seconds() / 3600, 1) if cash_at else None,
            "refill_uzs": r["refill_uzs"],
        })
    far = float("inf")
    atms.sort(key=lambda a: (a["low_cash_at"] is None, a["hours_to_low_cash"] if a["hours_to_low_cash"] is not None else far))

    def within(field: str, hours: float) -> int:
        return sum(1 for a in atms if a[field] is not None and a[field] <= hours)

    methods: Dict[str, int] = {}
    for a in atms:
        methods[a["method"]] = methods.get(a["method"], 0) + 1

    return {
        "run": _run_payload(run),
        "last_attempt": _run_payload(last),
        "summary": {
            "atms": len(atms),
            "withdrawal_24h": sum(a["withdrawal_24h"] or 0 for a in atms),
            "withdrawal_horizon": sum(a["withdrawal_horizon"] or 0 for a in atms),
            "refill_total": sum(a["refill_uzs"] or 0 for a in atms),
            "low_cash_24h": within("hours_to_low_cash", 24),
            "low_cash_72h": within("hours_to_low_cash", 72),
            "cashout_24h": within("hours_to_cashout", 24),
            "cashout_horizon": sum(1 for a in atms if a["cashout_at"]),
            "methods": methods,
        },
        "daily": [
            {"date": str(d["forecast_date"])[:10], "withdrawal": d["withdrawal"], "atms": d["atms"]} for d in daily
        ],
        "atms": atms,
    }


def _run_payload(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not run:
        return None
    return {
        "id": run["id"],
        "status": run["status"],
        "model": run["model"],
        "started_at": _iso(run["started_at"]),
        "finished_at": _iso(run["finished_at"]),
        "horizon_days": run["horizon_days"],
        "train_rows": run["train_rows"],
        "atm_count": run["atm_count"],
        "metrics": run["metrics"] or {},
        "error": run["error_message"],
        "schedule": f"ежедневно в {forecast.settings.FORECAST_HOUR:02d}:00 ({forecast.settings.TIMEZONE})",
    }


def forecast_overview() -> Dict[str, Any]:
    return _cached("forecast", _forecast_overview)


def _withdrawals_history(days: int) -> Dict[str, Any]:
    with connect() as conn:
        daily = forecast.daily_withdrawals(conn, days)
        hourly = forecast.hourly_withdrawals(conn, days)
        tids = {int(r["id"]): r["tid"] for r in conn.execute("SELECT id, tid FROM atms").fetchall()}
    if daily.empty:
        return {"days": days, "daily": [], "hourly": [], "top_atms": [], "total": 0, "refills": 0}
    per_day = daily.groupby("day").agg(
        withdrawal=("withdrawn", "sum"), refills=("refills", "sum"), atms=("atm_id", "nunique")
    ).reset_index()
    per_atm = daily.groupby("atm_id").agg(
        withdrawal=("withdrawn", "sum"), refills=("refills", "sum"), days_seen=("day", "nunique")
    )
    per_atm = per_atm[per_atm["withdrawal"] > 0].sort_values("withdrawal", ascending=False)
    hours_seen = hourly.groupby("atm_id")["covered_hours"].sum() if not hourly.empty else pd.Series(dtype=float)
    registry = _registry_by_tid()
    atms = []
    for atm_id, r in per_atm.iterrows():
        tid = tids.get(int(atm_id))
        reg = registry.get(str(tid)) or {}
        atms.append({
            "tid": tid, "region": reg.get("region"), "branch": reg.get("branch"),
            "address": reg.get("registry_address"),
            "withdrawal": float(r["withdrawal"]), "refills": int(r["refills"]),
            "avg_per_day": float(r["withdrawal"]) / max(1, int(r["days_seen"])),
            "avg_per_hour": float(r["withdrawal"]) / max(1.0, float(hours_seen.get(atm_id, 0))),
        })
    top = atms[:15]
    return {
        "days": days,
        "total": float(per_day["withdrawal"].sum()),
        "refills": int(per_day["refills"].sum()),
        "daily": [
            {"date": d.strftime("%Y-%m-%d"), "withdrawal": float(w), "refills": int(rf), "atms": int(n)}
            for d, w, rf, n in per_day[["day", "withdrawal", "refills", "atms"]].itertuples(index=False)
        ],
        "hourly": [
            {"hour": h.strftime("%Y-%m-%dT%H:00"), "withdrawal": float(w), "refills": int(rf), "atms": int(n),
             "covered_hours": round(float(cov), 2)}
            for h, w, rf, n, cov in hourly.groupby("hour").agg(
                withdrawal=("withdrawn", "sum"), refills=("refills", "sum"), atms=("atm_id", "nunique"),
                covered=("covered_hours", "median"),
            ).reset_index()[["hour", "withdrawal", "refills", "atms", "covered"]].itertuples(index=False)
        ] if not hourly.empty else [],
        "top_atms": top,
        "atms": atms,
    }


def withdrawals_history(days: int) -> Dict[str, Any]:
    return _cached(f"withdrawals:{days}", lambda: _withdrawals_history(days))


def invalidate_cache() -> None:
    _cache.clear()


# ── Эффективность запаса наличных (вкладка «Аналитика ATM») ──
#
# Спрос — средние снятия в час за период (сумма падений остатка / часы, покрытые
# опросами). Норма запаса — NORM_DAYS дней спроса: всё, что лежит сверх неё,
# — лишние деньги, которые можно изъять при следующей инкассации.

NORM_DAYS = 30
LOW_COVER_DAYS = 3
MIN_COVERED_HOURS = 24   # меньше часов наблюдений — спрос не оцениваем


def assess_stock(balance: float, avg_per_hour: Optional[float], covered_hours: float, norm_days: int) -> Dict[str, Any]:
    """Хватит ли остатка и сколько в нём лишнего относительно нормы norm_days дней спроса.

    status: excess (запас больше нормы) / no_demand (денег много, снятий нет) /
    low (меньше LOW_COVER_DAYS дней) / ok / no_data (мало наблюдений)."""
    if covered_hours < MIN_COVERED_HOURS or avg_per_hour is None:
        return {"status": "no_data", "avg_per_day": None, "cover_days": None, "target": None, "excess": 0.0}
    per_day = avg_per_hour * 24.0
    target = per_day * norm_days
    if per_day <= 0:
        status = "no_demand" if balance > 0 else "ok"
        return {"status": status, "avg_per_day": 0.0, "cover_days": None, "target": 0.0, "excess": float(balance)}
    cover = balance / per_day
    excess = max(0.0, balance - target)
    status = "excess" if cover > norm_days else "low" if cover < LOW_COVER_DAYS else "ok"
    return {
        "status": status,
        "avg_per_day": per_day,
        "cover_days": round(cover, 1),
        "target": target,
        # изымать кратно миллиону, чтобы после изъятия запас не упал ниже нормы
        "excess": float(int(excess // 1_000_000) * 1_000_000),
    }


def _period_hourly(conn, days: int):
    """Почасовые снятия за последние `days` дней ИМЕЮЩИХСЯ данных (а не до «сейчас»):
    если сборщик стоял, период отсчитывается от последнего опроса."""
    row = conn.execute("SELECT MAX(polled_at) AS ts FROM atm_snapshots").fetchone()
    end = _ts(row["ts"]) if row and row["ts"] else None
    if end is None:
        return pd.DataFrame(), None
    lag_days = max(0, (datetime.now(timezone.utc) - end).days)
    hourly = forecast.hourly_withdrawals(conn, days + lag_days + 1)
    if not hourly.empty:
        start = pd.Timestamp(end.astimezone(forecast._tz()).replace(tzinfo=None)) - pd.Timedelta(days=days)
        hourly = hourly[hourly["hour"] >= start]
    return hourly, end


def _hour_profile(hourly: pd.DataFrame) -> List[Optional[float]]:
    """Средние снятия по часу суток 0..23 (сумма по банкоматам из hourly)."""
    if hourly.empty:
        return [None] * 24
    h = hourly.assign(hod=hourly["hour"].dt.hour)
    per_atm = h.groupby(["atm_id", "hod"]).agg(w=("withdrawn", "sum"), c=("covered_hours", "sum"))
    per_atm = per_atm[per_atm["c"] > 0]
    rate = (per_atm["w"] / per_atm["c"]).groupby(level="hod").sum()
    return [float(rate[i]) if i in rate.index else None for i in range(24)]


def _atm_efficiency(days: int, norm_days: int) -> Dict[str, Any]:
    with connect() as conn:
        hourly, end = _period_hourly(conn, days)
        latest = conn.execute(
            """
            SELECT a.id, a.tid, a.address, s.cdm_total_uzs AS balance, s.polled_at
            FROM atms a
            JOIN atm_snapshots s ON s.id = (
                SELECT id FROM atm_snapshots WHERE atm_id = a.id ORDER BY polled_at DESC, id DESC LIMIT 1
            )
            """
        ).fetchall()

    per_atm = (
        hourly.groupby("atm_id").agg(withdrawn=("withdrawn", "sum"), covered=("covered_hours", "sum"),
                                     refills=("refills", "sum"))
        if not hourly.empty else pd.DataFrame(columns=["withdrawn", "covered", "refills"])
    )
    # Снятия по дням для мини-графика в таблице; день без опросов — None (разрыв линии).
    spark: Dict[int, List[Optional[float]]] = {}
    if not hourly.empty:
        by_day = hourly.assign(day=hourly["hour"].dt.normalize()).groupby(["atm_id", "day"])["withdrawn"].sum()
        days_axis = pd.date_range(by_day.index.get_level_values("day").min(),
                                  by_day.index.get_level_values("day").max(), freq="D")
        for atm_id, series in by_day.groupby(level="atm_id"):
            s = series.droplevel("atm_id").reindex(days_axis)
            spark[int(atm_id)] = [None if pd.isna(v) else float(v) for v in s]
    registry = _registry_by_tid()
    atms = []
    for r in latest:
        balance = r["balance"]
        if balance is None or balance > forecast.MAX_PLAUSIBLE_UZS:
            continue
        balance = float(balance)
        stats = per_atm.loc[r["id"]] if r["id"] in per_atm.index else None
        covered = float(stats["covered"]) if stats is not None else 0.0
        avg_hour = float(stats["withdrawn"]) / covered if covered else None
        reg = registry.get(str(r["tid"])) or {}
        atms.append({
            "atm_id": int(r["id"]),
            "tid": r["tid"],
            "address": r.get("address") or reg.get("registry_address"),
            "region": reg.get("region"),
            "branch": reg.get("branch"),
            "balance": balance,
            "polled_at": _iso(r["polled_at"]),
            "withdrawn": float(stats["withdrawn"]) if stats is not None else 0.0,
            "covered_hours": round(covered, 1),
            "refills": int(stats["refills"]) if stats is not None else 0,
            "avg_per_hour": avg_hour,
            "daily_withdrawals": spark.get(int(r["id"]), []),
            **assess_stock(balance, avg_hour, covered, norm_days),
        })
    # Сначала — где больше всего лишних денег, затем — кому денег не хватит раньше всех.
    atms.sort(key=lambda a: (-a["excess"], a["cover_days"] if a["cover_days"] is not None else float("inf")))

    def count(status: str) -> int:
        return sum(1 for a in atms if a["status"] == status)

    covered_total = sum(a["covered_hours"] for a in atms)
    return {
        "days": days,
        "norm_days": norm_days,
        "low_cover_days": LOW_COVER_DAYS,
        "data_until": end.isoformat() if end else None,
        "summary": {
            "atms": len(atms),
            "balance": sum(a["balance"] for a in atms),
            "excess_total": sum(a["excess"] for a in atms),
            "excess": count("excess") + count("no_demand"),
            "low": count("low"),
            "ok": count("ok"),
            "no_data": count("no_data"),
            # средний спрос одного банкомата в час — по всем часам наблюдений сети
            "avg_per_hour_atm": sum(a["withdrawn"] for a in atms) / covered_total if covered_total else None,
            "avg_per_hour_network": sum(a["avg_per_hour"] or 0 for a in atms),
        },
        "hour_profile": _hour_profile(hourly),
        "atms": atms,
    }


def atm_efficiency(days: int, norm_days: int = NORM_DAYS) -> Dict[str, Any]:
    return _cached(f"efficiency:{days}:{norm_days}", lambda: _atm_efficiency(days, norm_days))


def _balance_history(atm_id: Optional[int], days: int) -> Dict[str, Any]:
    with connect() as conn:
        hourly, end = _period_hourly(conn, days)
        if end is None:
            return {"atm_id": atm_id, "days": days, "points": [], "hour_profile": [None] * 24}
        since = end - timedelta(days=days)
        rows = conn.execute(
            """
            SELECT atm_id, date_trunc('hour', polled_at AT TIME ZONE %(tz)s) AS hour,
                   AVG(cdm_total_uzs) AS balance
            FROM atm_snapshots
            WHERE polled_at >= %(since)s AND cdm_total_uzs IS NOT NULL AND cdm_total_uzs <= %(max_bal)s
              AND (%(atm_id)s::bigint IS NULL OR atm_id = %(atm_id)s::bigint)
            GROUP BY 1, 2
            """,
            {"tz": forecast.settings.TIMEZONE, "since": since, "max_bal": forecast.MAX_PLAUSIBLE_UZS, "atm_id": atm_id},
        ).fetchall()
    if atm_id is not None and not hourly.empty:
        hourly = hourly[hourly["atm_id"] == atm_id]
    if not rows:
        return {"atm_id": atm_id, "days": days, "points": [], "hour_profile": _hour_profile(hourly)}
    df = pd.DataFrame(rows, columns=["atm_id", "hour", "balance"])
    df["balance"] = df["balance"].astype(float)
    wide = df.pivot_table(index="hour", columns="atm_id", values="balance").sort_index()
    # Для суммы по сети пропущенный час банкомата берём из предыдущего опроса,
    # иначе каждый пропуск выглядит провалом остатка всей сети.
    wide = wide.reindex(pd.date_range(wide.index.min(), wide.index.max(), freq="h")).ffill(limit=forecast.MAX_GAP_HOURS)
    total = wide.sum(axis=1, min_count=1).dropna()
    return {
        "atm_id": atm_id,
        "days": days,
        "points": [{"t": h.strftime("%Y-%m-%dT%H:00"), "v": float(v)} for h, v in total.items()],
        "hour_profile": _hour_profile(hourly),
    }


def balance_history(atm_id: Optional[int], days: int) -> Dict[str, Any]:
    return _cached(f"balance:{atm_id}:{days}", lambda: _balance_history(atm_id, days))


def _atm_history(atm_id: int) -> Dict[str, Any]:
    """Вся история одного банкомата: почасовой остаток, снятия по дням и по часу суток."""
    with connect() as conn:
        row = conn.execute(
            "SELECT MIN(polled_at) AS first, MAX(polled_at) AS last FROM atm_snapshots WHERE atm_id = %s", (atm_id,)
        ).fetchone()
        first, last = (_ts(row["first"]), _ts(row["last"])) if row else (None, None)
        if first is None:
            return {"atm_id": atm_id, "from": None, "to": None, "points": [], "daily": [], "hour_profile": [None] * 24}
        days = (datetime.now(timezone.utc) - first).days + 1
        hourly, _ = _period_hourly(conn, days)
    hist = _balance_history(atm_id, days)
    daily = []
    if not hourly.empty:
        mine = hourly[hourly["atm_id"] == atm_id]
        per_day = mine.assign(day=mine["hour"].dt.normalize()).groupby("day").agg(
            withdrawal=("withdrawn", "sum"), refills=("refills", "sum"), covered=("covered_hours", "sum")
        )
        daily = [
            {"date": d.strftime("%Y-%m-%d"), "withdrawal": float(r["withdrawal"]), "refills": int(r["refills"]),
             "covered_hours": round(float(r["covered"]), 1)}
            for d, r in per_day.iterrows()
        ]
    return {
        "atm_id": atm_id,
        "from": first.isoformat(),
        "to": last.isoformat(),
        "points": hist["points"],
        "daily": daily,
        "hour_profile": hist["hour_profile"],
    }


def atm_history(atm_id: int) -> Dict[str, Any]:
    return _cached(f"atm-history:{atm_id}", lambda: _atm_history(atm_id))
