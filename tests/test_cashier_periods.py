"""Monthly KPI imports, cross-script search and filter consistency (temporary database)."""
from __future__ import annotations

import json
from datetime import date

import pytest

from api.core import db as core_db
from api.core.cashier import analytics, repository
from api.core.cashier.helpers import (
    clean_branch_name,
    detect_period,
    fix_mixed_script,
    normalize_period,
    period_label,
    search_fold,
)


def _record(name, branch="Бунёдкор БХМ", position="Universal kassir", bek=0, front=0,
            note="", status="active"):
    return {
        "tab_number": name[:3], "full_name": name, "position": position,
        "days_worked": 20, "std_days": 22,
        "operations_count": bek + front, "operations_minutes": (bek + front) * 2,
        "bek_count": bek, "front_count": front, "load_percent": 50,
        "branch_name": branch, "raw_note": note, "hr_status_code": status,
        "hr_status_label": status, "metrics": {},
    }


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(core_db, "DB_PATH", tmp_path / "bank.db")
    monkeypatch.setattr(repository, "_tables_initialized", False)
    yield
    repository._tables_initialized = False


def _july():
    return {"records": [
        _record("ALIYEVA MALIKA", bek=100, front=10),
        _record("KARIMOVA NODIRA", front=200, bek=5, branch="Tошкент шаҳар БХО"),
        _record("TEMP ISHCHI", bek=50, note="(вакт.)", status="temporary"),
        _record("DEKRET AYOL", note="(декрет2)", status="maternity"),
        _record("YOLG'IZ DEKRET", branch="Xонобод БХМ", note="(декрет2)", status="maternity"),
        _record("SOATOV KASAL", note="(касал)", status="sick"),
        _record("MUDIR BOSHLIQ", position="Kassa mudiri"),
    ]}


def test_period_helpers():
    today = date(2026, 10, 6)
    assert detect_period("Kassirlar iyul.xlsx", today) == "2026-07"
    assert detect_period("Кассирлар август 2026", today) == "2026-08"
    assert detect_period("kpi_2026-09", today) == "2026-09"
    assert detect_period("отчет 09.2026", today) == "2026-09"
    assert detect_period("Kassirlar noyabr", today) == "2025-11"
    assert detect_period("Kassirlar-2", today) is None
    assert detect_period("maydon", today) is None
    assert normalize_period("2026-13") is None
    assert period_label("2026-07") == "Июль 2026"


def test_cross_script_search_and_branch_cleaning():
    assert fix_mixed_script("Tошкент шаҳар") == "Тошкент шаҳар"
    assert fix_mixed_script("Yunusobod BXM") == "Yunusobod BXM"
    assert clean_branch_name("11204 - Жиззах  БХО") == "Жиззах БХО"
    assert search_fold("Тошкент") == search_fold("Tошкент") == search_fold("Toshkent")
    assert search_fold("Мухтарова") in search_fold("MUXTAROVA OYSUDAXON")
    assert search_fold("Жўрақулова") == search_fold("JO`RAQULOVA")


def test_months_are_kept_separately_and_replaced_by_period(temp_db):
    jul = repository.save_cashier_import("Kassirlar-2.xlsx", _july(), "2026-07")
    aug = repository.save_cashier_import("Kassirlar avgust.xlsx", {"records": _july()["records"][:3]}, "2026-08")
    assert jul["replaced_previous"] is False

    imports = repository.list_cashier_imports()
    assert [i["period"] for i in imports] == ["2026-08", "2026-07"]
    assert [i["period_label"] for i in imports] == ["Август 2026", "Июль 2026"]

    assert analytics.cashier_analytics()["import"]["id"] == aug["import_id"]
    assert analytics.cashier_analytics(import_id=jul["import_id"])["total"] == 7
    assert analytics.cashier_analytics(import_id=aug["import_id"])["total"] == 3

    again = repository.save_cashier_import("Kassirlar avgust v2.xlsx", {"records": _july()["records"][:2]}, "2026-08")
    assert again["replaced_previous"] is True
    imports = repository.list_cashier_imports()
    assert len(imports) == 2
    assert analytics.cashier_analytics(import_id=again["import_id"])["total"] == 2
    assert analytics.cashier_analytics(import_id=jul["import_id"])["total"] == 7

    assert repository.delete_cashier_import(again["import_id"]) is True
    assert [i["period"] for i in repository.list_cashier_imports()] == ["2026-07"]
    with core_db._connect() as c:
        left = c.execute("SELECT COUNT(*) FROM cashier_reports").fetchone()[0]
    assert left == 7


def test_save_requires_valid_period(temp_db):
    with pytest.raises(ValueError):
        repository.save_cashier_import("x.xlsx", _july(), "2026-7")


def test_legacy_import_gets_period_from_import_date(temp_db):
    with core_db._connect() as c:
        c.execute("CREATE TABLE cashier_imports (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, "
                  "imported_at TEXT NOT NULL, rows_count INTEGER NOT NULL, report_label TEXT)")
        c.execute("INSERT INTO cashier_imports(filename, imported_at, rows_count) "
                  "VALUES('Kassirlar-2.xlsx', '2026-08-13T18:21:03+00:00', 0)")
    assert [i["period"] for i in repository.list_cashier_imports()] == ["2026-07"]


