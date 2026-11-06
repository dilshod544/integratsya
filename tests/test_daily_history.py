"""История по дням: остаток, выдачи и обновление в базе."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from atm_monitor import daily_history as dh
from atm_monitor import encashments
from atm_monitor.config import settings
from atm_monitor.db import connect

A = 18906161


def _bal(day, index, nominal, count, currency="UZS", kind="BILLCASSETTE"):
    return {"date": f"{day} 00:00:00", "index": index, "unitId": str(index), "type": kind, "currency": currency,
            "values": str(nominal), "count": count}


def _tur(day, nominal, presented, dispensed=None, index=1):
    return {"date": f"{day} 00:00:00", "index": index, "currency": "UZS", "values": str(nominal),
            "dispensedCount": presented if dispensed is None else dispensed, "presentedCount": presented, "cashInCount": 0}


def test_first_partial_day_is_dropped():
    items = [_bal("2026-10-25", 1, 200000, 5), _bal("2026-10-26", 1, 200000, 4)]
    assert [r["day"] for r in dh.balance_rows(items, A)] == ["2026-10-26"]
    assert dh.balance_rows([], A) == []


def test_repeated_turnover_rows_are_summed():
    items = [_tur("2026-10-25", 200000, 99), _tur("2026-10-26", 200000, 3), _tur("2026-10-26", 200000, 4, dispensed=5)]
    rows = dh.turnover_rows(items, A)
    assert len(rows) == 1
    assert rows[0]["presented_count"] == 7 and rows[0]["dispensed_count"] == 8


def _balance_day(day, count):
    return [
        {"day": day, "cassette_index": 1, "cassette_type": "BILLCASSETTE", "currency": "UZS", "nominal": 200000, "count": count},
        # кассеты-заглушки BTech и отбраковка в остаток не входят
        {"day": day, "cassette_index": 5, "cassette_type": "BILLCASSETTE", "currency": "NCR", "nominal": 5, "count": 0},
        {"day": day, "cassette_index": 8, "cassette_type": "REJECTCASSETTE", "currency": None, "nominal": 0, "count": 11},
    ]


def test_series_matches_real_cycle_numbers():
    """26.10: загружено 920 млн, выдано 103,5 млн, на конец дня ≈ 816,5 млн."""
    balances = _balance_day("2026-10-25", 2125) + _balance_day("2026-10-26", 4082)
    turnovers = [{"day": "2026-10-26", "currency": "UZS", "nominal": 200000, "presented_count": 518,
                  "dispensed_count": 518, "cash_in_count": 0}]
    first, second = dh.daily_series(balances, turnovers, {"2026-10-26"})
    assert first["balance"] == 425_000_000 and first["load"] is False and first["withdrawn"] is None
    assert second["balance"] == 816_400_000 and second["load"] is True
    assert second["withdrawn"] == 103_600_000 and second["withdrawn_estimated"] is False
    assert second["notes"] == {"200000": 4082}


def test_missing_turnover_is_estimated_from_balance_drop_but_not_on_load_days():
    balances = _balance_day("2026-08-24", 1000) + _balance_day("2026-08-25", 900) + _balance_day("2026-08-26", 2000)
    series = dh.daily_series(balances, [], {"2026-08-26"})
    assert series[1]["withdrawn"] == 20_000_000 and series[1]["withdrawn_estimated"] is True
    # в день загрузки выдач по остатку не узнать: это пропуск, а не ноль
    assert series[2]["withdrawn"] is None and series[2]["withdrawn_estimated"] is False


def test_sync_stores_history_and_then_refreshes_only_recent_days(pg_db, monkeypatch):
    monkeypatch.setattr(settings, "ENCASHMENT_REQUEST_PAUSE_SEC", 0)
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "/x/{atm_id}")
    monkeypatch.setattr(encashments, "fetch_encashments", lambda a: [])
    today = datetime.now(timezone.utc).date()
    d0, d1 = str(today - timedelta(days=2)), str(today - timedelta(days=1))
    starts = []

    def balance(atm_id, start, end):
        starts.append(start[:10])
        return [_bal(d0, 1, 200000, 10), _bal(d1, 1, 200000, len(starts))]

    monkeypatch.setattr(encashments, "fetch_balance_history", balance)
    monkeypatch.setattr(encashments, "fetch_turnover_history", lambda a, s, e: [_tur(d0, 200000, 1), _tur(d1, 200000, 2)])
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (%s, 'T')", (A,))

    encashments.sync_encashments([A], days=30)
    encashments.sync_encashments([A], days=30)

    assert starts[0] == str(today - timedelta(days=31)) or starts[0] == str(today - timedelta(days=32))
    assert starts[1] > starts[0]                         # второй раз только последние дни
    with connect() as conn:
        rows = conn.execute("SELECT day::text, count FROM btech_balance_daily ORDER BY day").fetchall()
        assert rows == [{"day": d1, "count": 2}]         # значение обновилось, дублей нет
        assert conn.execute("SELECT presented_count FROM btech_turnover_daily").fetchall() == [{"presented_count": 2}]
