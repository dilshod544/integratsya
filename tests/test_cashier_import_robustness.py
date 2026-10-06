"""New monthly files: wrong formats, other layouts and user mistakes must not break the data."""
from __future__ import annotations

import csv
from datetime import date, datetime

import openpyxl
import pytest

from api.core import db as core_db
from api.core.cashier import repository
from api.core.cashier.helpers import detect_period, parse_hr_status, sval
from api.core.cashier.parsers import parse_cashier_status_xlsx, parse_cashiers_xlsx

HEADER = [
    '№', 'Табел рақами', 'Лавозим', 'Ф.И.Ш', 'Изох', 'Локал код', 'БХМ номи',
    'Интизомий жазо тури', 'Сана ва буйруқ рақами', 'Интизомий жазо сабаблари',
    'Кассир иш хажми (юклама) (420)', 'Иш куни', 'Ишлаган куни',
    'Купюра санаш (1)', None, 'Коммунал туловлар (кирим-чиқим) (3)', None,
]
SUB = [None] * 13 + ['Сони', 'минутда', 'Сони', 'минутда']
TOTALS = ['Жами'] + [None] * 12 + [300, 300, 20, 60]
PEOPLE = [
    [1, 118339.0, 'Universal kassir', 'ALIYEVA MALIKA', None, '11088', 'Бунёдкор БХМ',
     None, None, None, 0.5, 22, 22, 200, 200, 0, 0],
    [2, 118340, 'Bosh kassir', 'BOBOSHEV ANVAR', None, '11088', 'Бунёдкор БХМ',
     'Хайфсан', datetime(2026, 7, 3), 'Кечикиш', 0.4, 22, 21, 100, 100, 20, 60],
    [3, 118341, 'Universal kassir', 'KARIMOVA NODIRA', '(декрет2)', '11088', 'Бунёдкор БХМ',
     None, None, None, 0, 22, 0, 0, 0, 0, 0],
]


def _kpi_rows():
    return [[None] * 13 + ['Бек офис'], HEADER, SUB, TOTALS, *PEOPLE]


def _write_xlsx(path, rows, empty_active_sheet=False):
    wb = openpyxl.Workbook()
    ws = wb.active
    if empty_active_sheet:
        ws.title = 'Свод'
        ws.append(['Сводная таблица'])
        ws = wb.create_sheet('Kassir')
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def test_kpi_file_parses_by_headers(tmp_path):
    p = parse_cashiers_xlsx(_write_xlsx(tmp_path / 'kpi.xlsx', _kpi_rows()))
    recs = {r['full_name']: r for r in p['records']}
    assert len(recs) == 3
    a = recs['ALIYEVA MALIKA']
    assert a['tab_number'] == '118339'
    assert a['branch_name'] == '11088 - Бунёдкор БХМ'
    assert a['bek_count'] == 200 and a['front_count'] == 0
    b = recs['BOBOSHEV ANVAR']
    assert b['hr_status_code'] == 'active'
    assert b['discipline_date_order'] == '03.07.2026'
    assert recs['KARIMOVA NODIRA']['hr_status_code'] == 'maternity'


def test_report_on_second_sheet_is_found(tmp_path):
    p = parse_cashiers_xlsx(_write_xlsx(tmp_path / 'kpi.xlsx', _kpi_rows(), empty_active_sheet=True))
    assert len(p['records']) == 3


def test_changed_layout_is_rejected_not_guessed(tmp_path):
    rows = _kpi_rows()
    rows[1] = [h if h != 'Ф.И.Ш' else 'Ходим' for h in HEADER]
    with pytest.raises(ValueError, match='Ф.И.Ш'):
        parse_cashiers_xlsx(_write_xlsx(tmp_path / 'kpi.xlsx', rows))

    rows = _kpi_rows()
    rows[1] = [None if h in ('Локал код', 'БХМ номи') else h for h in HEADER]
    with pytest.raises(ValueError, match='БХМ номи'):
        parse_cashiers_xlsx(_write_xlsx(tmp_path / 'kpi2.xlsx', rows))


