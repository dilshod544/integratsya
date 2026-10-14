"""Tests for the ВАШ (валюта айирбошлаш шохобчалари) parser and branch grouping."""
from pathlib import Path

import openpyxl
import pytest

from api.core.db import bulk_insert_branches
from api.core.vash import (
    _resolve_header,
    parse_location,
    parse_vash_xlsx,
    replace_vash_zones,
    vash_by_branch,
)

BRANCHES = [
    {"local_code": "10463", "region": "Юнусобод тумани", "address": "Юнусобод БХМ. 100084 Тошкент шаҳар",
     "lat": 41.36, "lon": 69.28, "incassation": 1},
    {"local_code": "10853", "region": "Бектемир тумани", "address": "Тўқимачилик БХМ. 100000 Тошкент шаҳар",
     "lat": 41.21, "lon": 69.33, "incassation": 0},
]


def test_parse_location_formats():
    assert parse_location("41.311081, 69.240562")[("lat")] == 41.311081
    p = parse_location(41.311081, 69.240562)
    assert (p["lat"], p["lon"], p["swapped"]) == (41.311081, 69.240562, False)
    p = parse_location("69.240562 41.311081")
    assert (p["lat"], p["lon"], p["swapped"]) == (41.311081, 69.240562, True)
    p = parse_location("41,311081; 69,240562")
    assert (p["lat"], p["lon"]) == (41.311081, 69.240562)
    p = parse_location("https://www.google.com/maps/place/X/@41.3201,69.2502,17z/data=!3m1")
    assert (p["lat"], p["lon"]) == (41.3201, 69.2502)
    p = parse_location("https://yandex.uz/maps/?ll=69.3301%2C41.2105&z=16")
    assert (p["lat"], p["lon"], p["swapped"]) == (41.2105, 69.3301, False)
    p = parse_location("41°18'39.9\"N 69°14'26.0\"E")
    assert p["lat"] == pytest.approx(41.311083, abs=1e-5) and p["lon"] == pytest.approx(69.240556, abs=1e-5)
    assert parse_location("55.75, 37.62")["lat"] is None
    assert "вне Узбекистана" in parse_location("55.75, 37.62")["error"]
    assert "Короткая ссылка" in parse_location("https://maps.app.goo.gl/abc")["error"]
    assert parse_location(None, "")["raw"] is None
    assert parse_location("41.31, 69.24")["coarse"] is True


def test_header_recognition():
    cases = {
        "№": "row_no", "Т/р": "row_no", "№ п/п": "row_no",
        "ВАШ рақами": "vash_number", "VASH raqami": "vash_number", "ВАШ №": "vash_number",
        "БХМ коди": "bxm_code", "Код БXM": "bxm_code", "Филиал коди": "bxm_code", "Локал код": "bxm_code",
        "БХМ номи": "bxm_name", "BXO/BXM nomi": "bxm_name", "Филиал номи": "bxm_name",
        "Манзил": "address", "ВАШ манзили": "address", "Адрес": "address",
        "Локация": "location", "Lat, Long": "location", "Координаты": "location", "Геолокация": "location",
        "Кенглик": "lat", "Latitude": "lat", "Узунлик": "lon", "Долгота": "lon",
        "ВАШ сони": "vash_count",
    }
    for text, field in cases.items():
        assert _resolve_header(text) == field, text


