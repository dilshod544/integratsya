"""Shared fixtures: every DB test gets a freshly created schema in a separate PostgreSQL test database."""
from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import atm_monitor.db as atm_monitor_db
from api.core import config, db
from atm_monitor.config import settings

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql://localhost:5432/bank_intelligence_test")


@pytest.fixture
def pg_db(tmp_path, monkeypatch):
    dbname = conninfo_to_dict(TEST_DATABASE_URL).get("dbname") or ""
    if not dbname.endswith("_test"):
        pytest.fail(f"TEST_DATABASE_URL must point to a *_test database (got {dbname!r}): the schema is wiped.")

    monkeypatch.setattr(config, "DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(settings, "DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)   # seeded admin password file goes here
    db.close_pool()
    atm_monitor_db.close_pool()
    db._probe_connection()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS atm_monitor CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    db.init_db()
    yield
    db.close_pool()
    atm_monitor_db.close_pool()


@pytest.fixture(autouse=True)
def no_real_btech_reports(monkeypatch):
    """Тесты не должны обращаться к настоящему BTech, даже если в .env есть логин."""
    from atm_monitor import encashments

    monkeypatch.setattr(encashments, "fetch_balance_history", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(encashments, "fetch_turnover_history", lambda *a, **k: [], raising=True)
