"""
Инкассации банкоматов за выбранный день и предыдущий (вкладка «Инкассации»
страницы /dashboard/analytics.html).

Источник — циклы инкассации BTech (таблица btech_encashments, сбор
atm_monitor/encashments.py). Инкассация в BTech — это начало нового цикла:
  * loaded нового цикла   — сколько загрузили;
  * unloaded прошлого цикла — сколько изъяли, т.е. сколько было до инкассации
    (опросы это подтверждают: остаток до инкассации = unloaded прошлого цикла).
Если BTech не записал изъятие, «было» считаем по прошлому циклу (загружено − набрано:
у закрытых циклов unloaded = loaded − dispensed), а если и этого нет — берём последний
опрос до инкассации.

BTech иногда пишет служебный цикл длиной в минуту, повторяющий суммы прошлого.
Такие циклы (короче SHORT_CYCLE) не считаются инкассациями.

Инкассация бывает в два шага: кассеты сняли (BTech открывает «пустой» цикл без
загрузки и выдачи), а через час-другой загрузили. Закрытый пустой цикл — это
не отдельная инкассация: «было» берём из цикла до него, а время изъятия
показываем отдельно (unloaded_at).
"""
from __future__ import annotations

import json

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from atm_monitor.config import settings
from atm_monitor.db import connect

from .atm_analytics import _iso, _registry_by_tid, _ts

SHORT_CYCLE = timedelta(minutes=30)
POLL_LOOKBACK = timedelta(hours=24)  # «было» по опросу — не старше этого до инкассации (время опроса показываем)

TYPES = {
    "replace": "Замена кассет",
    "load_empty": "Загрузка пустого",
    "unload": "Изъятие без загрузки",
    "load": "Загрузка",            # нет данных, сколько было до инкассации
}


def _json(value: Any) -> Dict[str, Any]:
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value or {}


