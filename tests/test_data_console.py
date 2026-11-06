"""Скрытый модуль данных: доступ, просмотр таблиц, полнота по датам, догрузка."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from atm_monitor import data_console, encashments
from atm_monitor.config import settings
from atm_monitor.db import connect

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(pg_db):
    app = FastAPI()
    app.include_router(data_console.router)
    return TestClient(app)


def _request(origin=None):
    headers = [(b"host", b"testserver")]
    if origin:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": ("127.0.0.1", 5000)})


def test_other_sites_are_rejected():
    data_console.check_request(_request())
    data_console.check_request(_request("http://testserver"))
    with pytest.raises(HTTPException) as e:
        data_console.check_request(_request("https://evil.example"))
    assert e.value.status_code == 403


def test_module_opens_without_login_when_platform_login_is_off(pg_db, monkeypatch):
    from test_upload_safety import _app

    with TestClient(_app(monkeypatch, True)) as c:
        assert c.get("/api/data-console/tables").status_code == 200
        assert c.get("/api/data-console/tables", headers={"origin": "https://evil.example"}).status_code == 403


def test_module_requires_admin_when_platform_login_is_on(pg_db, monkeypatch):
    from test_upload_safety import _app

    with TestClient(_app(monkeypatch, False)) as c:
        import core.auth as core_auth  # путь к core появляется после импорта api.main

        core_auth.update_user(1, new_password="secret1")
        role = next(r for r in core_auth.list_roles() if r["name"] == "viewer")
        core_auth.create_user("viewer1", "secret2", None, role["id"])

        url = "/api/data-console/tables"
        assert c.get(url).status_code == 401
        assert c.post("/api/auth/login", json={"username": "viewer1", "password": "secret2"}).status_code == 200
        assert c.get(url).status_code == 403
        assert c.post("/api/auth/login", json={"username": "admin", "password": "secret1"}).status_code == 200
        assert c.get(url).status_code == 200


def test_password_columns_are_never_returned_or_searchable(client):
    with connect() as conn:
        stored = conn.execute("SELECT password_hash FROM public.users LIMIT 1").fetchone()["password_hash"]
    data = client.get("/api/data-console/tables/public/users").json()
    assert {"password_hash", "password_salt"} == set(data["hidden_columns"])
    assert "password_hash" not in data["rows"][0]
    assert client.get("/api/data-console/tables/public/users", params={"q": stored}).json()["total"] == 0
    assert client.get("/api/data-console/tables/public/users", params={"sort": "password_hash"}).status_code == 400


def test_unknown_tables_and_schemas_are_refused(client):
    assert client.get("/api/data-console/tables/pg_catalog/pg_authid").status_code == 404
    assert client.get("/api/data-console/tables/public/nope").status_code == 404
    assert client.get('/api/data-console/tables/public/users";DROP TABLE users;--').status_code == 404


def test_table_paging_sort_and_search(client):
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1,'AAA'), (2,'BBB'), (3,'50%')")
    url = "/api/data-console/tables/atm_monitor/atms"
    page = client.get(url, params={"limit": 2, "sort": "id", "direction": "asc"}).json()
    assert page["total"] == 3 and [r["id"] for r in page["rows"]] == [1, 2]
    assert [r["id"] for r in client.get(url, params={"limit": 2, "offset": 2, "sort": "id", "direction": "asc"}).json()["rows"]] == [3]
    assert client.get(url, params={"q": "bbb"}).json()["total"] == 1
    assert client.get(url, params={"q": "%"}).json()["total"] == 1   # % — обычный символ, а не «всё»


def test_atm_status():
    start = NOW - timedelta(days=92)
    fresh = {"last_success_at": NOW - timedelta(hours=1), "synced_since": start - timedelta(hours=1),
             "balance_since": start - timedelta(hours=1), "last_error": None}
    assert data_console.atm_status(fresh, start, NOW) == "ok"
    assert data_console.atm_status({}, start, NOW) == "never"
    assert data_console.atm_status({**fresh, "last_error": "503"}, start, NOW) == "error"
    assert data_console.atm_status({**fresh, "synced_since": start + timedelta(days=30)}, start, NOW) == "shallow"
    assert data_console.atm_status({**fresh, "last_success_at": NOW - timedelta(days=3)}, start, NOW) == "stale"
    assert data_console.atm_status({**fresh, "balance_since": None}, start, NOW) == "shallow"


def test_polls_status():
    assert data_console.polls_status(96, 96) == "ok"
    assert data_console.polls_status(87, 96) == "ok"
    assert data_console.polls_status(10, 96) == "partial"
    assert data_console.polls_status(0, 96) == "none"
    assert data_console.polls_status(0, 0) == "ok"


def _cycle(id_, start, end):
    return {"id": id_, "startedAt": start.isoformat(), "finishedAt": end.isoformat() if end else None,
            "loaded": {"currencies": {"UZS": {"total": 100}}}, "cashOutTotals": [], "actions": []}


def _day(data, d):
    return next(c for c in data["calendar"] if c["day"] == d.isoformat())


def test_coverage_tells_empty_date_from_unchecked_atm(client, monkeypatch):
    """Цикла за дату нет у обоих банкоматов, но проверен только первый: пропуск — только у второго."""
    monkeypatch.setattr(settings, "ENCASHMENT_REQUEST_PAUSE_SEC", 0)
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "/x/{atm_id}")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=5)
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1,'A'), (2,'B')")

    monkeypatch.setattr(encashments, "fetch_encashments", lambda atm_id: [_cycle(10, start, None)])
    encashments.sync_encashments([1], days=30)

    data = client.get("/api/data-console/coverage", params={"days": 30}).json()
    assert data["atm_status_counts"] == {"ok": 1, "never": 1}
    today = now.astimezone(data_console._tz()).date()
    old = _day(data, today - timedelta(days=20))
    assert (old["encash_covered"], old["encash_empty"], old["encash_unverified"]) == (0, 1, 1)
    recent = _day(data, today)
    assert (recent["encash_covered"], recent["encash_unverified"]) == (1, 1)
    assert client.get("/api/data-console/coverage/day", params={"day": old["day"]}).json()["atm_ids"] == [2]

    # более длинный период банкомат 1 ещё не покрыт
    assert client.get("/api/data-console/coverage", params={"days": 90}).json()["atm_status_counts"].get("shallow") == 1

    encashments.sync_encashments([2], days=30)
    after = client.get("/api/data-console/coverage", params={"days": 30}).json()
    assert after["atm_status_counts"] == {"ok": 2}
    assert after["days_with_encash_gaps"] == 0


def test_chain_gap_is_counted(client, monkeypatch):
    monkeypatch.setattr(settings, "ENCASHMENT_REQUEST_PAUSE_SEC", 0)
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "/x/{atm_id}")
    now = datetime.now(timezone.utc)
    cycles = [_cycle(1, now - timedelta(days=9), now - timedelta(days=7)),
              _cycle(2, now - timedelta(days=5), now - timedelta(days=3)),  # 2 дня разрыва
              _cycle(3, now - timedelta(days=3), None)]
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1,'A')")
    monkeypatch.setattr(encashments, "fetch_encashments", lambda atm_id: cycles)
    encashments.sync_encashments([1], days=30)
    atm = client.get("/api/data-console/coverage", params={"days": 30}).json()["atms"][0]
    assert atm["cycles"] == 3 and atm["chain_gaps"] == 1 and atm["open_cycles"] == 1 and atm["source_cycles"] == 3


def test_error_keeps_depth_and_marks_atm(client, monkeypatch):
    monkeypatch.setattr(settings, "ENCASHMENT_REQUEST_PAUSE_SEC", 0)
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "/x/{atm_id}")
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1,'A')")
    monkeypatch.setattr(encashments, "fetch_encashments", lambda atm_id: [])
    encashments.sync_encashments([1], days=30)

    def boom(atm_id):
        raise RuntimeError("503")

    monkeypatch.setattr(encashments, "fetch_encashments", boom)
    encashments.sync_encashments([1], days=30)
    atm = client.get("/api/data-console/coverage", params={"days": 30}).json()["atms"][0]
    assert atm["status"] == "error" and atm["last_error"] == "503" and atm["last_success_at"]
    assert data_console._ids_for_pull("missing", 30, None) == [1]


def test_stop_interrupts_sync(pg_db, monkeypatch):
    monkeypatch.setattr(settings, "ENCASHMENT_REQUEST_PAUSE_SEC", 0)
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "/x/{atm_id}")
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1,'A'), (2,'B'), (3,'C')")
    import threading

    stop = threading.Event()
    calls = []

    def fetch(atm_id):
        calls.append(atm_id)
        stop.set()
        return []

    monkeypatch.setattr(encashments, "fetch_encashments", fetch)
    stats = encashments.sync_encashments([1, 2, 3], stop=stop)
    assert calls == [1] and stats["stopped"] == 1


@pytest.fixture
def app_client(pg_db, monkeypatch):
    from test_upload_safety import _app

    with TestClient(_app(monkeypatch, True)) as c:
        yield c


def test_atm_card_collects_everything_for_one_atm(app_client, monkeypatch):
    monkeypatch.setattr(settings, "ENCASHMENT_REQUEST_PAUSE_SEC", 0)
    monkeypatch.setattr(settings, "ATM_ENCASHMENT_PATH", "/x/{atm_id}")
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute("INSERT INTO atms (id, tid) VALUES (1,'A'), (2,'B')")
    cycle = _cycle(10, now - timedelta(days=2), None)
    cycle["cashOutTotals"] = [{"id": "1", "number": 1, "type": "BILLCASSETTE", "values": 200000, "count": 5}]
    cycle["actions"] = [{"id": 5, "createdAt": now.isoformat(), "evtno": 1, "data": "TOP CASSETTE REMOVED"}]
    monkeypatch.setattr(encashments, "fetch_encashments", lambda atm_id: [cycle] if atm_id == 1 else [])
    encashments.sync_encashments([1, 2], days=30)

    # страница банкомата открывается без входа администратора, как и другие страницы с банкоматами
    card = app_client.get("/api/atm-monitor/card", params={"id": 1}).json()
    assert card["atm"]["tid"] == "A" and card["snapshot"] is None and card["sync"]["source_cycles"] == 1
    assert card["encashments"][0]["cassettes"][0]["nominal"] == 200000
    assert card["encashments"][0]["actions"][0]["data"] == "TOP CASSETTE REMOVED"
    assert app_client.get("/api/atm-monitor/card", params={"id": 2}).json()["encashments"] == []
    assert app_client.get("/api/atm-monitor/card", params={"id": 999}).status_code == 404
    assert [a["tid"] for a in app_client.get("/api/atm-monitor/card/search").json()] == ["A", "B"]


def test_atm_card_finds_atm_by_each_identifier(app_client, monkeypatch):
    import api.main as main

    with connect() as conn:
        conn.execute("""INSERT INTO atms (id, tid, terminal_id, last_seen_at) VALUES
            (10, 'DUP', 'T-OLD', now() - interval '1 day'),
            (11, 'DUP', 'T-NEW', now()),
            (12, 'OTHER', 'T-DIRECT', now())""")
    get = lambda **p: app_client.get("/api/atm-monitor/card", params=p)

    # дубль TID: самая свежая запись и ссылка на вторую
    by_tid = get(tid="DUP").json()
    assert by_tid["atm"]["id"] == 11 and [o["id"] for o in by_tid["same_tid"]] == [10]

    # реестр сопоставлен с TID: из дублей выбирается запись с тем же terminal_id
    monkeypatch.setattr(main, "get_atm", lambda t: {"live_tid": "DUP"})
    assert get(terminal="T-OLD").json()["atm"]["id"] == 10

    # реестр не сопоставлен, но terminal_id есть в BTech
    monkeypatch.setattr(main, "get_atm", lambda t: {"address": "ул. 1"})
    assert get(terminal="T-DIRECT").json()["atm"]["id"] == 12

    # в реестре есть, в BTech нет: понятное сообщение, а не чужой банкомат
    missing = get(terminal="NOPE")
    assert missing.status_code == 404 and "реестре" in missing.json()["detail"] and "ул. 1" in missing.json()["detail"]
    monkeypatch.setattr(main, "get_atm", lambda t: None)
    assert get(terminal="NOPE").status_code == 404


def test_pull_refuses_without_login(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_USERNAME", "")
    assert client.post("/api/data-console/pull", json={"scope": "all"}).status_code == 400