def _offset_layout(path: Path) -> None:
    """Table starts at C3 under a merged title; two-row header with merged «Локация»;
    БХМ code/name merged over three ВАШ rows; numbering row, blank row, «Жами», a duplicate."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ВАШ"
    ws["B1"] = "Ўзсаноатқурилишбанк ВАШлари рўйхати"
    ws.merge_cells("B1:I1")
    for col, text in zip("CDEFG", ["№", "ВАШ рақами", "БХМ коди", "БХМ номи", "Манзил"]):
        ws[f"{col}3"] = text
        ws.merge_cells(f"{col}3:{col}4")
    ws["H3"] = "Локация"
    ws.merge_cells("H3:I3")
    ws["H4"], ws["I4"] = "Кенглик", "Узунлик"
    for i, col in enumerate("CDEFGHI", start=1):
        ws[f"{col}5"] = i
    rows = {
        6: [1, 101, 10463, "Юнусобод БХМ", "Амир Темур 1", 41.360012, 69.280034],
        7: [2, 102, None, None, "Ц-4, 12", 41.361145, 69.289921],
        8: [3, 103, None, None, "Шаҳрисабз 5", 69.270111, 41.370222],     # lon/lat swapped
        10: [4, "201", "10853", "Тўқимачилик БХМ", "Бектемир", "41.2105", "69.3301"],
        11: [5, "102", "10853", "Тўқимачилик БХМ", "Бектемир 2", 41.21, 69.33],   # duplicate ВАШ 102
        12: [6, 301, None, None, "Номаълум", 41.1, 69.1],                  # nothing to link to
        13: [None, None, None, "Жами", None, None, None],
    }
    for r, values in rows.items():
        for col, v in zip("CDEFGHI", values):
            ws[f"{col}{r}"] = v
    ws.merge_cells("E6:E8")
    ws.merge_cells("F6:F8")
    wb.save(path)


def test_offset_two_row_header_and_merged_cells(tmp_path):
    xlsx = tmp_path / "vash.xlsx"
    _offset_layout(xlsx)
    parsed = parse_vash_xlsx(xlsx, {"10463", "10853"})
    recs = parsed["records"]
    assert [r["vash_number"] for r in recs] == ["101", "102", "103", "201"]
    assert [r["bxm_code"] for r in recs] == ["10463", "10463", "10463", "10853"]
    assert recs[2]["lat"] == 41.370222 and recs[2]["lon"] == 69.270111
    assert parsed["coords_swapped"] == 1
    assert parsed["sheets"][0]["header_row"] == 4
    assert parsed["sheets"][0]["columns"] == {"C": "row_no", "D": "vash_number", "E": "bxm_code",
                                              "F": "bxm_name", "G": "address", "H": "lat", "I": "lon"}
    errors = [e["error"] for e in parsed["errors"]]
    assert len(errors) == 2
    assert any("Дубликат" in e for e in errors)
    assert any("не к чему привязать" in e for e in errors)
    assert recs[3]["source_row"] == 10


def _single_cell_layout(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["т/р", "ВАШ №", "Филиал коди", "Филиал номи", "Манзили", "Lat, Long"])
    ws.append([1, "V-1", 10463, "Yunusobod BXM", "a", "41.311081, 69.240562"])
    ws.append([2, "V-2", "10463", "Yunusobod BXM", "b", "https://www.google.com/maps/place/@41.320001,69.250001,17z"])
    ws.append([3, "V-3", 10463.0, "Yunusobod BXM", "c", "41°18'39.9\"N 69°14'26.0\"E"])
    ws.append([4, "V-4", 10463, "Yunusobod BXM", "d", "41,333301 69,222201"])
    ws.append([5, "V-5", None, "To'qimachilik", "e", "https://yandex.uz/maps/?ll=69.330101%2C41.210501&z=16"])
    ws.append([6, "V-6", 99999, "Unknown BXM", "f", "41.500001 69.500001"])
    ws.append([7, "V-7", 10463, "Yunusobod BXM", "g", "https://maps.app.goo.gl/abc"])
    ws.append([8, "V-8", 10463, "Yunusobod BXM", "h", "55.75, 37.62"])
    ws.append([])
    ws.append([None, None, None, None, None, None])
    wb.save(path)


def test_single_location_cell_and_grouping(pg_db, tmp_path):
    bulk_insert_branches(BRANCHES)
    xlsx = tmp_path / "vash2.xlsx"
    _single_cell_layout(xlsx)
    parsed = parse_vash_xlsx(xlsx, {"10463", "10853"})
    assert len(parsed["records"]) == 8
    assert sum(1 for r in parsed["records"] if r["lat"] is not None) == 6
    assert len(parsed["errors"]) == 2

    stats = replace_vash_zones(parsed["records"])
    assert stats == {"saved": 8, "matched": 7, "unmatched": 1, "branches_matched": 2, "with_coords": 6}

    data = vash_by_branch()
    by_code = {g["local_code"]: g for g in data["branches"]}
    assert by_code["10463"]["count"] == 6 and by_code["10463"]["with_coords"] == 4
    assert by_code["10853"]["count"] == 1
    assert by_code["10853"]["items"][0]["match_method"] == "name"
    assert by_code["10463"]["branch_name"] == "Юнусобод БХМ"
    assert data["unmatched"][0]["bxm_code"] == "99999"
    assert (data["total"], data["matched"], data["with_coords"]) == (8, 7, 6)


def test_headerless_summary_copy_and_sections(tmp_path):
    wb = openpyxl.Workbook()
    summary = wb.active
    summary.title = "Свод"
    summary.append(["№", "БХМ коди", "БХМ номи", "ВАШ сони"])
    summary.append([1, "10463", "Юнусобод", 6])

    plain = wb.create_sheet("Рўйхат")
    data = [
        [1, "A-1", 10463, "Юнусобод БХМ", "x", 41.300001, 69.200001],
        [2, "A-2", 10463, "Юнусобод БХМ", "y", 41.300002, 69.200002],
        [3, "A-3", 10853, "Тўқимачилик БХМ", "z", 41.200003, 69.300003],
    ]
    for i, row in enumerate(data, start=3):
        for j, v in enumerate(row, start=2):
            plain.cell(i, j, v)
    copy = wb.create_sheet("Рўйхат (2)")
    for i, row in enumerate(data, start=3):
        for j, v in enumerate(row, start=2):
            copy.cell(i, j, v)

    grouped = wb.create_sheet("Гуруҳ")
    grouped.append(["№", "ВАШ рақами", "Манзил", "Локация"])
    grouped.append(["Тошкент шаҳри"])
    grouped.merge_cells("A2:D2")
    grouped.append(["10463 Юнусобод БХМ"])
    grouped.merge_cells("A3:D3")
    grouped.append([1, "G-1", "p", "41.310001, 69.210001"])
    grouped.append([2, "G-2", "q", "41.310002, 69.210002"])
    grouped.append(["10853 Тўқимачилик БХМ", None, None, None])
    grouped.append([3, "G-3", "r", "41.210003, 69.310003"])
    path = tmp_path / "vash3.xlsx"
    wb.save(path)

    parsed = parse_vash_xlsx(path, {"10463", "10853"})
    sheets = {s["sheet"]: s for s in parsed["sheets"]}
    assert "сводная" in sheets["Свод"]["skipped"]
    assert sheets["Рўйхат"]["header_row"] is None and sheets["Рўйхат"]["records"] == 3
    assert "копия" in sheets["Рўйхат (2)"]["skipped"]
    assert sheets["Гуруҳ"]["records"] == 3
    by_vash = {r["vash_number"]: r["bxm_code"] for r in parsed["records"]}
    assert by_vash == {"A-1": "10463", "A-2": "10463", "A-3": "10853",
                       "G-1": "10463", "G-2": "10463", "G-3": "10853"}
    assert any("Тошкент шаҳри" in w["warning"] for w in parsed["warnings"])
    assert not parsed["errors"]


def test_no_vash_list_raises(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.append(["Ф.И.Ш", "Лавозим"])
    wb.active.append(["Иванов", "кассир"])
    path = tmp_path / "other.xlsx"
    wb.save(path)
    with pytest.raises(ValueError, match="не найден список ВАШ"):
        parse_vash_xlsx(path)