def test_unreadable_files_give_clear_errors(tmp_path):
    xls = tmp_path / 'old.xls'
    xls.write_bytes(b'\xd0\xcf\x11\xe0 old excel')
    with pytest.raises(ValueError, match='.xlsx'):
        parse_cashiers_xlsx(xls)

    broken = tmp_path / 'broken.xlsx'
    broken.write_bytes(b'not a zip at all')
    with pytest.raises(ValueError, match='повреждён'):
        parse_cashiers_xlsx(broken)

    empty = _write_xlsx(tmp_path / 'empty.xlsx', [])
    with pytest.raises(ValueError):
        parse_cashiers_xlsx(empty)


def test_csv_semicolon_cp1251(tmp_path):
    path = tmp_path / 'kpi.csv'
    with open(path, 'w', encoding='cp1251', errors='replace', newline='') as f:
        w = csv.writer(f, delimiter=';')
        for r in _kpi_rows():
            w.writerow(['' if v is None else (v.strftime('%d.%m.%Y') if isinstance(v, datetime) else v) for v in r])
    recs = parse_cashiers_xlsx(path)['records']
    assert [r['full_name'] for r in recs] == ['ALIYEVA MALIKA', 'BOBOSHEV ANVAR', 'KARIMOVA NODIRA']
    assert recs[0]['branch_name'] == '11088 - Бунёдкор БХМ'


def test_kpi_file_is_rejected_as_staff_registry(tmp_path):
    with pytest.raises(ValueError, match='Отчёт KPI'):
        parse_cashier_status_xlsx(_write_xlsx(tmp_path / 'kpi.xlsx', _kpi_rows()))


def test_registry_skips_numeric_names(tmp_path):
    rows = [
        ['МФО', 'Лавозим', 'Ф.И.Ш', 'Изох'],
        ['11088', 'Universal kassir', 'ALIYEVA MALIKA', ''],
        ['11088', 'Universal kassir', '3398539', ''],
        ['11088', 'Universal kassir', 'BOBOSHEV ANVAR', ''],
        ['11088', 'Universal kassir', "Bo'sh", ''],
    ]
    recs = parse_cashier_status_xlsx(_write_xlsx(tmp_path / 'shtat.xlsx', rows))['records']
    names = {r['full_name']: r['status_code'] for r in recs}
    assert '3398539' not in names
    assert names['BOBOSHEV ANVAR'] == 'active'
    assert names['ВАКАНСИЯ (Universal kassir)'] == 'vacant'


def test_empty_seat_word_vs_chief_cashier():
    assert parse_hr_status('', 'Bosh kassir', 'ALIYEVA MALIKA')['status_code'] == 'active'
    assert parse_hr_status('', 'Universal kassir', 'TOSHBOSHEV BOBUR')['status_code'] == 'active'
    assert parse_hr_status("bo'sh o'rin", 'Universal kassir', '-')['status_code'] == 'vacant'
    assert parse_hr_status('', 'Universal kassir', '(Вакант)')['status_code'] == 'vacant'


def test_cell_values_to_text():
    assert sval(118339.0) == '118339'
    assert sval(1.5) == '1.5'
    assert sval(datetime(2026, 7, 3, 0, 0)) == '03.07.2026'
    assert sval(date(2026, 7, 3)) == '03.07.2026'


def test_short_date_in_filename():
    today = date(2026, 10, 6)
    assert detect_period("Kassirlar bo'yicha 20.07.26", today) == '2026-07'
    assert detect_period('31.08.2026 kassirlar', today) == '2026-08'


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(core_db, 'DB_PATH', tmp_path / 'bank.db')
    monkeypatch.setattr(repository, '_tables_initialized', False)
    yield
    repository._tables_initialized = False


def test_same_file_under_another_month_is_flagged(temp_db, tmp_path):
    parsed = parse_cashiers_xlsx(_write_xlsx(tmp_path / 'kpi.xlsx', _kpi_rows()))
    jul = repository.save_cashier_import('Kassirlar.xlsx', parsed, '2026-07')
    assert jul['same_data_as'] == []
    aug = repository.save_cashier_import('Kassirlar.xlsx', parsed, '2026-08')
    assert aug['same_data_as'] == ['Июль 2026']
    again = repository.save_cashier_import('Kassirlar.xlsx', parsed, '2026-08')
    assert again['replaced_previous'] is True
    assert again['same_data_as'] == ['Июль 2026']
    assert len(repository.list_cashier_imports()) == 2
