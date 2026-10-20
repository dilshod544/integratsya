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
