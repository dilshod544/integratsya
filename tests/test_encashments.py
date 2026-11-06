"""Инкассации BTech: разбор ответа, период, повторный сбор без дублей."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from atm_monitor import api_client, encashments
from atm_monitor.config import settings
from atm_monitor.db import connect

ATM_ID = 18906161


def _money(total, **values):
    return {"currencies": {"UZS": {"total": total, "values": values}}, "units": {}}


# Цикл 344373 банкомата 18906161 из ответа BTech (сокращён список кассет и действий).
OPEN_CYCLE = {
    "id": 344373,
    "startedAt": "2026-10-30T07:25:04+00:00",
    "finishedAt": None,
    "loaded": _money(910000000),
    "unloaded": None,
    "left": _money(502140000),
    "dispensed": _money(407860000),
    "rejected": _money(1440000),
    "presented": _money(406420000),
    "accepted": {"currencies": {}, "units": {}},
    "retracted": {"total": 0},
    "cashOutTotals": [
        {"id": "1", "number": 1, "status": "LOW", "type": "BILLCASSETTE", "currency": "UZS", "values": 200000,
         "count": 19, "loaded": 2000, "dispensedCount": 1981, "presentedCount": 1974, "rejectedCount": 7,
         "retractedCount": 0},
        {"id": "0", "number": 8, "status": "OK", "type": "REJECTCASSETTE", "currency": "", "values": 0,
         "count": 11, "loaded": 0, "dispensedCount": 0, "presentedCount": 0, "rejectedCount": 0,
         "retractedCount": 0},
    ],
    "cashInTotals": [],
    "actions": [
        {"id": 7566936, "createdAt": "2026-10-30T07:20:36.552Z", "encashmentId": 344373, "atmId": ATM_ID,
         "evtno": 30030, "data": "REJECT BIN REMOVED"},
    ],
}


def test_row_matches_table_on_the_site():
    row = encashments.encashment_row(OPEN_CYCLE, ATM_ID)
    assert row["loaded_uzs"] == 910_000_000
    assert row["dispensed_uzs"] == 407_860_000   # «Набрано»
    assert row["presented_uzs"] == 406_420_000   # «Выдано»
    assert row["rejected_uzs"] == 1_440_000      # «Отбраковано»
    assert row["started_at"] == datetime(2026, 10, 30, 7, 25, 4, tzinfo=timezone.utc)
    assert row["finished_at"] is None


def test_missing_amounts_are_null_not_zero():
    row = encashments.encashment_row(OPEN_CYCLE, ATM_ID)
    assert row["unloaded_uzs"] is None            # unloaded: null
    assert row["accepted_uzs"] is None            # currencies: {}
    assert row["retracted_total"] == 0            # а настоящий ноль остаётся нулём


def test_window_keeps_open_cycles_and_cycles_ending_inside():
    since = datetime(2026, 8, 6, tzinfo=timezone.utc)
    assert encashments.in_window({"finishedAt": None}, since)
    # начался до границы, закончился после: снятия этого цикла попадают в период
    assert encashments.in_window({"finishedAt": "2026-08-17T06:50:23+00:00"}, since)
    assert not encashments.in_window({"finishedAt": "2026-08-05T05:31:33+00:00"}, since)


def test_resync_updates_open_cycle_without_duplicates(pg_db):
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (%s, '544830')", (ATM_ID,))
        encashments.save_encashments(conn, ATM_ID, [OPEN_CYCLE])

    closed = copy.deepcopy(OPEN_CYCLE)
    closed["finishedAt"] = "2026-11-10T07:00:00+00:00"
    closed["unloaded"] = _money(300000000)
    closed["cashOutTotals"][0]["count"] = 5
    with connect() as conn:
        encashments.save_encashments(conn, ATM_ID, [closed])

    with connect() as conn:
        rows = conn.execute("SELECT * FROM btech_encashments").fetchall()
        assert len(rows) == 1
        assert rows[0]["unloaded_uzs"] == 300_000_000 and rows[0]["finished_at"] is not None
        assert rows[0]["raw_json"]["id"] == 344373
        cass = conn.execute("SELECT number, nominal, count FROM btech_encashment_cassettes ORDER BY number").fetchall()
        assert cass == [{"number": 1, "nominal": 200000, "count": 5}, {"number": 8, "nominal": 0, "count": 11}]
        assert conn.execute("SELECT COUNT(*) AS n FROM btech_encashment_actions").fetchone()["n"] == 1


def test_nul_characters_from_device_errors_are_saved_without_them(pg_db):
    cycle = copy.deepcopy(OPEN_CYCLE)
    cycle["actions"][0]["data"] = "Reason for Rejection: 0x00\x00\x00"
    cycle["errors"] = {"undefined\x00": ["Illegal parameter\x00"]}
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (%s, '544830')", (ATM_ID,))
        encashments.save_encashments(conn, ATM_ID, [cycle])
    with connect() as conn:
        assert conn.execute("SELECT data FROM btech_encashment_actions").fetchone()["data"] == "Reason for Rejection: 0x00"
        assert conn.execute("SELECT raw_json FROM btech_encashments").fetchone()["raw_json"]["errors"] == {"undefined": ["Illegal parameter"]}


def test_sync_continues_after_one_failed_atm(pg_db, monkeypatch):
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "/x/{atm_id}")
    monkeypatch.setattr(settings, "ENCASHMENT_REQUEST_PAUSE_SEC", 0)
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1, 'A'), (%s, 'B')", (ATM_ID,))

    def fake_fetch(atm_id):
        if atm_id == 1:
            raise RuntimeError("timeout")
        return [OPEN_CYCLE]

    monkeypatch.setattr(encashments, "fetch_encashments", fake_fetch)
    stats = encashments.sync_encashments()
    assert stats == {"atms": 1, "encashments": 1, "errors": 1, "aborted": 0, "stopped": 0}


def test_sync_stops_when_every_request_fails(pg_db, monkeypatch):
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "/x/{atm_id}")
    monkeypatch.setattr(settings, "ENCASHMENT_REQUEST_PAUSE_SEC", 0)
    calls = []

    def fake_fetch(atm_id):
        calls.append(atm_id)
        raise RuntimeError("404")

    monkeypatch.setattr(encashments, "fetch_encashments", fake_fetch)
    stats = encashments.sync_encashments(list(range(50)))
    assert stats["aborted"] == 1 and len(calls) == encashments.MAX_CONSECUTIVE_ERRORS


def test_fetch_requests_all_cycles_not_first_page(monkeypatch):
    seen = {}
    monkeypatch.setattr(api_client, "_get_items", lambda url, params: seen.update(url=url, params=params) or [])
    api_client.fetch_encashments(ATM_ID)
    assert seen["url"].endswith(f"/api/base/atm/{ATM_ID}/encashment")
    assert seen["params"]["limit"] == -1


def test_sync_refuses_without_configured_path(monkeypatch):
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "")
    with pytest.raises(RuntimeError, match="ATM_ENCASHMENT_PATH"):
        encashments.sync_encashments([ATM_ID])
