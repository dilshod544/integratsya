"""Analytics calculation engine for cashier performance, workload, and report generation."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from ..db import connect
from .helpers import clean_branch_name, norm, optional_text, search_fold
from .parsers import _classify_op_group
from .repository import get_cashier_import, list_cashier_imports


def cashier_role(row: Dict[str, Any]) -> Optional[str]:
    """Determine cashier role from operation counts. None if the cashier has no ops."""
    b = float(row.get('bek_count', 0) or 0)
    f = float(row.get('front_count', 0) or 0)
    if b == 0 and f == 0:
        return None
    return 'front' if f > b else 'back'


def _is_true_absent(row: Dict[str, Any]) -> bool:
    """Person is on maternity/vacation and did not work — the seat is empty."""
    if row.get('hr_status_code') not in ('maternity', 'vacation'):
        return False
    return float(row.get('operations_count') or 0) <= 0


def is_uncovered_absence(row: Dict[str, Any]) -> bool:
    """Empty seat: absent and no temporary replacement assigned."""
    if not _is_true_absent(row):
        return False
    if row.get('replaced_by_full_name'):
        return False
    return int(row.get('has_replacement') or 0) == 0


def apply_replacement_pairing(rows: List[Dict[str, Any]]) -> None:
    """Pair вакт. temps only to empty seats (0 operations). Mutates rows in place."""
    for x in rows:
        st = x.get('hr_status_code')
        if st == 'temporary':
            x['replacing_full_name'] = None
        elif st in ('maternity', 'vacation'):
            x['replaced_by_full_name'] = None
            x['has_replacement'] = 0

    def pair(temp: Dict[str, Any], absent: Dict[str, Any]) -> None:
        temp['replacing_full_name'] = absent.get('full_name')
        absent['replaced_by_full_name'] = temp.get('full_name')
        absent['has_replacement'] = 1

    # In the KPI report the вакт. row sits directly above the seat it covers, so that
    # direction is paired over the whole list first; "absent above temp" only fills
    # what is left (otherwise "декрет / вакт. / декрет" pairs the wrong neighbour).
    ordered = sorted(rows, key=lambda z: (int(z.get('id') or 0), z.get('full_name') or ''))
    for temp_first in (True, False):
        for i in range(len(ordered) - 1):
            temp, absent = (ordered[i], ordered[i + 1]) if temp_first else (ordered[i + 1], ordered[i])
            if (
                temp.get('hr_status_code') == 'temporary'
                and _is_true_absent(absent)
                and not temp.get('replacing_full_name')
                and not absent.get('replaced_by_full_name')
            ):
                pair(temp, absent)

    def _zip_pair(groups: dict) -> None:
        for lst in groups.values():
            temps = [
                x for x in lst
                if x.get('hr_status_code') == 'temporary' and not x.get('replacing_full_name')
            ]
            absents = [x for x in lst if _is_true_absent(x) and not x.get('replaced_by_full_name')]
            for temp, absent in zip(temps, absents):
                pair(temp, absent)

    by_branch_pos: dict[tuple[str, str], list] = defaultdict(list)
    for x in rows:
        by_branch_pos[
            ((x.get('branch_name') or '').strip(), (x.get('position') or '').strip())
        ].append(x)
    _zip_pair(by_branch_pos)

    by_branch: dict[str, list] = defaultdict(list)
    for x in rows:
        by_branch[(x.get('branch_name') or '').strip()].append(x)
    _zip_pair(by_branch)


_ABSENCE_LABELS = {'maternity': '🟣 В декрете', 'vacation': '🔵 В отпуске'}


def refresh_status_labels(rows: List[Dict[str, Any]]) -> None:
    """Rebuild status labels from the final pairing so the text never contradicts
    the counters (labels saved at import time reflect the parser's own pairing)."""
    for x in rows:
        st = x.get('hr_status_code')
        if st == 'temporary':
            rep = x.get('replacing_full_name')
            x['hr_status_label'] = f"🟡 Временный (замещает {rep})" if rep else '🟡 Временный сотрудник'
        elif st in _ABSENCE_LABELS:
            base = _ABSENCE_LABELS[st]
            if not _is_true_absent(x):
                x['hr_status_label'] = base
            elif x.get('replaced_by_full_name'):
                x['hr_status_label'] = f"{base} (замена: {x['replaced_by_full_name']})"
            else:
                x['hr_status_label'] = f"{base} (без замены!)"


def infer_absent_roles(rows: List[Dict[str, Any]]) -> None:
    """Give empty-seat absentees the BEK/FRONT role of the temp covering their seat.

    Run after apply_replacement_pairing. Absentees without a working replacement keep
    cashier_type=None. Mutates rows in place.
    """
    temps = {
        x.get('replacing_full_name'): x
        for x in rows
        if x.get('hr_status_code') == 'temporary' and x.get('replacing_full_name')
    }
    for x in rows:
        x['cashier_type_inferred'] = False
        if x.get('cashier_type') or not _is_true_absent(x):
            continue
        temp = temps.get(x.get('full_name'))
        if temp and temp.get('cashier_type'):
            x['cashier_type'] = temp['cashier_type']
            x['cashier_type_inferred'] = True


def _top_metric_value(row: Dict[str, Any], role: Optional[str] = None) -> float:
    """Value used for Top-10 ranking: BEK / FRONT count, or total ops."""
    r = str(role or '').strip().lower()
    if r == 'back':
        return float(row.get('bek_count', 0) or 0)
    if r == 'front':
        return float(row.get('front_count', 0) or 0)
    return float(row.get('operations_count', 0) or 0)


def _build_top_lists(
    rows: List[Dict[str, Any]],
    role: Optional[str] = None,
    limit: int = 10,
) -> tuple[list[Dict[str, Any]], dict[str, list]]:
    """Build global Top-N and per-position Top-N using the role-aware metric."""
    ranked = sorted(rows, key=lambda z: _top_metric_value(z, role), reverse=True)
    top_cashiers = [x for x in ranked if _top_metric_value(x, role) > 0][:limit]

    pos_groups: dict[str, list] = {}
    for x in rows:
        pos = (x.get('position') or '').strip() or 'Прочее'
        pos_groups.setdefault(pos, []).append(x)

    top_by_position = {
        p: [
            x for x in sorted(lst, key=lambda z: _top_metric_value(z, role), reverse=True)
            if _top_metric_value(x, role) > 0
        ][:limit]
        for p, lst in pos_groups.items()
    }
    return top_cashiers, top_by_position


_NORM_DAYS_FALLBACK = 22.0
_MINUTES_PER_DAY = 480.0


def _enrich(x: dict, detail: bool = False):
    """Enrich a cashier report row in-place and return (x, ops_dict)."""
    ops_cnt = float(x.get('operations_count', 0) or 0)
    ops_min = float(x.get('operations_minutes', 0) or 0)
    days = float(x.get('days_worked', 0) or 0)
    std_days = float(x.get('std_days', 0) or 0)
    norm_days = std_days or _NORM_DAYS_FALLBACK

    x['avg_seconds_per_operation'] = round(ops_min * 60 / ops_cnt, 1) if ops_cnt else 0
    x['operations_per_day'] = round(ops_cnt / days, 1) if days else 0

    raw = x.get('metrics_json') or {}
    ops = raw.get('operations') if isinstance(raw, dict) and 'operations' in raw else (raw if isinstance(raw, dict) else {})
    if not x.get('employee_number'):
        x['employee_number'] = raw.get('employee_number', '') if isinstance(raw, dict) else ''
    x['branch_name'] = clean_branch_name(x.get('branch_name', ''))

    # Prefer Excel load_percent persisted on the row; recompute only if missing.
    lp = float(x.get('load_percent') or 0)
    if 0 < lp <= 1.0:
        lp = round(lp * 100, 1)
    if lp == 0 and days > 0 and ops_min > 0:
        denom = std_days or days
        lp = round((ops_min / (denom * _MINUTES_PER_DAY)) * 100, 1)
    x['load_percent'] = round(lp, 1)
    x['load_difference'] = float(x.get('load_difference') or 0)
    x['std_days'] = std_days

    x['cashier_type'] = cashier_role(x)

    bek_cnt = float(x.get('bek_count', 0) or 0)
    front_cnt = float(x.get('front_count', 0) or 0)
    split_total = bek_cnt + front_cnt
    x['bek_pct'] = round((bek_cnt / split_total) * 100, 1) if split_total else 0.0
    x['front_pct'] = round(100.0 - x['bek_pct'], 1) if split_total else 0.0
    x['days_worked_pct'] = round((days / norm_days) * 100, 1) if days else 0.0

    x['hours_worked'] = round(ops_min / 60, 1)
    mins = int(ops_min)
    x['hours_str'] = f"{mins // 60} ч {mins % 60} мин"

    _EXCLUDE_KEYWORDS = ('жамибек', 'jamibek', 'жамифронт', 'jamfront',
                          'жамиамали', 'bekfark', 'frontfark', 'жами')
    real_metrics = {
        n: v for n, v in ops.items()
        if isinstance(v, dict) and not any(k in norm(n).replace(' ', '') for k in _EXCLUDE_KEYWORDS)
    }
    real_ops_total = sum(v.get('count', 0) for v in real_metrics.values()) or ops_cnt or 1

    x['metrics'] = [
        {
            'name': n,
            'section': ('БЭК-операции' if _classify_op_group(n) == 'back' else 'ФРОНТ-операции'),
            'count': v.get('count', 0),
            'minutes': v.get('minutes', 0),
            'pct': round(v.get('count', 0) / real_ops_total * 100, 1),
        }
        for n, v in real_metrics.items()
    ]
    x['metrics'].sort(key=lambda m: m['count'], reverse=True)
    x['top_direction'] = x['metrics'][0]['name'] if x['metrics'] else '—'

    # Discipline / punishment data (pass-through from DB)
    x['discipline_type'] = optional_text(x.get('discipline_type'))
    x['discipline_date_order'] = optional_text(x.get('discipline_date_order'))
    x['discipline_reason'] = optional_text(x.get('discipline_reason'))
    x['has_discipline'] = bool(x['discipline_type'] or x['discipline_date_order'] or x['discipline_reason'])

    return x, ops


_VACANT_KEYWORDS = ('вакант', 'вакансия', 'vakant', 'vakansiya', 'бош урин', 'буш урин', 'свобод')
_SUMMARY_OP_KEYS = ('жамибек', 'jamibek', 'жамифронт', 'jamfront', 'жамиамали',
                    'beкфарк', 'фронтфарк', 'bekfark', 'frontfark', 'жами')


def _branch_key(branch: Any) -> str:
    return search_fold(clean_branch_name(str(branch or '')))


def build_cashier_rows(conn, import_id: int) -> List[Dict[str, Any]]:
    """All cashiers of one monthly import, fully enriched and before any UI filter.

    Every list, counter and the detail view are derived from this single set, so the
    numbers agree no matter which filters are selected.
    """
    rows = conn.execute('SELECT * FROM cashier_reports WHERE import_id=%s ORDER BY id', (import_id,)).fetchall()

    for x in rows:
        _enrich(x)
        if any(w in norm(x['full_name']) for w in _VACANT_KEYWORDS):
            x['hr_status_code'] = 'vacant'
            x['hr_status_label'] = '⚪ Вакант (Свободная ставка)'
            x['full_name'] = f"ВАКАНСИЯ ({x.get('position') or 'Кассир'})"
        else:
            x['hr_status_code'] = x.get('hr_status_code') or 'active'
            x['hr_status_label'] = x.get('hr_status_label') or '🟢 Работает'
        if not x.get('branch_name'):
            x['branch_name'] = 'Не указан'

    apply_replacement_pairing(rows)
    refresh_status_labels(rows)
    infer_absent_roles(rows)
    return rows


def _filter_rows(
    rows: List[Dict[str, Any]],
    branches: List[str],
    role: Optional[str] = None,
    search: Optional[str] = None,
    position: Optional[str] = None,
    status: Optional[str] = None,
    branch: Optional[str] = None,
) -> List[Dict[str, Any]]:
    role_q = str(role or '').strip().lower()
    if role_q in ('back', 'front'):
        rows = [x for x in rows if x.get('cashier_type') == role_q]
    elif role_q == 'none':
        rows = [x for x in rows if not x.get('cashier_type')]

    pos_q = str(position or '').strip().lower()
    if pos_q:
        rows = [x for x in rows if (x.get('position') or '').strip().lower() == pos_q]

    br_q = _branch_key(branch)
    if br_q:
        exact = {b for b in branches if _branch_key(b) == br_q}
        if exact:
            rows = [x for x in rows if x.get('branch_name') in exact]
        else:
            rows = [x for x in rows if br_q in _branch_key(x.get('branch_name'))]

    st_q = str(status or '').strip().lower()
    if st_q == 'no_replacement':
        rows = [x for x in rows if is_uncovered_absence(x)]
    elif st_q == 'maternity':
        rows = [x for x in rows if x.get('hr_status_code') == 'maternity' and not is_uncovered_absence(x)]
    elif st_q == 'disciplined':
        rows = [x for x in rows if x.get('has_discipline')]
    elif st_q in ('active', 'vacation', 'temporary', 'vacant', 'sick'):
        rows = [x for x in rows if x.get('hr_status_code') == st_q]

    q_words = search_fold(search).split()
    if q_words:
        def haystack(x):
            return search_fold(' '.join(str(x.get(k) or '') for k in (
                'full_name', 'position', 'tab_number', 'employee_number', 'hr_status_label',
                'branch_name', 'raw_note', 'replaced_by_full_name', 'replacing_full_name',
            )))
        rows = [x for x in rows if all(w in haystack(x) for w in q_words)]
    return rows


def _operation_totals(rows: List[Dict[str, Any]], role_q: str):
    """Category breakdown and BEK/FRONT totals for the rows on screen."""
    cats: dict[str, dict] = {}
    allowed_group = role_q if role_q in ('back', 'front') else None
    bek_tot = front_tot = 0.0
    for x in rows:
        m_json = x.get('metrics_json') or {}
        ops_data = m_json.get('operations') if isinstance(m_json, dict) and 'operations' in m_json else (m_json if isinstance(m_json, dict) else {})
        row_has_ops = False
        for n, v in ops_data.items():
            if not isinstance(v, dict):
                continue
            if any(k in norm(n).replace(' ', '') for k in _SUMMARY_OP_KEYS):
                continue
            cnt = float(v.get('count', 0) or 0)
            mins = float(v.get('minutes', 0) or 0)
            op_group = _classify_op_group(n)
            if op_group == 'back':
                bek_tot += cnt
                row_has_ops = True
            elif op_group == 'front':
                front_tot += cnt
                row_has_ops = True
            if allowed_group is not None and op_group != allowed_group:
                continue
            a = cats.setdefault(n, {'name': n, 'count': 0, 'minutes': 0})
            a['count'] += cnt
            a['minutes'] += mins
        if not row_has_ops:
            bek_tot += float(x.get('bek_count', 0) or 0)
            front_tot += float(x.get('front_count', 0) or 0)

    tot_cat_count = sum(c['count'] for c in cats.values()) or 1
    categories = []
    for c_item in sorted(cats.values(), key=lambda item: item['count'], reverse=True):
        c_item['pct'] = round(c_item['count'] / tot_cat_count * 100, 1)
        categories.append(c_item)
    return categories, bek_tot, front_tot


def _summary(rows: List[Dict[str, Any]], bek_tot: float, front_tot: float, role: Optional[str]) -> Dict[str, Any]:
    total_ops = bek_tot + front_tot
    total_min = sum(float(x.get('operations_minutes', 0) or 0) for x in rows)
    working = [x for x in rows if float(x.get('operations_count') or 0) > 0]
    tot_load = sum(float(x.get('load_percent', 0) or 0) for x in working)
    maternity_all = [x for x in rows if x.get('hr_status_code') == 'maternity']
    maternity = [x for x in maternity_all if not is_uncovered_absence(x)]
    count = lambda pred: sum(1 for x in rows if pred(x))
    return {
        'cashiers': len(rows),
        'working_cashiers': len(working),
        'operations': round(total_ops),
        'minutes': round(total_min),
        'hours': round(total_min / 60, 1),
        'avg_seconds_per_operation': round(total_min * 60 / total_ops, 1) if total_ops else 0,
        'avg_load_percent': round(tot_load / len(working), 1) if working else 0,
        'bek_operations': round(bek_tot),
        'bek_pct': round(bek_tot / total_ops * 100, 1) if total_ops else 0,
        'front_operations': round(front_tot),
        'front_pct': round(front_tot / total_ops * 100, 1) if total_ops else 0,
        'back_cashiers': count(lambda x: x.get('cashier_type') == 'back'),
        'front_cashiers': count(lambda x: x.get('cashier_type') == 'front'),
        'no_role_cashiers': count(lambda x: not x.get('cashier_type')),
        'active_cashiers': count(lambda x: x.get('hr_status_code') == 'active'),
        'vacation_cashiers': count(lambda x: x.get('hr_status_code') == 'vacation'),
        'maternity_cashiers': len(maternity),
        'maternity_back': sum(1 for x in maternity if x.get('cashier_type') == 'back'),
        'maternity_front': sum(1 for x in maternity if x.get('cashier_type') == 'front'),
        'maternity_unknown': sum(1 for x in maternity if not x.get('cashier_type')),
        'maternity_no_replacement': len(maternity_all) - len(maternity),
        'temporary_cashiers': count(lambda x: x.get('hr_status_code') == 'temporary'),
        'sick_cashiers': count(lambda x: x.get('hr_status_code') == 'sick'),
        'vacant_positions': count(lambda x: x.get('hr_status_code') == 'vacant'),
        'no_replacement_cashiers': count(is_uncovered_absence),
        'disciplined_cashiers': count(lambda x: x.get('has_discipline')),
        'active_filter': role or 'all',
    }


_EMPTY_RESPONSE = {
    'summary': {}, 'cashiers': [], 'top_cashiers': [], 'categories': [], 'positions': [],
    'branches': [], 'top_by_position': {}, 'import': None, 'imports': [],
    'page': 1, 'page_size': 25, 'total_pages': 1, 'total': 0,
}


def cashier_analytics(
    import_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 25,
    role: Optional[str] = None,
    search: Optional[str] = None,
    position: Optional[str] = None,
    status: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Filtered, paginated analytics for one monthly import (latest month by default)."""
    imports = list_cashier_imports()
    info = next((i for i in imports if i['id'] == import_id), None) if import_id else (imports[0] if imports else None)
    if not info:
        return {**_EMPTY_RESPONSE, 'imports': imports}

    with connect() as c:
        universe = build_cashier_rows(c, info['id'])

    positions_list = sorted({(x.get('position') or '').strip() or 'Прочее' for x in universe})
    branches_list = sorted({(x.get('branch_name') or '').strip() for x in universe if (x.get('branch_name') or '').strip()})

    role_q = str(role or '').strip().lower()
    rows = _filter_rows(universe, branches_list, role, search, position, status, branch)

    top_cashiers, top_by_position = _build_top_lists(rows, role_q)
    rows = sorted(rows, key=lambda z: (-_top_metric_value(z, role_q), z.get('full_name') or ''))
    categories_list, bek_tot, front_tot = _operation_totals(rows, role_q)

    total = len(rows)
    page_size = min(max(1, page_size), 200)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), total_pages)
    start = (page - 1) * page_size

    return {
        'import': info,
        'imports': imports,
        'summary': _summary(rows, bek_tot, front_tot, role),
        'cashiers': rows[start:start + page_size],
        'top_cashiers': top_cashiers,
        'categories': categories_list,
        'positions': positions_list,
        'branches': branches_list,
        'top_by_position': top_by_position,
        'page': page,
        'page_size': page_size,
        'total': total,
        'total_pages': total_pages,
    }


def cashier_detail(report_id: int) -> Optional[Dict[str, Any]]:
    """One cashier, computed exactly like in the list (status, replacement, role)."""
    with connect() as c:
        r = c.execute('SELECT import_id FROM cashier_reports WHERE id=%s', (report_id,)).fetchone()
    if not r:
        return None
    info = get_cashier_import(r['import_id'])
    if not info:
        return None
    with connect() as c:
        rows = build_cashier_rows(c, info['id'])

    x = next((z for z in rows if z['id'] == report_id), None)
    if not x:
        return None
    x['filename'] = info.get('filename')
    x['imported_at'] = info.get('imported_at')
    x['period'] = info.get('period')
    x['period_label'] = info.get('period_label')
    return x
