"""Экономия: оценка цикла и сводка по реальным загрузкам."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.core import economics, savings
from atm_monitor import history
from atm_monitor.db import connect
from tests_helpers import seed_snapshot

ECON = {"trip_cost_uzs": 500_000.0, "cost_of_funds_pct": 14.6, "withdrawal_income_pct": 1.0}


def test_evaluate_cycle_numbers():
    # 10 дней, сняли 100 млн (10 млн/сутки), банк держал в среднем 500 млн.
    # Система грузит 80 млн при запасе 1 день: хватит на (80−10)/10 = 7 дней, средний остаток 10 + 35 = 45 млн.
    ev = savings.evaluate_cycle(days=10, withdrawn=100e6, covered_hours=240, avg_available=500e6,
                                cashout_hours=5, system_load=80e6, system_safety_days=1, econ=ECON)
    assert ev["valid"]
    assert ev["system_cycle_days"] == 7
    assert ev["system_avg_cash"] == 45e6
    assert ev["freed_cash"] == 455e6
    r = 0.146 / 365
    bank = 500_000 / 10 + r * 500e6
    system = 500_000 / 7 + r * 45e6
    assert abs(ev["saving"] - (bank - system) * 10) < 1e-3
    assert abs(ev["lost_withdrawals"] - 5 * 10e6 / 24) < 1e-6
    assert abs(ev["lost_income"] - ev["lost_withdrawals"] * 0.01) < 1e-6


def test_evaluate_cycle_without_money_params_keeps_volumes():
    ev = savings.evaluate_cycle(days=10, withdrawn=100e6, covered_hours=240, avg_available=500e6,
                                cashout_hours=0, system_load=80e6, system_safety_days=1, econ={})
    assert ev["valid"] and ev["saving"] is None and ev["freed_cash"] == 455e6


def test_evaluate_cycle_skips_poor_coverage():
    ev = savings.evaluate_cycle(days=10, withdrawn=1e6, covered_hours=50, avg_available=1e8,
                                cashout_hours=0, system_load=1e8, system_safety_days=1, econ=ECON)
    assert not ev["valid"] and ev["skip_reason"] == "мало опросов за цикл"


def test_summary_backtest_on_real_cycles(pg_db):
    economics.update_settings({"trip_cost_uzs": 500_000, "cost_of_funds_pct": 14.6}, "test")
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=40)
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1, 'T1')")
        bal, t = 900e6, start
        while t <= now:
            if t.hour == 10 and (t - start).days % 10 == 0 and t > start + timedelta(hours=12):
                bal = 900e6                       # банк каждые 10 дней грузит до 900 млн
            seed_snapshot(conn, 1, t, bal, [("OK", None, 100_000)])
            bal = max(0.0, bal - 1e6)             # спрос 24 млн/сутки
            t += timedelta(hours=1)
    history.process_history()
    out = savings.savings_summary(60)
    p = out["potential"]
    assert p["cycles"] >= 2 and p["by_method"]["backtest"] == p["cycles"]
    # банк держит в среднем ~780 млн, система — около (24 × T + 24)/2 + 24: денег освобождается много
    assert p["freed_cash"] > 300e6
    assert p["saving"] > 0 and out["money_available"]
    assert out["realized"]["cycles"] == 0
