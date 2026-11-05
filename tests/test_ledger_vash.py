from datetime import date
from pathlib import Path

import openpyxl

from api.core.ledger_vash import (
    apply_ledger_to_branches,
    branch_ledger_analytics,
    parse_branch_ledger_xlsx,
    parse_vash_operations_xlsx,
    replace_branch_ledger,
    replace_vash_operations,
    vash_operations_analytics,
)
from api.core.db import connect


def _xlsx(path: Path, headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_ledger_filter_by_month_in_one_import(pg_db, tmp_path):
    path = tmp_path / "ledger-span.xlsx"
    _xlsx(path, ["oper_day", "coa_code", "code", "code_currency", "acc_id", "branch_id", "saldo_out"], [
        [date(2026, 9, 28), "10101", "20208000", "000", "A1", "10463", 111],
        [date(2026, 10, 1), "10101", "20208000", "000", "A1", "10463", 200],
        [date(2026, 10, 19), "10101", "20208000", "000", "A1", "10463", 500],
    ])
    replace_branch_ledger(parse_branch_ledger_xlsx(path)["records"], "span.xlsx")
    sept = branch_ledger_analytics(month="2026-09")
    assert sept["selected_day"] == "2026-09-28"
    assert sept["total_uzs"] == 111
    assert sept["filter_month"] == "2026-09"
    october = branch_ledger_analytics(month="2026-10")
    assert october["selected_day"] == "2026-10-19"
    assert october["total_uzs"] == 500
    one_day = branch_ledger_analytics(oper_day="2026-10-01")
    assert one_day["selected_day"] == "2026-10-01"
    assert one_day["total_uzs"] == 200
    assert one_day["filter_month"] is None
    periods = __import__("api.core.ledger_vash", fromlist=["ledger_import_periods"]).ledger_import_periods()
    assert [m["key"] for m in periods["months"]] == ["2026-09", "2026-10"]


def test_vash_filter_by_month_in_one_import(pg_db, tmp_path):
    path = tmp_path / "vash-span.xlsx"
    _xlsx(path, ["ci_acc", "co_acc", "obmen_punkt", "branch", "date"], [
        ["CI", "CO", "12", "10463", date(2026, 9, 2)],
        ["CI", "CO", "12", "10463", date(2026, 9, 14)],
        ["CI", "CO", "99", "11000", date(2026, 10, 1)],
    ])
    replace_vash_operations(parse_vash_operations_xlsx(path)["records"], "span.xlsx")
    sept = vash_operations_analytics(month="2026-09")
    assert sept["operations"] == 2
    assert sept["filter_month"] == "2026-09"
    assert sept["from"] == "2026-09-02" and sept["to"] == "2026-09-14"
    october = vash_operations_analytics(month="2026-10")
    assert october["operations"] == 1
    assert next(p for p in october["points"] if p["obmen_punkt"] == "99")["ops"] == 1
    one_day = vash_operations_analytics(oper_day="2026-09-02")
    assert one_day["operations"] == 1
    assert one_day["filter_day"] == "2026-09-02"


def test_ledger_latest_day_keeps_limits(pg_db, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _xlsx(path, ["oper_day", "coa_code", "code", "code_currency", "acc_id", "branch_id", "saldo_out"], [
        [date(2026, 10, 1), "10101", "20208000", "000", "A1", "10463", 100],
        [date(2026, 10, 19), "10101", "20208000", "000", "A1", "10463", 500],
        [date(2026, 10, 19), "10101", "20208840", "840", "A2", "10463", 20],
        [date(2026, 10, 19), "10101", "20208000", "000", "B1", "11000", 70],
    ])
    parsed = parse_branch_ledger_xlsx(path)
    assert len(parsed["records"]) == 4
    stats = replace_branch_ledger(parsed["records"])
    assert stats["saved"] == 4 and stats["branches"] == 2

    # Ikkinchi yuklash alohida import. Analitika shuni ochadi, ikkalasini qo'shmaydi.
    replace_branch_ledger(parsed["records"])
    data = branch_ledger_analytics()
    assert data["latest_day"] == "2026-10-19"
    assert data["rows"] == 3
    assert data["total_uzs"] == 570
    branch = next(b for b in data["branches"] if b["branch_id"] == "10463")
    assert branch["uzs"] == 500

    target = [{"local_code": "10463", "cash": {"limit_uzs": 1000, "limit_usd": 50, "balance_uzs": 1}}]
    apply_ledger_to_branches(target)
    assert target[0]["cash"]["balance_uzs"] == 500
    assert target[0]["cash"]["limit_uzs"] == 1000
    assert target[0]["cash"]["limit_usd"] == 50
    assert target[0]["cash"]["ledger_as_of"] == "2026-10-19"
    usd = next(c for c in target[0]["cash"]["currencies"] if c["iso"] == "840")
    assert usd["amount"] == 20


def test_vash_bank_headers_and_us_dates(tmp_path):
    path = tmp_path / "bank.xlsx"
    _xlsx(path, ["CL_ACC", "CO_ACC", "OBMEN_PUNKT", "BRANCH_ID", "CURR_DAY"], [
        ["10101", "17101", "N06003049", 10759, "9/2/2026"],
        ["10101", "17101", "N06003049", 10759, "9/14/2026"],
        ["10101", "17101", None, 11085, "9/2/2026"],
        ["10101", "17101", "15", 10463, "15/9/2026"],
    ])
    parsed = parse_vash_operations_xlsx(path)
    assert [r["op_date"] for r in parsed["records"]] == [
        date(2026, 9, 2), date(2026, 9, 14), date(2026, 9, 15),
    ]
    assert parsed["records"][0]["obmen_punkt"] == "N06003049"
    assert parsed["records"][0]["branch"] == "10759"
    assert parsed["records"][0]["ci_acc"] == "10101"
    assert len(parsed["errors"]) == 1


def test_vash_operations_rank_and_match_zone(pg_db, tmp_path):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO vash_zones (vash_number, bxm_code, address, lat, lon)
            VALUES ('12', '10463', 'Юнусобод', 41.3, 69.2)
            """
        )
    path = tmp_path / "vash.xlsx"
    rows = [["CI-1", "CO-1", "12", "10463", date(2026, 10, 19)]] * 8
    rows += [["CI-2", "CO-2", "99", "11000", date(2026, 10, 19)]]
    _xlsx(path, ["ci_acc", "co_acc", "obmen_punkt", "branch", "date"], rows)
    parsed = parse_vash_operations_xlsx(path)
    assert replace_vash_operations(parsed["records"])["points"] == 2
    data = vash_operations_analytics()
    assert data["operations"] == 9
    top = data["points"][0]
    assert top["obmen_punkt"] == "12"
    assert top["ops"] == 8
    assert top["on_map"] is True
    assert top["band"] == "high"
    quiet = next(p for p in data["points"] if p["obmen_punkt"] == "99")
    assert quiet["on_map"] is False
    assert quiet["band"] == "low"


def test_vash_n_prefix_matches_zone_coordinates(pg_db, tmp_path):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO vash_zones (vash_number, bxm_code, address, lat, lon)
            VALUES ('06003041', '10737', 'Бухоро', 39.77, 64.42),
                   ('06003099', '10737', 'Бухоро 2', 39.78, 64.43)
            """
        )
    path = tmp_path / "vash-n.xlsx"
    _xlsx(path, ["CL_ACC", "CO_ACC", "OBMEN_PUNKT", "BRANCH_ID", "CURR_DAY"], [
        ["10101", "17101", "N06003041", 10737, "9/2/2026"],
    ])
    parsed = parse_vash_operations_xlsx(path)
    replace_vash_operations(parsed["records"])
    point = vash_operations_analytics()["points"][0]
    assert point["obmen_punkt"] == "N06003041"
    assert point["on_map"] is True
    assert point["vash_number"] == "06003041"
    assert point["address"] == "Бухоро"
    assert point["lat"] == 39.77
    quiet = next(p for p in vash_operations_analytics()["points"] if p["band"] == "none")
    assert quiet["vash_number"] == "06003099"
    assert quiet["ops"] == 0


def test_numbered_imports_stay_separate(pg_db, tmp_path):
    from api.core.ledger_vash import (
        delete_branch_ledger_import,
        delete_vash_operation_import,
        list_branch_ledger_imports,
        list_vash_operation_imports,
    )

    first = tmp_path / "ledger-1.xlsx"
    _xlsx(first, ["oper_day", "coa_code", "code", "code_currency", "acc_id", "branch_id", "saldo_out"], [
        [date(2026, 10, 1), "10101", "20208000", "000", "A1", "10463", 100],
    ])
    second = tmp_path / "ledger-2.xlsx"
    _xlsx(second, ["oper_day", "coa_code", "code", "code_currency", "acc_id", "branch_id", "saldo_out"], [
        [date(2026, 10, 2), "10101", "20208000", "000", "A1", "10463", 900],
    ])
    a = replace_branch_ledger(parse_branch_ledger_xlsx(first)["records"], "ledger-1.xlsx")
    b = replace_branch_ledger(parse_branch_ledger_xlsx(second)["records"], "ledger-2.xlsx")
    assert a["label"] == "1-й импорт" and b["label"] == "2-й импорт"
    assert [i["seq"] for i in list_branch_ledger_imports()] == [1, 2]
    assert branch_ledger_analytics()["total_uzs"] == 900
    assert branch_ledger_analytics(import_id=a["import_id"])["total_uzs"] == 100
    assert delete_branch_ledger_import(b["import_id"]) is True
    assert branch_ledger_analytics()["total_uzs"] == 100

    v1 = tmp_path / "vash-1.xlsx"
    _xlsx(v1, ["ci_acc", "co_acc", "obmen_punkt", "branch", "date"], [
        ["CI", "CO", "12", "10463", date(2026, 10, 1)],
    ])
    v2 = tmp_path / "vash-2.xlsx"
    _xlsx(v2, ["ci_acc", "co_acc", "obmen_punkt", "branch", "date"], [
        ["CI", "CO", "99", "11000", date(2026, 10, 2)],
        ["CI", "CO", "99", "11000", date(2026, 10, 3)],
    ])
    one = replace_vash_operations(parse_vash_operations_xlsx(v1)["records"], "vash-1.xlsx")
    two = replace_vash_operations(parse_vash_operations_xlsx(v2)["records"], "vash-2.xlsx")
    assert [i["label"] for i in list_vash_operation_imports()] == ["1-й импорт", "2-й импорт"]
    opened = vash_operations_analytics()
    assert opened["operations"] == 2
    assert opened["points"][0]["obmen_punkt"] == "99"
    assert vash_operations_analytics(one["import_id"])["operations"] == 1
    assert delete_vash_operation_import(two["import_id"]) is True
    assert vash_operations_analytics()["operations"] == 1


def test_rows_already_in_db_stay_before_new_import(pg_db, tmp_path):
    from datetime import datetime, timezone

    from api.core.dataset_imports import (
        KIND_VASH,
        adopt_unassigned,
        list_dataset_imports,
        restore_original_import_time,
    )
    from api.core.db import connect

    original = datetime(2026, 10, 21, 15, 34, 22, tzinfo=timezone.utc)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO vash_operations (ci_acc, co_acc, obmen_punkt, branch, op_date, imported_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            ("CI", "CO", "12", "10463", date(2026, 9, 2), original),
        )
        adopt_unassigned(conn, KIND_VASH, "vash_operations", "op_date")
        conn.execute("UPDATE dataset_imports SET imported_at = now() WHERE kind = %s", (KIND_VASH,))
        restore_original_import_time(conn, KIND_VASH, "vash_operations")
    previous = list_dataset_imports(KIND_VASH)
    assert [i["label"] for i in previous] == ["1-й импорт"]
    assert previous[0]["rows_count"] == 1
    assert previous[0]["imported_at"].startswith("2026-10-21")
    assert previous[0]["filename"] is None

    path_new = tmp_path / "new.xlsx"
    _xlsx(path_new, ["ci_acc", "co_acc", "obmen_punkt", "branch", "date"], [
        ["CI", "CO", "99", "11000", date(2026, 10, 2)],
        ["CI", "CO", "99", "11000", date(2026, 10, 3)],
    ])
    fresh = replace_vash_operations(parse_vash_operations_xlsx(path_new)["records"], "new.xlsx")
    listed = list_dataset_imports(KIND_VASH)
    assert [i["label"] for i in listed] == ["1-й импорт", "2-й импорт"]
    assert listed[0]["id"] == previous[0]["id"]
    assert fresh["label"] == "2-й импорт"
    opened = vash_operations_analytics()
    assert opened["import"]["seq"] == 2
    assert opened["operations"] == 2
    kept = vash_operations_analytics(previous[0]["id"])
    assert kept["operations"] == 1
    assert kept["points"][0]["obmen_punkt"] == "12"
