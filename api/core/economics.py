"""Экономические параметры банка для рекомендаций и расчёта экономии.

Финансовые параметры (стоимость денег, стоимость выезда, доход со снятия)
по умолчанию НЕ заданы: без них система не придумывает суммы в деньгах, а
показывает объёмы (лишние наличные, часы без денег) и пишет, какой параметр
нужно ввести. Значения задаются в /dashboard/savings.html (только админ) и
хранятся в таблице app_settings вместе с тем, кто и когда их поменял.

Параметры политики (страховой запас, границы цикла) имеют разумные значения
по умолчанию, и это явно помечено в ответе API (source = "default")."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .db import connect

SETTINGS: List[Dict[str, Any]] = [
    {"key": "cost_of_funds_pct", "label": "Стоимость денег для банка", "unit": "% годовых", "default": None,
     "hint": "Сколько банк теряет в год на каждом суме, лежащем без дела в банкомате (ставка привлечения / "
             "доходность, которую дали бы эти деньги). Например, ставка ЦБ или внутренняя трансфертная ставка.",
     "min": 0, "max": 100},
    {"key": "trip_cost_uzs", "label": "Стоимость одного выезда инкассации", "unit": "сум", "default": None,
     "hint": "Машина, топливо, инкассаторы, охрана, пересчёт — в среднем на один заезд к банкомату.",
     "min": 0, "max": 1e10},
    {"key": "withdrawal_income_pct", "label": "Доход банка со снятия", "unit": "% от суммы", "default": None,
     "hint": "Средняя комиссия/доход банка с суммы снятий. Нужна, чтобы оценить потери, пока банкомат без денег.",
     "min": 0, "max": 10},
    {"key": "safety_days", "label": "Страховой запас", "unit": "дней спроса", "default": 1.0,
     "hint": "Сколько дней спроса должно оставаться в банкомате к приезду инкассации.", "min": 0, "max": 14},
    {"key": "default_cycle_days", "label": "Цикл загрузки без стоимостей", "unit": "дней", "default": 7.0,
     "hint": "На сколько дней грузить, пока не заданы стоимость денег и стоимость выезда "
             "(с ними цикл считается оптимальным по формуле).", "min": 1, "max": 60},
    {"key": "min_cycle_days", "label": "Минимальный цикл", "unit": "дней", "default": 2.0,
     "hint": "Чаще этого не ездить к одному банкомату.", "min": 0.5, "max": 30},
    {"key": "max_cycle_days", "label": "Максимальный цикл", "unit": "дней", "default": 21.0,
     "hint": "Реже этого не ездить (износ купюр, ревизии).", "min": 1, "max": 90},
]
_BY_KEY = {s["key"]: s for s in SETTINGS}


def create_settings_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key        TEXT PRIMARY KEY,
            value      DOUBLE PRECISION,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by TEXT
        )
    """)


def get_settings() -> Dict[str, Any]:
    """{key: {value, source: bank|default|missing, ...meta}} и плоский словарь values."""
    with connect() as conn:
        rows = {r["key"]: r for r in conn.execute("SELECT * FROM app_settings").fetchall()}
    items, values = [], {}
    for meta in SETTINGS:
        row = rows.get(meta["key"])
        if row is not None and row["value"] is not None:
            value, source = float(row["value"]), "bank"
        elif meta["default"] is not None:
            value, source = float(meta["default"]), "default"
        else:
            value, source = None, "missing"
        values[meta["key"]] = value
        items.append({
            **{k: meta[k] for k in ("key", "label", "unit", "hint", "min", "max")},
            "value": value, "default": meta["default"], "source": source,
            "updated_at": row["updated_at"] if row else None,
            "updated_by": row["updated_by"] if row else None,
        })
    return {"items": items, "values": values,
            "missing": [i["key"] for i in items if i["source"] == "missing"]}


def update_settings(changes: Dict[str, Optional[float]], user: Optional[str]) -> Dict[str, Any]:
    with connect() as conn:
        for key, value in changes.items():
            meta = _BY_KEY.get(key)
            if meta is None:
                raise ValueError(f"Неизвестный параметр {key!r}")
            if value is not None:
                value = float(value)
                if not (meta["min"] <= value <= meta["max"]):
                    raise ValueError(f"{meta['label']}: значение вне диапазона {meta['min']}…{meta['max']}")
            conn.execute(
                """INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (%s, %s, now(), %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(),
                       updated_by = EXCLUDED.updated_by""",
                (key, value, user),
            )
    return get_settings()
