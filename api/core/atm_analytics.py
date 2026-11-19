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
  * idle        — онлайн, но без выдачи наличных больше 30 дней.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from atm_monitor import forecast
from atm_monitor.db import connect

from .config import DEFAULT_CAPACITY
from .db import _compute_status, list_atms

log = logging.getLogger(__name__)

MAX_REAL_NOMINAL = 200_000        # крупнейшая купюра UZS
STALE_AFTER = timedelta(hours=3)   # снимок старше последнего опроса на столько — «нет данных»
IDLE_AFTER = timedelta(days=30)  # «Без снятий» — больше месяца

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
    available = snap.get("cdm_available_uzs")
    if available is None:
        available = balance
    if balance is None:
        cash = "unknown"
    elif balance > forecast.MAX_PLAUSIBLE_UZS:
        cash = "anomaly"
    elif available <= 0:
        cash = "empty"
    else:
        # По доступным деньгам: купюры в неисправной (INOP) кассете клиенту не выдать.
        cash = _compute_status(available, capacity)

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


def _last_observed_withdrawals(conn, since: datetime) -> Dict[int, datetime]:
    """Последнее наблюдаемое снятие: падение остатка между соседними опросами (≤3 ч).

    Поле BTech `last_transaction_cash_out` часто устаревает или NULL, хотя кассеты
    реально опустошаются — для «Без снятий > 30 дн» берём и этот сигнал.
    """
    rows = conn.execute(
        """
        WITH ordered AS (
            SELECT atm_id, polled_at, cdm_total_uzs,
                   LAG(cdm_total_uzs) OVER (PARTITION BY atm_id ORDER BY polled_at) AS prev,
                   LAG(polled_at) OVER (PARTITION BY atm_id ORDER BY polled_at) AS prev_t
            FROM atm_snapshots
            WHERE polled_at >= %(since)s
              AND cdm_total_uzs IS NOT NULL
              AND cdm_total_uzs <= %(max_bal)s
        )
        SELECT atm_id, MAX(polled_at) AS last_wd
        FROM ordered
        WHERE prev IS NOT NULL
          AND cdm_total_uzs < prev
          AND (prev - cdm_total_uzs) > 100000
          AND EXTRACT(EPOCH FROM (polled_at - prev_t)) / 3600.0 <= %(max_gap)s
        GROUP BY atm_id
        """,
        {
            "since": since,
            "max_bal": forecast.MAX_PLAUSIBLE_UZS,
            "max_gap": forecast.MAX_GAP_HOURS,
        },
    ).fetchall()
    out: Dict[int, datetime] = {}
    for r in rows:
        ts = _ts(r["last_wd"])
        if ts:
            out[int(r["atm_id"])] = ts
    return out


