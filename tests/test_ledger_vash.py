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

    # Повтор той же даты заменяет строки, а не дублирует.
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
            VALUES ('06003041', '10737', 'Бухоро', 39.77, 64.42)
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
