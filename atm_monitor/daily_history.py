"""История банкомата по дням из отчётов BTech (те же данные, что на вкладке «Статистика»).

balanceHistory — остаток каждой кассеты на конец дня; turnoverHistory — выдачи по номиналам.
В turnoverHistory одна кассета встречается за день несколько раз, строки суммируются.
Первый день в ответе BTech неполный (начало диапазона попадает в его середину), он отбрасывается."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from .utils import parse_int

MIN_NOMINAL = 1000
MAX_NOMINAL = 200_000


def _day(item: Dict[str, Any]) -> str:
    return str(item.get("date"))[:10]


def _without_first_day(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    days = {_day(i) for i in items}
    if not days:
        return []
    first = min(days)
    return [i for i in items if _day(i) != first]


def balance_rows(items: List[Dict[str, Any]], atm_id: int) -> List[Dict[str, Any]]:
    return [
        {
            "atm_id": atm_id,
            "day": _day(i),
            "cassette_index": i["index"],
            "unit_id": i.get("unitId"),
            "cassette_type": i.get("type"),
            "currency": i.get("currency"),
            "nominal": parse_int(i.get("values")),
            "count": parse_int(i.get("count")),
        }
        for i in _without_first_day(items)
    ]


def turnover_rows(items: List[Dict[str, Any]], atm_id: int) -> List[Dict[str, Any]]:
    totals: Dict[tuple, Dict[str, Any]] = {}
    for i in _without_first_day(items):
        key = (_day(i), i.get("currency") or "", parse_int(i.get("values")) or 0)
        row = totals.setdefault(key, {
            "atm_id": atm_id, "day": key[0], "currency": key[1], "nominal": key[2],
            "dispensed_count": 0, "presented_count": 0, "cash_in_count": 0,
        })
        row["dispensed_count"] += parse_int(i.get("dispensedCount")) or 0
        row["presented_count"] += parse_int(i.get("presentedCount")) or 0
        row["cash_in_count"] += parse_int(i.get("cashInCount")) or 0
    return list(totals.values())


def daily_series(
    balances: Iterable[Dict[str, Any]],
    turnovers: Iterable[Dict[str, Any]],
    load_days: Set[str],
) -> List[Dict[str, Any]]:
    """Ряд для графика: остаток UZS на конец дня и выдачи за день, по возрастанию дат.

    Если BTech не отдал выдачи за день, они оцениваются по падению остатка (withdrawn_estimated).
    В день загрузки падение остатка ничего не говорит о выдачах, там остаётся None."""
    days: Dict[str, Dict[str, Any]] = {}
    for b in balances:
        day = str(b["day"])[:10]
        row = days.setdefault(day, {"day": day, "balance": 0.0, "notes": {}, "presented": None, "dispensed": None, "cash_in": None})
        nominal = b.get("nominal") or 0
        if b.get("cassette_type") == "BILLCASSETTE" and b.get("currency") == "UZS" and MIN_NOMINAL <= nominal <= MAX_NOMINAL:
            count = b.get("count") or 0
            row["balance"] += count * nominal
            row["notes"][str(nominal)] = row["notes"].get(str(nominal), 0) + count
    for t in turnovers:
        row = days.get(str(t["day"])[:10])
        if row is None or t.get("currency") != "UZS":
            continue
        for src, dst in (("presented_count", "presented"), ("dispensed_count", "dispensed"), ("cash_in_count", "cash_in")):
            row[dst] = (row[dst] or 0) + (t.get(src) or 0) * (t.get("nominal") or 0)

    series = [days[d] for d in sorted(days)]
    previous: Optional[Dict[str, Any]] = None
    for row in series:
        row["load"] = row["day"] in load_days
        row["withdrawn"] = row["presented"]
        row["withdrawn_estimated"] = False
        if row["withdrawn"] is None and previous is not None and not row["load"]:
            row["withdrawn"] = max(0.0, previous["balance"] - row["balance"])
            row["withdrawn_estimated"] = True
        previous = row
    return series