def _network_status() -> Dict[str, Any]:
    with connect() as conn:
        snaps = conn.execute(
            """
            SELECT a.id AS atm_id, a.tid, a.serial, a.address, a.place, a.vendor_name, a.model_name,
                   s.id AS snapshot_id, s.polled_at, s.agent_status, s.agent_last_online,
                   s.service_status, s.app_status, s.cdm_total_uzs, s.cdm_available_uzs,
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
        from atm_monitor.history import capacity_by_atm

        capacities = capacity_by_atm(conn, 0)
        # Окно чуть шире IDLE_AFTER, чтобы не пропустить снятие на границе 30 дней.
        observed_wd = _last_observed_withdrawals(
            conn, datetime.now(timezone.utc) - IDLE_AFTER - timedelta(days=5)
        )

    registry = _registry_by_tid()
    latest_poll = max((_ts(s["polled_at"]) for s in snaps), default=None)

    atms = []
    cassette_status: Dict[str, int] = {}
    reject_full = 0
    cash_by_nominal: Dict[int, float] = {}
    for s in snaps:
        reg = registry.get(str(s["tid"])) or {}
        capacity = reg.get("capacity") or capacities.get(int(s["atm_id"])) or DEFAULT_CAPACITY
        cls = classify(s, latest_poll, capacity)

        # Idle: BTech-поле + фактически наблюдаемые падения остатка.
        field_out = _ts(s.get("last_transaction_cash_out"))
        observed = observed_wd.get(int(s["atm_id"]))
        candidates = [t for t in (field_out, observed) if t is not None]
        effective_out = max(candidates) if candidates else None
        polled = _ts(s.get("polled_at"))
        if cls["operational"] in ("working", "busy") and polled is not None:
            cls["idle"] = effective_out is None or (polled - effective_out > IDLE_AFTER)
        else:
            cls["idle"] = False

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
            "available": s.get("cdm_available_uzs") if s.get("cdm_available_uzs") is not None else balance,
            "capacity": capacity,
            "balance_pct": round(balance / capacity * 100, 1) if balance is not None and capacity else None,
            "agent_status": s.get("agent_status"),
            "service_status": s.get("service_status"),
            "app_status": s.get("app_status"),
            "polled_at": _iso(s.get("polled_at")),
            "agent_last_online": _iso(s.get("agent_last_online")),
            "last_cash_out": _iso(effective_out),
            "last_cash_out_btech": _iso(field_out),
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
        "idle_30d": count(lambda a: a["idle"]),
        "idle_24h": count(lambda a: a["idle"]),  # alias = idle_30d (совместимость)
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

# Потолок остатка для «результата»: ATM с низким спросом и остатком выше этого
# уровня — кандидаты на изъятие до CAP (история «было X → стало 400 млн»).
CAP_UZS = 400_000_000


def estimate_period_demand(avg_per_hour: Optional[float], covered_hours: float, days: int) -> Optional[float]:
    """Ожидаемые снятия за `days` дней по среднему в час.

    None — наблюдений меньше MIN_COVERED_HOURS, спрос не оцениваем (иначе пара
    часов данных выглядела бы как «низкий спрос за месяц»)."""
    if avg_per_hour is None or covered_hours < MIN_COVERED_HOURS:
        return None
    return float(avg_per_hour) * 24.0 * days


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
    # Валютные ATM (USD-кассеты) — отдельный сегмент, не смешиваем с «свыше 400 млн» UZS.
    usd_by_tid: Dict[str, float] = {}
    try:
        from .atm_live import all_latest_cassettes

        for tid, cass in all_latest_cassettes().items():
            usd = [
                c for c in cass
                if str(c.get("currency") or "").strip().upper() == "USD"
            ]
            if usd:
                usd_by_tid[str(tid)] = float(sum((c.get("balance") or 0) for c in usd))
    except Exception:
        log.exception("efficiency: USD cassettes unavailable")

    atms = []
    for r in latest:
        balance = r["balance"]
        if balance is None or balance > forecast.MAX_PLAUSIBLE_UZS:
            continue
        balance = float(balance)
        stats = per_atm.loc[r["id"]] if r["id"] in per_atm.index else None
        covered = float(stats["covered"]) if stats is not None else 0.0
        withdrawn = float(stats["withdrawn"]) if stats is not None else 0.0
        avg_hour = withdrawn / covered if covered else None
        reg = registry.get(str(r["tid"])) or {}
        stock = assess_stock(balance, avg_hour, covered, norm_days)
        # Потолок 400 млн UZS: сколько можно изъять, чтобы осталось ровно CAP.
        over_cap = max(0.0, balance - CAP_UZS)
        over_cap = float(int(over_cap // 1_000_000) * 1_000_000)
        # Спрос за весь период — по среднему в час, а не по сумме наблюдённых часов:
        # если данных меньше `days` дней, сумма снятий занижена и ATM ложно
        # попадал бы в «низкий спрос».
        period_demand = estimate_period_demand(avg_hour, covered, days)
        low_demand = period_demand is not None and period_demand <= CAP_UZS
        high_demand = period_demand is not None and period_demand > CAP_UZS
        has_usd = str(r["tid"]) in usd_by_tid
        usd_balance = usd_by_tid.get(str(r["tid"]), 0.0) if has_usd else 0.0
        # Сегменты результата (взаимно читаемые, валютные отдельно):
        # over_cap — UZS-only, остаток > 400 млн, спрос за период ≤ 400 млн
        # high_demand — спрос за период > 400 млн
        # cap_ok — UZS-only, остаток ≤ 400 млн (в потолке)
        # currency — есть USD-кассета
        over_cap_candidate = (not has_usd) and over_cap > 0 and low_demand
        atms.append({
            "atm_id": int(r["id"]),
            "tid": r["tid"],
            "address": r.get("address") or reg.get("registry_address"),
            "region": reg.get("region"),
            "branch": reg.get("branch"),
            "balance": balance,
            "polled_at": _iso(r["polled_at"]),
            "withdrawn": withdrawn,
            "covered_hours": round(covered, 1),
            "refills": int(stats["refills"]) if stats is not None else 0,
            "avg_per_hour": avg_hour,
            "daily_withdrawals": spark.get(int(r["id"]), []),
            **stock,
            "cap_uzs": CAP_UZS,
            "over_cap": over_cap,
            "period_demand": period_demand,
            "low_demand": low_demand,
            "high_demand": high_demand,
            "has_usd": has_usd,
            "usd_balance": usd_balance,
            "over_cap_candidate": over_cap_candidate,
            "period_demand_observed": withdrawn,
            "segment": (
                "currency" if has_usd
                else "high_demand" if high_demand
                else "over_cap" if over_cap_candidate
                else "cap_ok" if balance <= CAP_UZS or low_demand
                else "no_data"
            ),
        })
    # Сначала — где больше всего лишних денег, затем — кому денег не хватит раньше всех.
    atms.sort(key=lambda a: (
        -a["over_cap"],
        -a["excess"],
        a["cover_days"] if a["cover_days"] is not None else float("inf"),
    ))

    def count(status: str) -> int:
        return sum(1 for a in atms if a["status"] == status)

    def seg(name: str) -> List[Dict[str, Any]]:
        return [a for a in atms if a["segment"] == name]

    over_cap_rows = seg("over_cap")
    high_rows = seg("high_demand")
    ok_rows = seg("cap_ok")
    cur_rows = seg("currency")
    cur_over = [a for a in cur_rows if a["over_cap"] > 0 and a["low_demand"]]
    covered_total = sum(a["covered_hours"] for a in atms)
    return {
        "days": days,
        "norm_days": norm_days,
        "low_cover_days": LOW_COVER_DAYS,
        "cap_uzs": CAP_UZS,
        "data_until": end.isoformat() if end else None,
        "summary": {
            "atms": len(atms),
            "balance": sum(a["balance"] for a in atms),
            "excess_total": sum(a["excess"] for a in atms),
            "excess": count("excess") + count("no_demand"),
            "low": count("low"),
            "ok": count("ok"),
            "no_data": count("no_data"),
            # Разделённые сегменты для отчёта «результат»
            "over_cap": len(over_cap_rows),
            "over_cap_total": sum(a["over_cap"] for a in over_cap_rows),
            "over_cap_balance": sum(a["balance"] for a in over_cap_rows),
            "high_demand": len(high_rows),
            "high_demand_total": sum(a["period_demand"] for a in high_rows),
            "no_data_demand": len(seg("no_data")),
            "cap_ok": len(ok_rows),
            "cap_ok_balance": sum(a["balance"] for a in ok_rows),
            "currency": len(cur_rows),
            "currency_over_cap": len(cur_over),
            "currency_over_cap_total": sum(a["over_cap"] for a in cur_over),
            "currency_usd_total": sum(a["usd_balance"] for a in cur_rows),
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


# ── Excel export (analytics «Состояние сети») ─────────────────

_OP_LABEL = {
    "working": "Работает",
    "busy": "Занят клиентом",
    "out_of_service": "Не работает",
    "offline": "Офлайн",
    "no_data": "Нет свежих данных",
}
_CASH_LABEL = {
    "empty": "Не заряжен",
    "critical": "Критично мало",
    "warning": "Мало",
    "ok": "Норма",
    "unknown": "Нет данных",
    "anomaly": "Ошибка данных BTech",
}
_DEVICE_STATUS_RU = {
    "HWERROR": "аппаратная ошибка",
    "POWEROFF": "выключен",
    "OFFLINE": "нет связи с устройством",
    "FRAUDATTEMPT": "попытка мошенничества",
    "USERERROR": "ошибка клиента",
    "NODEVICE": "отсутствует",
    "NOTREADY": "не готов",
}
_CASS_STATUS_RU = {
    "OK": "норма",
    "LOW": "мало купюр",
    "EMPTY": "пусто",
    "MISSING": "кассета не установлена",
    "INOP": "кассета неисправна",
    "NOREF": "состояние не определено",
}


def _dev_status_ru(status: Optional[str]) -> str:
    if not status:
        return "ошибка"
    return _DEVICE_STATUS_RU.get(status, status.lower())


def _problems_text(atm: Dict[str, Any]) -> str:
    """Тот же смысл, что колонка «Что не так» на странице аналитики — плоский текст для Excel."""
    lines: List[str] = []
    op = atm.get("operational")
    if op == "offline":
        lines.append("Нет связи")
    elif op == "no_data":
        lines.append("Нет свежих данных")
    elif op == "out_of_service":
        reason = (atm.get("out_of_service_reason") or "").lower()
        lines.append(f"Не работает: {reason}" if reason else "Не работает")
    cash = atm.get("cash")
    if cash == "empty":
        lines.append("Наличные закончились")
    elif cash == "critical":
        lines.append("Критично мало наличных")
    elif cash == "warning":
        lines.append("Мало наличных")
    elif cash == "anomaly":
        lines.append("BTech передаёт неверный остаток")
    for f in atm.get("faults") or []:
        lines.append(f"{f.get('label') or f.get('device')} — {_dev_status_ru(f.get('status'))}")
    if atm.get("idle"):
        lines.append("Нет снятий больше месяца")
    return "; ".join(lines) if lines else "всё в порядке"


def _cassettes_text(atm: Dict[str, Any]) -> str:
    parts = []
    for c in atm.get("cassettes") or []:
        st = _CASS_STATUS_RU.get(c.get("status") or "", c.get("status") or "—")
        nom = c.get("nominal")
        cnt = c.get("count")
        parts.append(f"{nom} × {cnt if cnt is not None else '—'} ({st})")
    return "; ".join(parts)


def _faults_text(atm: Dict[str, Any]) -> str:
    faults = atm.get("faults") or []
    if not faults:
        return ""
    return "; ".join(f"{f.get('label') or f.get('device')} — {_dev_status_ru(f.get('status'))}" for f in faults)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _autosize(ws, max_width: int = 48) -> None:
    from openpyxl.utils import get_column_letter

    for idx, col in enumerate(ws.columns, start=1):
        width = 0
        for cell in col:
            if cell.value is None:
                continue
            width = max(width, min(max_width, len(str(cell.value)) + 2))
        ws.column_dimensions[get_column_letter(idx)].width = max(10, width)


def export_atm_status_xlsx(
    atm_ids: List[int],
    *,
    region: Optional[str] = None,
    filter_label: Optional[str] = None,
    search: Optional[str] = None,
) -> bytes:
    """XLSX по выбранным ATM (порядок как на экране). Числа — числами, для фильтров Excel."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    if not atm_ids:
        raise ValueError("Нет банкоматов для выгрузки")
    if len(atm_ids) > 5000:
        raise ValueError("Слишком много банкоматов для одной выгрузки")

    status = network_status()
    by_id = {int(a["atm_id"]): a for a in status["atms"]}
    rows = [by_id[i] for i in atm_ids if i in by_id]
    if not rows:
        raise ValueError("Выбранные банкоматы не найдены в текущем опросе")

    # Последняя инкассация из BTech (цикл загрузки) — в network_status её нет.
    last_enc: Dict[int, Dict[str, Any]] = {}
    with connect() as conn:
        for r in conn.execute(
            """
            SELECT DISTINCT ON (atm_id)
                   atm_id, started_at, finished_at, loaded_uzs, unloaded_uzs
            FROM btech_encashments
            WHERE atm_id = ANY(%(ids)s)
            ORDER BY atm_id, started_at DESC
            """,
            {"ids": [int(a["atm_id"]) for a in rows]},
        ).fetchall():
            started = _ts(r["started_at"])
            finished = _ts(r["finished_at"])
            last_enc[int(r["atm_id"])] = {
                # openpyxl не принимает tz-aware datetime
                "started_at": started.replace(tzinfo=None) if started else None,
                "finished_at": finished.replace(tzinfo=None) if finished else None,
                "loaded_uzs": r["loaded_uzs"],
                "unloaded_uzs": r["unloaded_uzs"],
                "open": r["finished_at"] is None,
            }

    wb = Workbook()
    meta = wb.active
    meta.title = "Сводка"
    thin = Border(
        left=Side(style="thin", color="D0D7DE"),
        right=Side(style="thin", color="D0D7DE"),
        top=Side(style="thin", color="D0D7DE"),
        bottom=Side(style="thin", color="D0D7DE"),
    )
    head_fill = PatternFill("solid", fgColor="E8EEF7")
    head_font = Font(bold=True, color="1E293B")
    label_font = Font(bold=True, color="334155")

    poll_at = _parse_iso(status.get("latest_poll_at"))
    cash_total = sum((a.get("balance") or 0) for a in rows if a.get("cash") != "anomaly")
    meta_rows = [
        ("Отчёт", "Состояние сети банкоматов"),
        ("Сформирован", datetime.now().strftime("%d.%m.%Y %H:%M")),
        ("Последний опрос BTech", poll_at.strftime("%d.%m.%Y %H:%M") if poll_at else "—"),
        ("Регион", region or "Все регионы"),
        ("Фильтр", filter_label or "Все банкоматы"),
        ("Поиск", search or "—"),
        ("Банкоматов в файле", len(rows)),
        ("Из них работают", sum(1 for a in rows if a.get("operational") == "working")),
        ("Заняты клиентом", sum(1 for a in rows if a.get("operational") == "busy")),
        ("Не работают", sum(1 for a in rows if a.get("operational") == "out_of_service")),
        ("Офлайн / нет данных", sum(1 for a in rows if a.get("operational") in ("offline", "no_data"))),
        ("Не заряжены", sum(1 for a in rows if a.get("cash") == "empty")),
        ("Мало наличных", sum(1 for a in rows if a.get("cash") in ("critical", "warning"))),
        ("С неисправностями", sum(1 for a in rows if a.get("faults"))),
        ("Сумма наличных UZS", cash_total),
    ]
    meta.append(["Параметр", "Значение"])
    for cell in meta[1]:
        cell.fill = head_fill
        cell.font = head_font
        cell.border = thin
    for label, value in meta_rows:
        meta.append([label, value])
        meta.cell(meta.max_row, 1).font = label_font
        for col in (1, 2):
            meta.cell(meta.max_row, col).border = thin
        if label == "Сумма наличных UZS":
            meta.cell(meta.max_row, 2).number_format = "#,##0"
    _autosize(meta)

    ws = wb.create_sheet("Банкоматы", 0)
    headers = [
        "TID", "Регион", "Филиал", "Адрес", "Место", "Модель", "Серийный номер",
        "Состояние", "Выдаёт наличные", "Что не так",
        "Наличные UZS", "% ёмкости", "Статус наличных", "Доступно UZS", "Ёмкость UZS",
        "Кассеты", "Неисправности",
        "Последний раз на связи", "Последнее снятие", "Последняя инкассация",
        "Загружено UZS (инкассация)", "Выгружено UZS (инкассация)", "Цикл инкассации",
        "Опрос",
        "Снимают за сутки (прогноз)", "Мало наличных к", "Опустеет к", "Рекомендуемая загрузка UZS",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = head_fill
        cell.font = head_font
        cell.border = thin
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{ws.cell(1, len(headers)).coordinate}"

    for a in rows:
        fc = a.get("forecast") or {}
        enc = last_enc.get(int(a["atm_id"])) or {}
        if enc:
            cycle = "открыт" if enc.get("open") else "закрыт"
        else:
            cycle = "нет данных"
        ws.append([
            a.get("tid"),
            a.get("region") or "Не сопоставлен с реестром",
            a.get("branch") or "",
            a.get("address") or "",
            a.get("place") or "",
            a.get("model") or "",
            a.get("serial") or "",
            _OP_LABEL.get(a.get("operational"), a.get("operational") or ""),
            "да" if a.get("can_dispense") else "нет",
            _problems_text(a),
            a.get("balance"),
            a.get("balance_pct"),
            _CASH_LABEL.get(a.get("cash"), a.get("cash") or ""),
            a.get("available"),
            a.get("capacity"),
            _cassettes_text(a),
            _faults_text(a),
            _parse_iso(a.get("agent_last_online")),
            _parse_iso(a.get("last_cash_out")),
            enc.get("started_at"),
            enc.get("loaded_uzs"),
            enc.get("unloaded_uzs"),
            cycle,
            _parse_iso(a.get("polled_at")),
            fc.get("withdrawal_24h"),
            _parse_iso(fc.get("low_cash_at")),
            _parse_iso(fc.get("cashout_at")),
            fc.get("refill_uzs"),
        ])
        r = ws.max_row
        for col in range(1, len(headers) + 1):
            cell = ws.cell(r, col)
            cell.border = thin
            cell.alignment = Alignment(vertical="top", wrap_text=col in (4, 10, 16, 17))
        for col in (11, 13, 14, 15, 21, 22, 25, 28):
            ws.cell(r, col).number_format = "#,##0"
        ws.cell(r, 12).number_format = "0.0"
        for col in (18, 19, 20, 24, 26, 27):
            ws.cell(r, col).number_format = "DD.MM.YYYY HH:MM"

    _autosize(ws)
    ws.row_dimensions[1].height = 30

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_atm_efficiency_xlsx(
    atm_ids: List[int],
    *,
    days: int = 30,
    norm_days: int = NORM_DAYS,
    filter_label: Optional[str] = None,
) -> bytes:
    """Excel по выборке «Аналитика ATM»: каждый сегмент — отдельный лист."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    if not atm_ids:
        raise ValueError("Нет банкоматов для выгрузки")
    if len(atm_ids) > 5000:
        raise ValueError("Слишком много банкоматов для одной выгрузки")

    data = atm_efficiency(days, norm_days)
    by_id = {int(a["atm_id"]): a for a in data["atms"]}
    rows = [by_id[i] for i in atm_ids if i in by_id]
    if not rows:
        raise ValueError("Выбранные банкоматы не найдены в аналитике")

    wb = Workbook()
    thin = Border(
        left=Side(style="thin", color="D0D7DE"),
        right=Side(style="thin", color="D0D7DE"),
        top=Side(style="thin", color="D0D7DE"),
        bottom=Side(style="thin", color="D0D7DE"),
    )
    head_fill = PatternFill("solid", fgColor="E8EEF7")
    head_font = Font(bold=True, color="1E293B")
    label_font = Font(bold=True, color="334155")
    cap = float(data.get("cap_uzs") or CAP_UZS)

    # Порядок листов: сегменты отдельно, без смешивания в одной таблице.
    seg_sheets = [
        ("over_cap", "1. Свыше 400 млн", "Свыше 400 млн · низкий спрос (UZS)"),
        ("high_demand", "2. Спрос выше 400", "Спрос > 400 млн за период"),
        ("cap_ok", "3. До 400 млн", "До 400 млн · норма потолка (UZS)"),
        ("currency", "4. Валютные USD", "Валютные ATM (USD)"),
        ("no_data", "5. Мало данных", "Свыше 400 млн · спрос не оценён (мало данных)"),
    ]
    by_seg: Dict[str, List[Dict[str, Any]]] = {k: [] for k, _, _ in seg_sheets}
    for a in rows:
        by_seg.setdefault(a.get("segment") or "cap_ok", []).append(a)

    meta = wb.active
    meta.title = "0. Сводка"
    meta.append(["Параметр", "Значение"])
    for cell in meta[1]:
        cell.fill = head_fill
        cell.font = head_font
        cell.border = thin
    meta_rows = [
        ("Отчёт", "Аналитика ATM · сегменты разделены по листам"),
        ("Сформирован", datetime.now().strftime("%d.%m.%Y %H:%M")),
        ("Период спроса, дней", data.get("days")),
        ("Норма запаса, дней", data.get("norm_days")),
        ("Потолок остатка UZS", cap),
        ("Фильтр UI", filter_label or "Текущая выборка"),
        ("Всего банкоматов в файле", len(rows)),
    ]
    for key, sheet_title, title in seg_sheets:
        part = by_seg.get(key) or []
        meta_rows.append((f"{title} · шт", len(part)))
        meta_rows.append((f"{title} · было UZS", sum(a.get("balance") or 0 for a in part)))
        meta_rows.append((f"{title} · изъять до {int(cap/1e6)} млн", sum(a.get("over_cap") or 0 for a in part)))
        if key == "currency":
            meta_rows.append((f"{title} · USD остаток", sum(a.get("usd_balance") or 0 for a in part)))
    for label, value in meta_rows:
        meta.append([label, value])
        meta.cell(meta.max_row, 1).font = label_font
        for col in (1, 2):
            meta.cell(meta.max_row, col).border = thin
        if isinstance(value, (int, float)) and not isinstance(value, bool) and "шт" not in label and "дней" not in label:
            meta.cell(meta.max_row, 2).number_format = "#,##0"
    _autosize(meta)

    headers = [
        "TID", "Регион", "Филиал", "Адрес",
        "Было (остаток UZS)", "Цель UZS", "Можно изъять до цели",
        "Спрос за период (прогноз) UZS", "Спрос в день UZS", "Хватит на, дн",
        "USD кассета", "USD остаток", "Статус запаса", "Опрос",
    ]

    def _write_seg_sheet(title: str, part: List[Dict[str, Any]]) -> None:
        ws = wb.create_sheet(title[:31])
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.border = thin
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.freeze_panes = "B2"
        if not part:
            ws.append(["—", "Нет банкоматов в этом сегменте"] + [""] * (len(headers) - 2))
            _autosize(ws)
            return
        part_sorted = sorted(part, key=lambda a: (-(a.get("over_cap") or 0), -(a.get("balance") or 0)))
        for a in part_sorted:
            ws.append([
                a.get("tid"),
                a.get("region") or "",
                a.get("branch") or "",
                a.get("address") or "",
                a.get("balance"),
                a.get("cap_uzs") or cap,
                a.get("over_cap") or 0,
                a.get("period_demand"),
                a.get("avg_per_day"),
                a.get("cover_days"),
                "да" if a.get("has_usd") else "нет",
                a.get("usd_balance") or 0,
                a.get("status"),
                _parse_iso(a.get("polled_at")),
            ])
            r = ws.max_row
            for col in range(1, len(headers) + 1):
                cell = ws.cell(r, col)
                cell.border = thin
                cell.alignment = Alignment(vertical="top", wrap_text=col == 4)
            for col in (5, 6, 7, 8, 9, 12):
                ws.cell(r, col).number_format = "#,##0"
            ws.cell(r, 10).number_format = "0.0"
            ws.cell(r, 14).number_format = "DD.MM.YYYY HH:MM"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
        _autosize(ws)
        ws.row_dimensions[1].height = 32

    # Листы сегментов сразу после сводки (порядок 1→4).
    for key, sheet_title, _title in seg_sheets:
        _write_seg_sheet(sheet_title, by_seg.get(key) or [])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _ensure_result_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS efficiency_result_baselines (
            atm_id BIGINT PRIMARY KEY REFERENCES atms(id),
            tid TEXT,
            baseline_balance DOUBLE PRECISION NOT NULL,
            target_uzs DOUBLE PRECISION NOT NULL DEFAULT 400000000,
            baseline_over_cap DOUBLE PRECISION NOT NULL DEFAULT 0,
            baseline_demand DOUBLE PRECISION,
            address TEXT,
            region TEXT,
            branch TEXT,
            has_usd BOOLEAN NOT NULL DEFAULT FALSE,
            opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at TIMESTAMPTZ,
            last_balance DOUBLE PRECISION,
            last_seen_at TIMESTAMPTZ,
            last_removed_uzs DOUBLE PRECISION NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS efficiency_result_events (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            atm_id BIGINT NOT NULL REFERENCES atms(id),
            at TIMESTAMPTZ NOT NULL DEFAULT now(),
            balance DOUBLE PRECISION NOT NULL,
            removed_uzs DOUBLE PRECISION NOT NULL,
            note TEXT
        )
        """
    )


def sync_efficiency_results(days: int = 30, norm_days: int = NORM_DAYS) -> Dict[str, Any]:
    """Фиксирует базу «было» для ATM свыше 400 млн (низкий спрос) и обновляет прогресс.

    Новые кандидаты → baseline = текущий остаток.
    Уже в учёте → last_balance; если упал ниже цели — closed_at.
    Экономия = sum(max(0, baseline − current)).
    """
    data = atm_efficiency(days, norm_days)
    cap = float(data.get("cap_uzs") or CAP_UZS)
    candidates = [
        a for a in data["atms"]
        if a.get("over_cap_candidate")
        or (a.get("has_usd") and (a.get("over_cap") or 0) > 0 and a.get("low_demand"))
    ]
    by_id = {int(a["atm_id"]): a for a in data["atms"]}
    now = datetime.now(timezone.utc)

    with connect() as conn:
        _ensure_result_tables(conn)
        existing = {
            int(r["atm_id"]): r
            for r in conn.execute("SELECT * FROM efficiency_result_baselines").fetchall()
        }
        opened = updated = closed = 0
        candidate_ids = {int(a["atm_id"]) for a in candidates}
        for a in candidates:
            aid = int(a["atm_id"])
            bal = float(a["balance"])
            over = float(a.get("over_cap") or 0)
            if aid not in existing:
                # Точный over относительно потолка (без округления до млн).
                exact_over = max(0.0, bal - cap)
                conn.execute(
                    """
                    INSERT INTO efficiency_result_baselines (
                        atm_id, tid, baseline_balance, target_uzs, baseline_over_cap,
                        baseline_demand, address, region, branch, has_usd,
                        opened_at, last_balance, last_seen_at, last_removed_uzs
                    ) VALUES (
                        %(atm_id)s, %(tid)s, %(bal)s, %(cap)s, %(over)s,
                        %(demand)s, %(address)s, %(region)s, %(branch)s, %(has_usd)s,
                        %(now)s, %(bal)s, %(now)s, 0
                    )
                    """,
                    {
                        "atm_id": aid, "tid": a.get("tid"), "bal": bal, "cap": cap, "over": exact_over,
                        "demand": a.get("period_demand") or a.get("withdrawn") or 0,
                        "address": a.get("address"), "region": a.get("region"), "branch": a.get("branch"),
                        "has_usd": bool(a.get("has_usd")), "now": now,
                    },
                )
                conn.execute(
                    """
                    INSERT INTO efficiency_result_events (atm_id, at, balance, removed_uzs, note)
                    VALUES (%s, %s, %s, 0, %s)
                    """,
                    (aid, now, bal, "baseline"),
                )
                opened += 1
                continue

            row = existing[aid]
            baseline = float(row["baseline_balance"])
            removed = max(0.0, baseline - bal)
            prev_removed = float(row.get("last_removed_uzs") or 0)
            newly_closed = bal <= float(row.get("target_uzs") or cap) and row.get("closed_at") is None
            conn.execute(
                """
                UPDATE efficiency_result_baselines SET
                    tid = %(tid)s,
                    address = %(address)s,
                    region = %(region)s,
                    branch = %(branch)s,
                    has_usd = %(has_usd)s,
                    last_balance = %(bal)s,
                    last_seen_at = %(now)s,
                    last_removed_uzs = %(removed)s,
                    closed_at = CASE
                        WHEN %(close)s THEN COALESCE(closed_at, %(now)s)
                        WHEN %(bal)s > target_uzs THEN NULL
                        ELSE closed_at
                    END
                WHERE atm_id = %(atm_id)s
                """,
                {
                    "atm_id": aid, "tid": a.get("tid"), "address": a.get("address"),
                    "region": a.get("region"), "branch": a.get("branch"),
                    "has_usd": bool(a.get("has_usd")), "bal": bal, "now": now,
                    "removed": removed, "close": newly_closed,
                },
            )
            if removed > prev_removed + 1 or newly_closed:
                note = "closed" if newly_closed else "progress"
                conn.execute(
                    """
                    INSERT INTO efficiency_result_events (atm_id, at, balance, removed_uzs, note)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (aid, now, bal, removed, note),
                )
            updated += 1
            if newly_closed:
                closed += 1

        # Обновить тех, кто уже в учёте, но сейчас не в кандидатах (остаток мог упасть)
        tracked_ids = set(existing) | candidate_ids
        for aid in list(existing):
            if aid in candidate_ids:
                continue
            cur = by_id.get(aid)
            if not cur:
                continue
            bal = float(cur["balance"])
            row = existing[aid]
            baseline = float(row["baseline_balance"])
            removed = max(0.0, baseline - bal)
            newly_closed = bal <= float(row.get("target_uzs") or cap) and row.get("closed_at") is None
            conn.execute(
                """
                UPDATE efficiency_result_baselines SET
                    last_balance = %(bal)s,
                    last_seen_at = %(now)s,
                    last_removed_uzs = %(removed)s,
                    closed_at = CASE WHEN %(close)s THEN COALESCE(closed_at, %(now)s) ELSE closed_at END
                WHERE atm_id = %(atm_id)s
                """,
                {"atm_id": aid, "bal": bal, "now": now, "removed": removed, "close": newly_closed},
            )
            if newly_closed or removed > float(row.get("last_removed_uzs") or 0) + 1:
                conn.execute(
                    """
                    INSERT INTO efficiency_result_events (atm_id, at, balance, removed_uzs, note)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (aid, now, bal, removed, "closed" if newly_closed else "progress"),
                )
            updated += 1
            if newly_closed:
                closed += 1

        try:
            conn.commit()
        except Exception:
            pass

    return {
        "opened": opened,
        "updated": updated,
        "closed": closed,
        "tracked": len(tracked_ids),
        "cap_uzs": cap,
    }


def efficiency_result_history(days: int = 30, norm_days: int = NORM_DAYS, sync: bool = True) -> Dict[str, Any]:
    """История результата: база «было», сейчас, сколько изъяли, общая экономия."""
    if sync:
        sync_meta = sync_efficiency_results(days, norm_days)
    else:
        sync_meta = {}

    data = atm_efficiency(days, norm_days)
    by_id = {int(a["atm_id"]): a for a in data["atms"]}
    cap = float(data.get("cap_uzs") or CAP_UZS)

    with connect() as conn:
        _ensure_result_tables(conn)
        rows = conn.execute(
            """
            SELECT * FROM efficiency_result_baselines
            ORDER BY last_removed_uzs DESC NULLS LAST, baseline_balance DESC
            """
        ).fetchall()
        events_by = {}
        for e in conn.execute(
            """
            SELECT atm_id, at, balance, removed_uzs, note
            FROM efficiency_result_events
            ORDER BY at DESC, id DESC
            """
        ).fetchall():
            events_by.setdefault(int(e["atm_id"]), []).append({
                "at": _iso(e["at"]),
                "balance": e["balance"],
                "removed_uzs": e["removed_uzs"],
                "note": e["note"],
            })

    out = []
    for r in rows:
        aid = int(r["atm_id"])
        live = by_id.get(aid) or {}
        baseline = float(r["baseline_balance"])
        current = float(live["balance"]) if live.get("balance") is not None else float(r["last_balance"] or baseline)
        target = float(r.get("target_uzs") or cap)
        # Точные суммы без округления до млн — для 100% сверки.
        removed = round(max(0.0, baseline - current), 2)
        still_over = round(max(0.0, current - target), 2)
        done = current <= target or r.get("closed_at") is not None
        out.append({
            "atm_id": aid,
            "tid": r.get("tid") or live.get("tid"),
            "address": r.get("address") or live.get("address"),
            "region": r.get("region") or live.get("region"),
            "branch": r.get("branch") or live.get("branch"),
            "has_usd": bool(r.get("has_usd") or live.get("has_usd")),
            "baseline_balance": round(baseline, 2),
            "current_balance": round(current, 2),
            "target_uzs": target,
            "removed_uzs": removed,
            "still_over_uzs": still_over,
            "baseline_over_cap": round(max(0.0, baseline - target), 2),
            "opened_at": _iso(r.get("opened_at")),
            "closed_at": _iso(r.get("closed_at")),
            "status": "done" if done else ("progress" if removed > 0 else "waiting"),
            "events": (events_by.get(aid) or [])[:12],
        })

    done_rows = [a for a in out if a["status"] == "done"]
    prog_rows = [a for a in out if a["status"] == "progress"]
    wait_rows = [a for a in out if a["status"] == "waiting"]
    economy = round(sum(a["removed_uzs"] for a in out), 2)
    return {
        "days": days,
        "norm_days": norm_days,
        "cap_uzs": cap,
        "sync": sync_meta,
        "summary": {
            "tracked": len(out),
            "waiting": len(wait_rows),
            "progress": len(prog_rows),
            "done": len(done_rows),
            "baseline_total": round(sum(a["baseline_balance"] for a in out), 2),
            "current_total": round(sum(a["current_balance"] for a in out), 2),
            "economy_uzs": economy,
            "still_over_total": round(sum(a["still_over_uzs"] for a in out), 2),
        },
        "atms": out,
    }


def export_efficiency_result_xlsx(days: int = 30, norm_days: int = NORM_DAYS) -> bytes:
    """Excel Result history: сводка экономии + ATM по статусам на отдельных листах."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    data = efficiency_result_history(days, norm_days, sync=True)
    rows = data["atms"]
    cap = float(data.get("cap_uzs") or CAP_UZS)
    s = data["summary"]

    wb = Workbook()
    thin = Border(
        left=Side(style="thin", color="D0D7DE"),
        right=Side(style="thin", color="D0D7DE"),
        top=Side(style="thin", color="D0D7DE"),
        bottom=Side(style="thin", color="D0D7DE"),
    )
    head_fill = PatternFill("solid", fgColor="E8EEF7")
    head_font = Font(bold=True, color="1E293B")
    label_font = Font(bold=True, color="334155")

    meta = wb.active
    meta.title = "0. Экономия"
    meta.append(["Параметр", "Значение"])
    for cell in meta[1]:
        cell.fill = head_fill
        cell.font = head_font
        cell.border = thin
    for label, value in [
        ("Отчёт", "Result history · снижение до 400 млн"),
        ("Сформирован", datetime.now().strftime("%d.%m.%Y %H:%M")),
        ("Период спроса, дней", data.get("days")),
        ("Потолок UZS", cap),
        ("В учёте ATM", s["tracked"]),
        ("Ожидают изъятия", s["waiting"]),
        ("В процессе", s["progress"]),
        ("Достигли цели", s["done"]),
        ("Было (сумма баз)", s["baseline_total"]),
        ("Сейчас (сумма)", s["current_total"]),
        ("Экономия (изъято с базы)", s["economy_uzs"]),
        ("Ещё сверх потолка", s["still_over_total"]),
    ]:
        meta.append([label, value])
        meta.cell(meta.max_row, 1).font = label_font
        for col in (1, 2):
            meta.cell(meta.max_row, col).border = thin
        if label in ("Потолок UZS", "Было (сумма баз)", "Сейчас (сумма)",
                     "Экономия (изъято с базы)", "Ещё сверх потолка"):
            meta.cell(meta.max_row, 2).number_format = "#,##0"
    _autosize(meta)

    headers = [
        "TID", "Статус", "Регион", "Филиал", "Адрес", "USD",
        "Было (база)", "Сейчас", "Цель", "Изъято с базы", "Ещё сверх цели",
        "База зафиксирована", "Цель достигнута",
    ]
    status_sheets = [
        ("waiting", "1. Ожидают"),
        ("progress", "2. В процессе"),
        ("done", "3. Достигли цели"),
    ]

    def write_sheet(title: str, part: List[Dict[str, Any]]) -> None:
        ws = wb.create_sheet(title[:31])
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.border = thin
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.freeze_panes = "B2"
        if not part:
            ws.append(["—", "Нет записей"] + [""] * (len(headers) - 2))
            _autosize(ws)
            return
        for a in sorted(part, key=lambda x: (-x["removed_uzs"], -x["baseline_balance"])):
            ws.append([
                a.get("tid"),
                {"waiting": "ожидает", "progress": "в процессе", "done": "цель достигнута"}.get(a["status"], a["status"]),
                a.get("region") or "",
                a.get("branch") or "",
                a.get("address") or "",
                "да" if a.get("has_usd") else "нет",
                a.get("baseline_balance"),
                a.get("current_balance"),
                a.get("target_uzs"),
                a.get("removed_uzs"),
                a.get("still_over_uzs"),
                _parse_iso(a.get("opened_at")),
                _parse_iso(a.get("closed_at")),
            ])
            r = ws.max_row
            for col in range(1, len(headers) + 1):
                cell = ws.cell(r, col)
                cell.border = thin
                cell.alignment = Alignment(vertical="top", wrap_text=col == 5)
            for col in (7, 8, 9, 10, 11):
                ws.cell(r, col).number_format = "#,##0"
            for col in (12, 13):
                ws.cell(r, col).number_format = "DD.MM.YYYY HH:MM"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
        _autosize(ws)
        ws.row_dimensions[1].height = 32

    for st, title in status_sheets:
        write_sheet(title, [a for a in rows if a["status"] == st])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