def _currencies(block: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{"UZS": {"total": N, "values": {"номинал": купюр}}} — только непустые валюты."""
    out = {}
    for cur, v in ((block or {}).get("currencies") or {}).items():
        total = (v or {}).get("total")
        if total is None:
            continue
        values = {int(k): int(n) for k, n in ((v or {}).get("values") or {}).items() if n}
        out[cur.upper()] = {"total": float(total), "values": dict(sorted(values.items()))}
    return out


def classify(before: Dict[str, Any], loaded: Dict[str, Any], before_known: bool,
             unknown: Optional[set] = None) -> str:
    """unknown — валюты, остаток которых до инкассации неизвестен (расчёт по циклу и опрос дают только UZS)."""
    had = any(v["total"] > 0 for v in before.values())
    put = any(v["total"] > 0 for v in loaded.values())
    if not put:
        return "unload"
    if not before_known:
        return "load"
    if had:
        return "replace"
    # UZS не было, но про другую валюту не знаем — «пустым» банкомат назвать нельзя
    return "load" if unknown else "load_empty"


def _day_bounds(day: date, tz: ZoneInfo):
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _report(day: date) -> Dict[str, Any]:
    tz = ZoneInfo(settings.TIMEZONE)
    days = [day, day - timedelta(days=1)]
    since, _ = _day_bounds(days[1], tz)
    _, until = _day_bounds(days[0], tz)

    with connect() as conn:
        rows = conn.execute(
            """
            WITH kept AS (
                SELECT e.*,
                       LAG(e.started_at)    OVER w AS prev_started_at,
                       LAG(e.finished_at)   OVER w AS prev_finished_at,
                       LAG(e.raw_json)      OVER w AS prev_raw,
                       LAG(e.dispensed_uzs) OVER w AS prev_dispensed_uzs,
                       LAG(e.rejected_uzs)  OVER w AS prev_rejected_uzs
                FROM btech_encashments e
                WHERE NOT (e.finished_at IS NOT NULL AND e.finished_at - e.started_at < %(short)s)
                  AND NOT (e.finished_at IS NOT NULL AND e.dispensed_uzs IS NULL
                           AND COALESCE(e.raw_json #> '{loaded,currencies}', '{}'::jsonb) = '{}'::jsonb)
                WINDOW w AS (PARTITION BY e.atm_id ORDER BY e.started_at)
            )
            SELECT k.id, k.atm_id, k.started_at, k.finished_at, k.raw_json, k.prev_started_at, k.prev_finished_at,
                   k.prev_raw, k.prev_dispensed_uzs, k.prev_rejected_uzs, k.accepted_uzs,
                   a.tid, a.serial, a.address, a.place, a.vendor_name, a.model_name,
                   pb.cdm_total_uzs AS poll_before_uzs, pb.polled_at AS poll_before_at,
                   pa.cdm_total_uzs AS poll_after_uzs, pa.polled_at AS poll_after_at
            FROM kept k
            JOIN atms a ON a.id = k.atm_id
            LEFT JOIN LATERAL (
                SELECT cdm_total_uzs, polled_at FROM atm_snapshots
                WHERE atm_id = k.atm_id AND polled_at <= k.started_at
                  AND polled_at > k.started_at - %(lookback)s
                ORDER BY polled_at DESC LIMIT 1
            ) pb ON TRUE
            LEFT JOIN LATERAL (
                SELECT cdm_total_uzs, polled_at FROM atm_snapshots
                WHERE atm_id = k.atm_id AND polled_at > k.started_at
                  AND polled_at < k.started_at + %(lookback)s
                ORDER BY polled_at LIMIT 1
            ) pa ON TRUE
            WHERE k.started_at >= %(since)s AND k.started_at < %(until)s
            ORDER BY k.started_at DESC
            """,
            {"short": SHORT_CYCLE, "lookback": POLL_LOOKBACK, "since": since, "until": until},
        ).fetchall()
        last_sync = conn.execute("SELECT MAX(synced_at) AS t FROM btech_encashments").fetchone()["t"]

    registry = _registry_by_tid()
    items = []
    for r in rows:
        raw = _json(r["raw_json"])
        prev = _json(r["prev_raw"])
        loaded = _currencies(raw.get("loaded"))
        before = _currencies(prev.get("unloaded")) if prev else {}
        before_source = "btech" if before else None
        prev_loaded_uzs = (_currencies(prev.get("loaded")).get("UZS") or {}).get("total") if prev else None
        if not before and prev_loaded_uzs is not None and r["prev_dispensed_uzs"] is not None:
            before = {"UZS": {"total": max(0.0, prev_loaded_uzs - float(r["prev_dispensed_uzs"])), "values": {}}}
            before_source = "cycle"
        if not before and r["poll_before_uzs"] is not None:
            before = {"UZS": {"total": float(r["poll_before_uzs"]), "values": {}}}
            before_source = "poll"
        unknown = set()
        if before_source in ("cycle", "poll"):
            prev_loaded = _currencies(prev.get("loaded")) if prev else {}
            unknown = {c for c, v in {**prev_loaded, **loaded}.items() if c != "UZS" and v["total"] > 0}
        kind = classify(before, loaded, before_source is not None, unknown)

        reg = registry.get(str(r["tid"])) or {}
        started = _ts(r["started_at"])
        prev_started = _ts(r["prev_started_at"])
        currencies = sorted(set(loaded) | set(before), key=lambda c: (c != "UZS", c))
        items.append({
            "id": r["id"],
            "atm_id": r["atm_id"],
            "tid": r["tid"],
            "serial": r["serial"],
            "address": r["address"] or reg.get("registry_address"),
            "place": r["place"],
            "model": " ".join(x for x in (r["vendor_name"], r["model_name"]) if x) or None,
            "region": reg.get("region"),
            "branch": reg.get("branch"),
            "day": started.astimezone(tz).date().isoformat(),
            "at": _iso(started),
            # Когда кассеты сняли, если это было заметно раньше загрузки (двухшаговая инкассация)
            "unloaded_at": _iso(r["prev_finished_at"])
            if r["prev_finished_at"] and started - _ts(r["prev_finished_at"]) > SHORT_CYCLE else None,
            "type": kind,
            "type_label": TYPES[kind],
            "currencies": currencies,
            "loaded": loaded,
            "before": before,
            "before_source": before_source,
            "poll_before": {"uzs": r["poll_before_uzs"], "at": _iso(r["poll_before_at"])} if r["poll_before_at"] else None,
            "poll_after": {"uzs": r["poll_after_uzs"], "at": _iso(r["poll_after_at"])} if r["poll_after_at"] else None,
            "prev_cycle": {
                "started_at": _iso(prev_started),
                "days": round((started - prev_started).total_seconds() / 86400, 1) if prev_started else None,
                "dispensed_uzs": r["prev_dispensed_uzs"],
                "rejected_uzs": r["prev_rejected_uzs"],
            } if prev_started else None,
            "accepted_uzs": r["accepted_uzs"],
        })

    def totals(rows_: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"count": len(rows_), "by_type": {k: 0 for k in TYPES}, "loaded": {}, "before": {}}
        for it in rows_:
            out["by_type"][it["type"]] += 1
            for key in ("loaded", "before"):
                for cur, v in it[key].items():
                    out[key][cur] = out[key].get(cur, 0) + v["total"]
        return out

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "last_sync_at": _iso(last_sync),
        "sync_every_hours": settings.ENCASHMENT_SYNC_HOURS,
        "timezone": settings.TIMEZONE,
        "types": TYPES,
        "days": [
            {"day": d.isoformat(), **totals([i for i in items if i["day"] == d.isoformat()])}
            for d in days
        ],
        "items": items,
    }


def today() -> date:
    return datetime.now(ZoneInfo(settings.TIMEZONE)).date()


def encashments_report(day: Optional[date] = None) -> Dict[str, Any]:
    return _report(day or today())


def export_encashments_xlsx(day: Optional[date] = None, ids: Optional[List[int]] = None) -> bytes:
    """Excel: одна строка — одна инкассация; суммы по валютам и купюрам."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    from .atm_analytics import _autosize

    data = encashments_report(day)
    items = data["items"]
    if ids:
        wanted = {str(i) for i in ids}
        items = [i for i in items if str(i["id"]) in wanted]
    if not items:
        raise ValueError("Нет инкассаций для выгрузки")

    tz = ZoneInfo(settings.TIMEZONE)
    wb = Workbook()
    ws = wb.active
    ws.title = "Инкассации"
    head = ["Дата", "Время", "TID", "Регион", "Филиал", "Адрес", "Тип инкассации", "Валюта",
            "Было до инкассации", "Загружено", "Разница", "Источник «было»",
            "Купюры до (номинал × шт)", "Загружено купюр (номинал × шт)",
            "Набрано за прошлый цикл (UZS)", "Длина прошлого цикла, дн"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2563EB")
        c.alignment = Alignment(vertical="center", wrap_text=True)

    bills = lambda v: ", ".join(f"{n:,} × {k}".replace(",", " ") for n, k in (v or {}).items())
    for it in items:
        at = datetime.fromisoformat(it["at"]).astimezone(tz)
        for cur in it["currencies"] or ["UZS"]:
            b = it["before"].get(cur) or {}
            l = it["loaded"].get(cur) or {}
            ws.append([
                at.strftime("%d.%m.%Y"), at.strftime("%H:%M"), it["tid"], it["region"], it["branch"], it["address"],
                it["type_label"], cur,
                b.get("total"), l.get("total"),
                (l.get("total") or 0) - (b.get("total") or 0) if b or l else None,
                {"btech": "BTech: изъято из кассет", "cycle": "Расчёт: загружено − набрано за прошлый цикл",
                 "poll": "Последний опрос до инкассации"}.get(it["before_source"], "нет данных"),
                bills(b.get("values")), bills(l.get("values")),
                (it["prev_cycle"] or {}).get("dispensed_uzs") if cur == "UZS" else None,
                (it["prev_cycle"] or {}).get("days"),
            ])
    for row in ws.iter_rows(min_row=2):
        for idx in (8, 9, 10, 14):
            row[idx].number_format = "#,##0"
    ws.freeze_panes = "A2"
    _autosize(ws)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
