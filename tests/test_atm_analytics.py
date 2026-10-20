from datetime import datetime, timedelta, timezone

from api.core import atm_analytics
from atm_monitor import forecast
from atm_monitor.db import connect

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def _snap(**kw):
    base = {
        "polled_at": NOW, "agent_status": "online", "service_status": "InService", "app_status": "InService",
        "cdm_total_uzs": 300e6, "last_transaction_cash_out": NOW - timedelta(minutes=5),
        "dispenser_status": "ONLINE", "card_reader_status": "BUSY", "printer_status": "NODEVICE",
    }
    base.update(kw)
    return base


def test_classify_operational_states():
    cap = 400e6
    assert atm_analytics.classify(_snap(), NOW, cap)["operational"] == "working"
    assert atm_analytics.classify(_snap(app_status="CustomerSession"), NOW, cap)["operational"] == "busy"
    oos = atm_analytics.classify(_snap(service_status="OutOfService", app_status="Supervisor"), NOW, cap)
    assert oos["operational"] == "out_of_service" and oos["out_of_service_reason"] == "Режим обслуживания"
    assert atm_analytics.classify(_snap(agent_status="disconnected"), NOW, cap)["operational"] == "offline"
    stale = _snap(polled_at=NOW - timedelta(hours=5))
    assert atm_analytics.classify(stale, NOW, cap)["operational"] == "no_data"


def test_classify_cash_faults_idle():
    cap = 400e6
    empty = atm_analytics.classify(_snap(cdm_total_uzs=0), NOW, cap)
    assert empty["cash"] == "empty" and not empty["can_dispense"]
    assert atm_analytics.classify(_snap(cdm_total_uzs=50e6), NOW, cap)["cash"] == "critical"
    assert atm_analytics.classify(_snap(cdm_total_uzs=120e6), NOW, cap)["cash"] == "warning"
    ok = atm_analytics.classify(_snap(), NOW, cap)
    assert ok["cash"] == "ok" and ok["faults"] == [] and ok["can_dispense"] and not ok["idle"]

    broken = atm_analytics.classify(_snap(dispenser_status="HWERROR", printer_status="HWERROR"), NOW, cap)
    assert {f["device"] for f in broken["faults"]} == {"dispenser", "printer"}
    assert not broken["can_dispense"]

    # 70 млрд в одном ATM физически невозможны — это ошибка данных BTech, а не «норма».
    assert atm_analytics.classify(_snap(cdm_total_uzs=70e9), NOW, cap)["cash"] == "anomaly"

    idle = atm_analytics.classify(_snap(last_transaction_cash_out=NOW - timedelta(hours=30)), NOW, cap)
    assert idle["idle"]


def _seed():
    now = datetime.now(timezone.utc)
    states = [
        (1, "T1", "InService", "InService", "online", 300e6, "ONLINE"),
        (2, "T2", "InService", "CustomerSession", "online", 100e6, "ONLINE"),
        (3, "T3", "OutOfService", "NotRunning", "online", 0, "ONLINE"),
        (4, "T4", "InService", "InService", "disconnected", 200e6, "HWERROR"),
    ]
    with connect() as conn:
        for atm_id, tid, svc, app, agent, bal, disp in states:
            conn.execute("INSERT INTO atms (id, tid, address) VALUES (%s, %s, 'addr')", (atm_id, tid))
            for h in range(48, -1, -1):
                snap_id = conn.execute(
                    """
                    INSERT INTO atm_snapshots (atm_id, polled_at, agent_status, service_status, app_status,
                        cdm_total_uzs, dispenser_status, last_transaction_cash_out)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (atm_id, now - timedelta(hours=h), agent, svc, app, bal + h * 1e6, disp, now),
                ).fetchone()["id"]
            conn.execute(
                """
                INSERT INTO cassette_snapshots (atm_snapshot_id, atm_id, polled_at, cassette_index,
                    cassette_type, status, count, currency, nominal)
                VALUES (%s,%s,%s,1,'BILLCASSETTE','OK',100,'UZS',200000)
                """,
                (snap_id, atm_id, now),
            )


def test_network_status_summary(pg_db):
    _seed()
    atm_analytics.invalidate_cache()
    data = atm_analytics.network_status()
    s = data["summary"]
    assert s["total"] == 4
    assert (s["working"], s["busy"], s["out_of_service"], s["offline"]) == (1, 1, 1, 1)
    assert s["cash_empty"] == 1 and s["cash_critical"] == 0 and s["cash_warning"] == 1
    assert s["dispenser_faults"] == 1
    assert s["can_dispense"] == 2
    assert data["by_region"][0]["region"] == "Не сопоставлен с реестром"
    assert data["cash_by_nominal"] == [{"nominal": 200000, "amount": 4 * 100 * 200000}]


def test_forecast_and_withdrawals_endpoints(pg_db):
    _seed()
    atm_analytics.invalidate_cache()
    assert atm_analytics.forecast_overview()["run"] is None

    assert forecast.run_forecast() == 4
    atm_analytics.invalidate_cache()
    fc = atm_analytics.forecast_overview()
    assert fc["run"]["status"] == "success" and fc["summary"]["atms"] == 4
    assert len(fc["daily"]) == forecast.settings.FORECAST_DAYS
    # Остаток падает на 1 млн в час — 24 млн в сутки.
    assert round(fc["summary"]["withdrawal_24h"] / 4 / 1e6) == 24

    hist = atm_analytics.withdrawals_history(7)
    assert hist["total"] > 0 and hist["top_atms"]
