"""Рекомендации инкассации: цикл, номиналы, срочность, журнал и связь с фактической загрузкой."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from api.core import economics, incassation_plan as plan
from atm_monitor import history
from atm_monitor.db import connect
from tests_helpers import seed_snapshot


ECON = {"safety_days": 1.0, "default_cycle_days": 7.0, "min_cycle_days": 2.0, "max_cycle_days": 21.0,
        "trip_cost_uzs": None, "cost_of_funds_pct": None}


def test_cycle_days_wilson_formula_and_bounds():
    # K = 500 000 сум, r = 14 %/год, D = 50 млн/сутки → T* = √(2K / (D·r/365))
    t, src = plan.cycle_days(50e6, 500_000, 14, 7, 1, 30)
    assert src == "optimal"
    assert abs(t - math.sqrt(2 * 500_000 / (50e6 * 0.14 / 365))) < 1e-9
    assert plan.cycle_days(50e6, None, 14, 7, 2, 21) == (7, "default")
    assert plan.cycle_days(1e3, 1e9, 1, 7, 2, 21)[0] == 21     # ограничение сверху


def test_split_notes_in_packs_and_capacity():
    notes = plan.split_notes(300e6, {200_000: 2.0, 50_000: 1.0}, 1e12)
    assert all(c % 100 == 0 for c in notes.values())
    assert plan.notes_value(notes) >= 300e6
    tight = plan.split_notes(300e6, {200_000: 2.0, 50_000: 1.0}, 300e6)
    assert plan.notes_value(tight) <= 300e6


def test_urgency_scale():
    assert plan.urgency(0, None, [])[0] == 10
    assert plan.urgency(10, None, [])[0] == 8
    assert plan.urgency(200, None, [])[0] == 1
    s, reasons = plan.urgency(60, 0.95, ["01.11 выходной"])
    assert s == 6 and len(reasons) == 3


def test_build_recommendation_amounts():
    now = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)  # среда
    rec = plan.build_recommendation(available=48e6, total=148e6, capacity=600e6, demand_per_day=48e6,
                                    demand_rank=0.5, shares={100_000: 1.0}, econ=ECON, now=now)
    assert rec["hours_to_empty"] == 24.0
    assert rec["due_at"] == now                       # остаток уже на страховом запасе (1 день)
    # 7 дней + 1 день запаса = 384 млн, вверх до целых пачек по 100 купюр × 100 000 = 390 млн
    assert rec["load_uzs"] == 390e6
    assert rec["amount_to_bring"] == rec["load_uzs"] - 48e6
    assert any("неисправных кассетах" in r for r in rec["reasons"])


def test_refresh_plan_journal_and_link(pg_db):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid, latitude, longitude, address) VALUES (1, 'T1', 41.3, 69.28, 'Тошкент шаҳри')")
        # 3 суток почасовых снимков: 2 млн в час
        for h in range(72, 0, -1):
            seed_snapshot(conn, 1, now - timedelta(hours=h), 200e6 - (72 - h) * 2e6, [("OK", None, 100_000)])
    history.process_history()
    assert plan.refresh_plan(now)["written"] == 1
    assert plan.refresh_plan(now + timedelta(minutes=15))["written"] == 0   # план не изменился — не пишем

    q = plan.queue()
    assert q["count"] == 1
    item = q["items"][0]
    assert abs(item["demand_per_day"] - 48e6) < 1e6 and item["method"] == "history"
    # остаток 58 млн при 2 млн/ч — около 29 ч до нуля: база 6 (+1, если впереди выходной/зарплата)
    assert 6 <= item["score"] <= 7

    assert plan.set_status(item["id"], "accepted", "tester")["status"] == "accepted"
    # банк загрузил банкомат — загрузка связывается с принятым планом
    with connect() as conn:
        seed_snapshot(conn, 1, now + timedelta(minutes=30), 500e6, [("OK", None, 100_000)])
    history.process_history()
    plan.refresh_plan(now + timedelta(minutes=31))
    with connect() as conn:
        inc = conn.execute("SELECT * FROM atm_incassations").fetchone()
        assert inc["recommendation_id"] == item["id"] and inc["followed_plan"] is True
        assert conn.execute("SELECT status FROM recommendations WHERE id = %s", (item["id"],)).fetchone()["status"] == "done"


def test_economics_settings_missing_by_default(pg_db):
    s = economics.get_settings()
    assert set(s["missing"]) == {"cost_of_funds_pct", "trip_cost_uzs", "withdrawal_income_pct"}
    s = economics.update_settings({"trip_cost_uzs": 400_000}, "admin")
    assert s["values"]["trip_cost_uzs"] == 400_000
