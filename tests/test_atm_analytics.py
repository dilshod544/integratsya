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


def test_export_atm_status_xlsx(pg_db):
    from io import BytesIO

    from openpyxl import load_workbook

    _seed()
    atm_analytics.invalidate_cache()
    ids = [a["atm_id"] for a in atm_analytics.network_status()["atms"]]
    raw = atm_analytics.export_atm_status_xlsx(
        ids, region="Сурхондарё вилояти", filter_label="Все банкоматы", search=None
    )
    assert raw[:2] == b"PK"
    wb = load_workbook(BytesIO(raw))
    assert wb.sheetnames[0] == "Банкоматы"
    assert wb["Банкоматы"].max_row == 1 + len(ids)
    assert wb["Банкоматы"]["A1"].value == "TID"
    assert wb["Сводка"]["B5"].value == "Сурхондарё вилояти"
    try:
        atm_analytics.export_atm_status_xlsx([])
        assert False, "empty export must fail"
    except ValueError:
        pass


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


def test_assess_stock_norm():
    hour = 1_000_000  # 24 млн в сутки
    over = atm_analytics.assess_stock(1_000e6, hour, 720, 30)
    assert over["status"] == "excess" and over["target"] == 720e6 and over["excess"] == 280e6
    assert atm_analytics.assess_stock(50e6, hour, 720, 30)["status"] == "low"
    assert atm_analytics.assess_stock(300e6, hour, 720, 30)["status"] == "ok"
    idle = atm_analytics.assess_stock(200e6, 0.0, 720, 30)
    assert idle["status"] == "no_demand" and idle["excess"] == 200e6
    assert atm_analytics.assess_stock(200e6, hour, 10, 30)["status"] == "no_data"


def test_efficiency_and_balance_history(pg_db):
    _seed()
    atm_analytics.invalidate_cache()
    eff = atm_analytics.atm_efficiency(7, 30)
    assert eff["summary"]["atms"] == 4
    a = eff["atms"][0]
    # Остаток падает на 1 млн в час.
    assert round(a["avg_per_hour"] / 1e6) == 1 and a["status"] in {"low", "ok"}
    assert len(eff["hour_profile"]) == 24
    assert eff["cap_uzs"] == atm_analytics.CAP_UZS
    assert "over_cap" in eff["summary"] and "over_cap_candidate" in a
    assert a["segment"] in {"over_cap", "high_demand", "cap_ok", "currency"}

    net = atm_analytics.balance_history(None, 7)
    one = atm_analytics.balance_history(a["atm_id"], 7)
    assert net["points"] and one["points"]
    assert net["points"][-1]["v"] > one["points"][-1]["v"]


def test_efficiency_over_cap_export(pg_db):
    _seed()
    atm_analytics.invalidate_cache()
    eff = atm_analytics.atm_efficiency(7, 30)
    ids = [a["atm_id"] for a in eff["atms"][:2]]
    blob = atm_analytics.export_atm_efficiency_xlsx(ids, days=7, norm_days=30, filter_label="test")
    assert blob[:2] == b"PK"


def test_atm_history_full(pg_db):
    _seed()
    atm_analytics.invalidate_cache()
    atm_id = atm_analytics.atm_efficiency(7, 30)["atms"][0]["atm_id"]
    h = atm_analytics.atm_history(atm_id)
    assert h["from"] and h["points"] and h["daily"]
    assert all(d["withdrawal"] >= 0 for d in h["daily"])
    assert atm_analytics.atm_history(-1)["points"] == []


def test_period_demand_extrapolates_short_history():
    # 60 ч наблюдений, 2 млн/ч: за 30 дней это ~1,44 млрд, а не 120 млн «сняли за период».
    demand = atm_analytics.estimate_period_demand(2_000_000, 60, 30)
    assert demand == 2_000_000 * 24 * 30
    assert demand > atm_analytics.CAP_UZS
    # Меньше суток наблюдений — спрос не оцениваем.
    assert atm_analytics.estimate_period_demand(2_000_000, 10, 30) is None
    assert atm_analytics.estimate_period_demand(None, 100, 30) is None