def test_filters_partition_and_lists_ignore_filters(temp_db):
    iid = repository.save_cashier_import("jul.xlsx", _july(), "2026-07")["import_id"]
    full = analytics.cashier_analytics(import_id=iid, page_size=200)
    assert full["total"] == 7

    roles = {r: analytics.cashier_analytics(import_id=iid, role=r)["total"] for r in ("back", "front", "none")}
    assert sum(roles.values()) == 7
    assert roles["front"] == 1

    statuses = ("active", "vacation", "temporary", "sick", "vacant")
    counted = sum(analytics.cashier_analytics(import_id=iid, status=s)["total"] for s in statuses)
    maternity_all = sum(1 for x in full["cashiers"] if x["hr_status_code"] == "maternity")
    assert counted + maternity_all == 7
    assert analytics.cashier_analytics(import_id=iid, status="sick")["total"] == 1

    filtered = analytics.cashier_analytics(import_id=iid, branch="Бунёдкор БХМ", role="front")
    assert filtered["branches"] == full["branches"]
    assert filtered["positions"] == full["positions"]
    assert "Тошкент шаҳар БХО" in full["branches"]
    assert "Хонобод БХМ" in full["branches"]


def test_maternity_filter_excludes_uncovered_and_card_matches(temp_db):
    iid = repository.save_cashier_import("jul.xlsx", _july(), "2026-07")["import_id"]
    d = analytics.cashier_analytics(import_id=iid)
    s = d["summary"]
    mat = analytics.cashier_analytics(import_id=iid, status="maternity")
    norep = analytics.cashier_analytics(import_id=iid, status="no_replacement")
    assert mat["total"] == s["maternity_cashiers"] == 1
    assert all(x["has_replacement"] for x in mat["cashiers"])
    assert s["maternity_no_replacement"] == 1
    assert norep["total"] == s["no_replacement_cashiers"]


def test_search_works_across_scripts(temp_db):
    iid = repository.save_cashier_import("jul.xlsx", _july(), "2026-07")["import_id"]
    for q in ("Тошкент шаҳар", "Tошкент", "toshkent", "Каримова", "karimova"):
        d = analytics.cashier_analytics(import_id=iid, search=q)
        assert [x["full_name"] for x in d["cashiers"]] == ["KARIMOVA NODIRA"], q
    assert analytics.cashier_analytics(import_id=iid, search="Хонобод")["total"] == 1


def test_detail_matches_list(temp_db):
    iid = repository.save_cashier_import("jul.xlsx", _july(), "2026-07")["import_id"]
    rows = {x["full_name"]: x for x in analytics.cashier_analytics(import_id=iid, page_size=200)["cashiers"]}
    for name in ("DEKRET AYOL", "YOLG'IZ DEKRET", "TEMP ISHCHI"):
        detail = analytics.cashier_detail(rows[name]["id"])
        for key in ("hr_status_code", "cashier_type", "replaced_by_full_name", "replacing_full_name", "has_replacement"):
            assert detail[key] == rows[name][key], (name, key)
        assert detail["period_label"] == "Июль 2026"


def test_registry_vacancies_and_namesakes(temp_db):
    iid = repository.save_cashier_import("jul.xlsx", {"records": [
        _record("ALIYEVA MALIKA", bek=10, branch="Бунёдкор БХМ"),
        _record("ALIYEVA MALIKA", bek=10, branch="Хонобод БХМ"),
        _record("ВАКАНСИЯ", branch="Бунёдкор БХМ"),
    ]}, "2026-07")["import_id"]
    repository.save_cashier_status_import("shtat.xlsx", {"records": [
        {"full_name": "ALIYEVA MALIKA", "branch_name": "Хонобод БХМ", "position": "Universal kassir",
         "status_code": "vacation", "status_label": "🔵 В отпуске"},
        {"full_name": "Вакант 1", "branch_name": "Бунёдкор БХМ", "position": "Universal kassir",
         "status_code": "vacant", "status_label": "⚪ Вакансия"},
        {"full_name": "Вакант 2", "branch_name": "Жиззах БХО", "position": "Universal kassir",
         "status_code": "vacant", "status_label": "⚪ Вакансия"},
    ]})
    d = analytics.cashier_analytics(import_id=iid, page_size=200)
    by_branch = {(x["full_name"], x["branch_name"]): x for x in d["cashiers"]}
    assert by_branch[("ALIYEVA MALIKA", "Хонобод БХМ")]["hr_status_code"] == "vacation"
    assert by_branch[("ALIYEVA MALIKA", "Бунёдкор БХМ")]["hr_status_code"] == "active"
    vacancies = sorted(x["branch_name"] for x in d["cashiers"] if x["hr_status_code"] == "vacant")
    assert vacancies == ["Бунёдкор БХМ", "Жиззах БХО"]
