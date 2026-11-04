"""История состояний: доступный остаток, события, инкассации, суточные итоги."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from atm_monitor import history
from atm_monitor.db import connect


def test_available_excludes_broken_cassettes():
    cass = [
        {"cassette_type": "BILLCASSETTE", "status": "LOW", "count": 10, "nominal": 200_000, "currency": "UZS"},
        {"cassette_type": "BILLCASSETTE", "status": "INOP", "count": 1937, "nominal": 200_000, "currency": "UZS"},
        {"cassette_type": "BILLCASSETTE", "status": "MISSING", "count": 0, "nominal": None},
        {"cassette_type": "REJECTCASSETTE", "status": "OK", "count": 152, "nominal": None},
    ]
    assert history.available_from_cassettes(cass, 389_400_000) == 2_000_000
    assert history.available_from_cassettes([], 5.0) == 5.0
    assert history.notes_by_nominal(cass) == {"200000": 1947}


def test_detect_incassation_methods():
    before = {"cdm_total_uzs": 50e6, "cdm_initial_uzs": 900e6}
    assert history.detect_incassation(before, {"cdm_total_uzs": 800e6, "cdm_initial_uzs": 800e6}) == "initial_amount"
    assert history.detect_incassation(before, {"cdm_total_uzs": 300e6, "cdm_initial_uzs": 900e6}) == "balance_jump"
    # депозит клиента на 5 млн — не инкассация
    assert history.detect_incassation(before, {"cdm_total_uzs": 55e6, "cdm_initial_uzs": 900e6}) is None
    # долив 30 млн к 500 млн (< 10 %): инкассация, если клиентских внесений не было…
    big = {"cdm_total_uzs": 500e6, "last_transaction_cash_in": "2026-11-01T10:00:00+00:00"}
    assert history.detect_incassation(big, {"cdm_total_uzs": 530e6, "last_transaction_cash_in": "2026-11-01T10:00:00+00:00"}) == "balance_jump"
    # …и не инкассация, если клиент вносил деньги
    assert history.detect_incassation(big, {"cdm_total_uzs": 530e6, "last_transaction_cash_in": "2026-11-02T09:00:00+00:00"}) is None
    assert history.detect_incassation(before, {"cdm_total_uzs": 40e6, "cdm_initial_uzs": 900e6}) is None


def test_diff_states_reports_only_changes():
    prev = {"agent": "online", "cassette:2": "OK", "cash": "has_cash"}
    cur = {"agent": "online", "cassette:2": "INOP", "cash": "has_cash"}
    assert history.diff_states(prev, cur) == [("cassette:2", "OK", "INOP")]


def _snap(conn, atm_id, at, total, cassettes, initial=None, agent="online"):
    raw = {"cdmInitialAmount": {"totalUzs": initial}} if initial is not None else {}
    sid = conn.execute(
        """INSERT INTO atm_snapshots (atm_id, polled_at, agent_status, service_status, app_status,
               cdm_total_uzs, dispenser_status, raw_json)
           VALUES (%s,%s,%s,'InService','InService',%s,'ONLINE',%s) RETURNING id""",
        (atm_id, at, agent, total, json.dumps(raw)),
    ).fetchone()["id"]
    for i, (status, count, nominal) in enumerate(cassettes, start=1):
        conn.execute(
            """INSERT INTO cassette_snapshots (atm_snapshot_id, atm_id, polled_at, cassette_index,
                   cassette_type, status, count, currency, nominal)
               VALUES (%s,%s,%s,%s,'BILLCASSETTE',%s,%s,'UZS',%s)""",
            (sid, atm_id, at, i, status, count, nominal),
        )
    return sid


def test_process_history_events_incassations_daily(pg_db):
    t0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1, 'T1')")
        # 1000 купюр по 100k = 100 млн; затем снятия, затем кассета 2 ломается
        _snap(conn, 1, t0, 100e6, [("OK", 600, 100_000), ("OK", 400, 100_000)], initial=400e6)
        _snap(conn, 1, t0 + timedelta(hours=1), 90e6, [("OK", 500, 100_000), ("OK", 400, 100_000)], initial=400e6)
        _snap(conn, 1, t0 + timedelta(hours=2), 80e6, [("LOW", 400, 100_000), ("INOP", 400, 100_000)], initial=400e6)
        # инкассация: кассеты сняты (почти ноль), через час — новая загрузка 400 млн
        _snap(conn, 1, t0 + timedelta(hours=3), 0, [("MISSING", 0, 100_000), ("MISSING", 0, 100_000)], initial=400e6)
        _snap(conn, 1, t0 + timedelta(hours=4), 400e6, [("OK", 2000, 100_000), ("OK", 2000, 100_000)], initial=400e6)

    stats = history.process_history()
    assert stats["incassations"] == 1

    with connect() as conn:
        avail = [r["cdm_available_uzs"] for r in conn.execute(
            "SELECT cdm_available_uzs FROM atm_snapshots ORDER BY polled_at").fetchall()]
        assert avail == [100e6, 90e6, 40e6, 0, 400e6]
        inc = conn.execute("SELECT * FROM atm_incassations").fetchone()
        # «до» — снимок перед снятием кассет, а не почти пустой
        assert inc["balance_before"] == 80e6 and inc["available_before"] == 40e6
        assert inc["balance_after"] == 400e6
        assert inc["notes_after"] == {"100000": 4000}
        kinds = {(e["kind"], e["new_value"]) for e in conn.execute("SELECT * FROM atm_state_events").fetchall()}
        assert ("cassette:2", "INOP") in kinds
        assert ("cash", "empty") in kinds and ("cash", "has_cash") in kinds
        daily = conn.execute("SELECT SUM(withdrawn) AS w, SUM(cashout_hours) AS c FROM atm_daily").fetchone()
        # снятия 10 + 10 млн; падение в ноль при замене кассет — не снятие
        assert daily["w"] == 20e6
        assert daily["c"] == 1.0
        assert history.capacity_by_atm(conn, 0)[1] == 400e6

    # повторная обработка ничего не дублирует
    assert history.process_history()["incassations"] == 0
