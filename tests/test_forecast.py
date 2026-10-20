from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from atm_monitor import forecast
from atm_monitor.db import connect

TZ = ZoneInfo("Asia/Tashkent")


def test_is_due_after_midnight_slot():
    now = datetime(2026, 10, 20, 0, 5, tzinfo=TZ)
    assert forecast.is_due(now, None, 0)
    assert forecast.is_due(now, datetime(2026, 10, 19, 0, 1, tzinfo=TZ), 0)
    assert not forecast.is_due(now, datetime(2026, 10, 20, 0, 1, tzinfo=TZ), 0)
    # До слота 03:00 сегодня — достаточно вчерашнего прогона после 03:00.
    early = datetime(2026, 10, 20, 2, 0, tzinfo=TZ)
    assert not forecast.is_due(early, datetime(2026, 10, 19, 3, 0, tzinfo=TZ), 3)


def test_project_balance_crossings_and_refill():
    now = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
    # 120 млн, расход 48 млн/сутки = 2 млн/час; порог 80 млн будет через 20 ч, ноль — через 60 ч.
    p = forecast.project_balance(120e6, [48e6] * 7, now, now.date(), 80e6, 1e9)
    assert p["low_cash_at"] == now + timedelta(hours=20)
    assert p["cashout_at"] == now + timedelta(hours=60)
    assert round(p["withdrawal_24h"]) == 48_000_000
    assert p["balance_ends"][:3] == [72e6, 24e6, 0.0]
    # спрос 7 × 48 = 336 млн + порог 80 − остаток 120 = 296 млн
    assert p["refill_uzs"] == 296_000_000
    # больше ёмкости кассет не загрузить: 400 − 120 = 280 млн
    capped = forecast.project_balance(120e6, [48e6] * 7, now, now.date(), 80e6, 400e6)
    assert capped["refill_uzs"] == 280_000_000


def test_project_balance_mid_day_counts_only_remaining_hours():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
    p = forecast.project_balance(300e6, [24e6, 24e6], now, now.date(), 80e6, 400e6)
    assert p["balance_ends"] == [288e6, 264e6]
    assert p["low_cash_at"] is None and p["cashout_at"] is None
    assert p["refill_uzs"] == 0


def _seed_history(days=30, atms=10):
    """Почасовые снимки: каждый ATM тратит ~1–2 млн в час (больше в будни), раз в 5 дней — инкассация."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    with connect() as conn:
        for a in range(1, atms + 1):
            conn.execute("INSERT INTO atms (id, tid) VALUES (%s, %s)", (a, f"T{a}"))
            bal, t = 300e6, start
            rows = []
            while t <= now:
                local = t.astimezone(TZ)
                per_hour = (1e6 + a * 1e5) * (1.4 if local.weekday() < 5 else 0.8)
                if local.hour == 9 and local.day % 5 == 0:
                    bal = 300e6
                bal = max(0.0, bal - per_hour)
                rows.append((a, t, bal))
                t += timedelta(hours=1)
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO atm_snapshots (atm_id, polled_at, cdm_total_uzs) VALUES (%s,%s,%s)", rows
                )


def test_daily_withdrawals_ignores_refills_and_cassette_swap(pg_db):
    t0 = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)  # 10:00 по Ташкенту
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1, 'T1')")
        # 100 → 90 (снятие 10) → 0 (замена кассет) → 200 (загрузка) → 195 (снятие 5); млн
        for i, bal in enumerate([100e6, 90e6, 0, 200e6, 195e6]):
            conn.execute(
                "INSERT INTO atm_snapshots (atm_id, polled_at, cdm_total_uzs) VALUES (1, %s, %s)",
                (t0 + timedelta(hours=i), bal),
            )
        df = forecast.daily_withdrawals(conn, days=10_000)
    assert len(df) == 1
    assert df.iloc[0]["withdrawn"] == 15e6
    assert df.iloc[0]["refills"] == 1
    assert df.iloc[0]["covered_hours"] == 4


def test_run_forecast_trains_xgboost_and_saves(pg_db):
    _seed_history(days=45)
    assert forecast.run_forecast() == 10
    with psycopg.connect(forecast.settings.DATABASE_URL) as conn:
        run = conn.execute(
            "SELECT status, model, atm_count, train_rows, metrics FROM atm_monitor.forecast_runs"
        ).fetchone()
        assert run[0] == "success"
        assert run[1] == "xgboost"
        assert run[2] == 10 and run[3] >= forecast.MIN_TRAIN_ROWS
        assert "wape" in run[4]
        methods = {m for (m,) in conn.execute("SELECT method FROM atm_monitor.atm_forecast_summary")}
        assert methods == {"xgboost"}
        days = conn.execute("SELECT COUNT(*) FROM atm_monitor.atm_forecasts").fetchone()[0]
        assert days == 10 * forecast.settings.FORECAST_DAYS
        # Модель восстанавливает порядок величины спроса (~24–40 млн/сутки).
        w = conn.execute("SELECT AVG(withdrawal_24h) FROM atm_monitor.atm_forecast_summary").fetchone()[0]
        assert 15e6 < w < 60e6


def test_run_forecast_falls_back_with_short_history(pg_db):
    _seed_history(days=3, atms=2)
    assert forecast.run_forecast() == 2
    with connect() as conn:
        run = conn.execute("SELECT model FROM forecast_runs").fetchone()
        methods = {r["method"] for r in conn.execute("SELECT method FROM atm_forecast_summary").fetchall()}
    assert run["model"] == "fallback"
    assert methods == {"mean_7d"}
