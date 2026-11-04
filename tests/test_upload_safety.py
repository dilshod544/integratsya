"""Загрузка файлов: имя от клиента не выводит запись за пределы временной папки,
битый файл — 400, а не 500."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


def _app(monkeypatch, auth_disabled: bool):
    """api/main.py импортирует модули как core.* (не api.core.*) — это отдельные
    объекты модулей, поэтому адрес тестовой базы подменяем и в них."""
    import api.main as main
    import core.config as core_config
    import core.db as core_db
    TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql://localhost:5432/bank_intelligence_test")

    monkeypatch.setattr(core_config, "DATABASE_URL", TEST_DATABASE_URL)
    core_db.close_pool()
    monkeypatch.setattr(main, "AUTH_DISABLED", auth_disabled)
    monkeypatch.setattr(main.atm_collector, "start_background", lambda: None)
    monkeypatch.setattr(main.atm_forecast, "start_background", lambda: None)
    return main.app


@pytest.fixture
def client(pg_db, monkeypatch):
    with TestClient(_app(monkeypatch, True)) as c:
        yield c


@pytest.mark.parametrize("endpoint", [
    "/api/atms/import", "/api/branches/import", "/api/sqb-rates/import",
    "/api/branches/balances/import", "/api/cash-equipment/import", "/api/vash/import",
    "/api/branch-ledger/import", "/api/vash/operations/import",
])
def test_traversal_filename_stays_in_temp_dir(client, endpoint):
    probe = os.path.join(tempfile.gettempdir(), "escaped_upload_probe.xlsx")
    if os.path.exists(probe):
        os.remove(probe)
    r = client.post(endpoint, files={"file": ("../escaped_upload_probe.xlsx", b"not an xlsx", "application/octet-stream")})
    assert r.status_code == 400, r.text
    assert not os.path.exists(probe)


def test_auth_gate_blocks_api_when_enabled(pg_db, monkeypatch):
    with TestClient(_app(monkeypatch, False)) as c:
        assert c.get("/api/atms").status_code == 401
        assert c.get("/api/health").status_code == 200
        r = c.get("/dashboard/map.html", follow_redirects=False)
        assert r.status_code == 303 and "login.html" in r.headers["location"]
