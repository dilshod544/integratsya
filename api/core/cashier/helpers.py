"""Helper utilities and constants for cashier analytics and data parsing."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Optional

_CYR_RE = re.compile(r'[\u0400-\u04FF]')
_LAT_RE = re.compile(r'[A-Za-z]')
# Latin letters that look identical to Cyrillic ones. Excel sources sometimes type
# the first letter of a Cyrillic word on a Latin layout ("Tошкент", "Xонобод").
_LATIN_TO_CYRILLIC = str.maketrans({
    'A': 'А', 'B': 'В', 'C': 'С', 'E': 'Е', 'H': 'Н', 'K': 'К', 'M': 'М', 'O': 'О',
    'P': 'Р', 'T': 'Т', 'X': 'Х', 'Y': 'У',
    'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 'x': 'х', 'y': 'у',
})


def fix_mixed_script(text: str) -> str:
    """Replace Latin look-alike letters inside words that are otherwise Cyrillic."""
    def fix_word(m: re.Match) -> str:
        w = m.group(0)
        if _CYR_RE.search(w) and _LAT_RE.search(w):
            return w.translate(_LATIN_TO_CYRILLIC)
        return w
    return re.sub(r'\w+', fix_word, text or '')


_UZ_CYR_TO_LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'j',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'x', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'sh', 'ъ': '', 'ы': 'i', 'ь': '', 'э': 'e', 'ю': 'yu',
    'я': 'ya', 'ў': 'o', 'қ': 'q', 'ғ': 'g', 'ҳ': 'h',
}


def search_fold(v: Any) -> str:
    """Fold text to one Latin form so Cyrillic and Latin spellings match in search.

    "Тошкент", "Tошкент" and "Toshkent" all become "toshkent"; "Мухтарова" matches
    "MUXTAROVA".
    """
    if v is None:
        return ''
    s = str(v).lower()
    s = ''.join(_UZ_CYR_TO_LAT.get(ch, ch) for ch in s)
    s = re.sub(r"['`′ʼʻ‘’]", '', s)
    s = re.sub(r'[^\w]+', ' ', s, flags=re.UNICODE)
    return s.strip()


_MONTHS = {
    1: ('yanvar', 'январ'),
    2: ('fevral', 'феврал'),
    3: ('mart', 'март'),
    4: ('aprel', 'апрел'),
    5: ('may', 'май', 'мая'),
    6: ('iyun', 'июн'),
    7: ('iyul', 'июл'),
    8: ('avgust', 'август'),
    9: ('sentyabr', 'sentabr', 'сентябр'),
    10: ('oktyabr', 'oktabr', 'октябр'),
    11: ('noyabr', 'ноябр'),
    12: ('dekabr', 'декабр'),
}
_WHOLE_WORD_MONTHS = {'may', 'май', 'мая', 'mart', 'март'}
MONTH_NAMES_RU = ('', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль',
                  'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')
_PERIOD_RE = re.compile(r'^(20\d{2})-(0[1-9]|1[0-2])$')


def normalize_period(value: Any) -> Optional[str]:
    """Return 'YYYY-MM' if value is a valid reporting month, else None."""
    s = str(value or '').strip()
    return s if _PERIOD_RE.match(s) else None


def period_label(period: Optional[str]) -> str:
    """'2026-07' -> 'Июль 2026'."""
    p = normalize_period(period)
    if not p:
        return 'Период не указан'
    y, m = p.split('-')
    return f"{MONTH_NAMES_RU[int(m)]} {y}"


def detect_period(text: str, today: Optional[date] = None) -> Optional[str]:
    """Guess the reporting month from a filename or sheet title.

    Recognises '2026-07', '07.2026', '07_2026', '20.07.26' and month names in Uzbek/Russian
    (Latin or Cyrillic). Without an explicit year, the most recent such month up to
    today is used.
    """
    s = str(text or '').lower()
    m = re.search(r'(20\d{2})[.\-_/ ](0[1-9]|1[0-2])(?!\d)', s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r'(?<!\d)(0[1-9]|1[0-2])[.\-_/ ](20\d{2})', s)
    if m:
        return f"{m.group(2)}-{m.group(1)}"
    m = re.search(r'(?<!\d)(?:0?[1-9]|[12]\d|3[01])\.(0[1-9]|1[0-2])\.(\d{2})(?!\d)', s)
    if m:
        return f"20{m.group(2)}-{m.group(1)}"

    def has_name(n: str) -> bool:
        # Short names ("may", "март") must be whole words: "maydon" is not May.
        tail = r'(?![a-zа-яёқўғҳ])' if n in _WHOLE_WORD_MONTHS else ''
        return re.search(rf'(?<![a-zа-яёқўғҳ]){re.escape(n)}{tail}', s) is not None

    month = None
    for num_, names in _MONTHS.items():
        if any(has_name(n) for n in names):
            month = num_
            break
    if month is None:
        return None
    y = re.search(r'(?<!\d)(20\d{2})(?!\d)', s)
    if y:
        return f"{y.group(1)}-{month:02d}"
    today = today or date.today()
    year = today.year if month <= today.month else today.year - 1
    return f"{year}-{month:02d}"


def clean_branch_name(raw_branch: str) -> str:
    """Clean up MFO numbers and bank prefixes from branch names."""
    if not raw_branch:
        return ''
    s = str(raw_branch).strip()
    s = re.sub(r'^\d{5}\s*-\s*[0-9A-Za-z]{4,5}\s*', '', s)
    s = re.sub(r'^[0-9A-Za-z]{4,5}\s*-\s*', '', s)
    s = re.sub(r'^\d{5}\s*', '', s)
    s = re.sub(r'["\']O[^\s]*zsanoatqurilishbank["\']?\s*ATB\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'ATB\s*', '', s, flags=re.IGNORECASE)
    s = s.strip(' -"\'' + '\xa0')
    s = re.sub(r'bank\s+xizmatlar[i]?\s+markaz[i]?', 'ЦБУ', s, flags=re.IGNORECASE)
    s = re.sub(r'bank\s+xizmatlar[i]?\s+ofis[i]?', 'ОБУ', s, flags=re.IGNORECASE)
    s = re.sub(r'центр\s+банковских\s+услуг', 'ЦБУ', s, flags=re.IGNORECASE)
    s = re.sub(r'офис\s+банковских\s+услуг', 'ОБУ', s, flags=re.IGNORECASE)
    s = fix_mixed_script(s)
    return re.sub(r'\s+', ' ', s).strip()


def norm(v: Any) -> str:
    """Normalise a cell value to a lowercase, ASCII-friendly string for matching."""
    if v is None:
        return ''
    s = str(v).lower().strip()
    # Cyrillic digraphs that appear in Uzbek sources
    cyr_map = {'қ': 'к', 'ў': 'у', 'ғ': 'г', 'ҳ': 'х', 'ё': 'е', 'ҷ': 'ч', 'ӣ': 'и'}
    for src, dst in cyr_map.items():
        s = s.replace(src, dst)
    s = re.sub(r"['`′ʼʻʼ]", '', s)
    s = re.sub(r'[^\w]+', ' ', s, flags=re.UNICODE)
    return s.strip()


def num(v: Any) -> float:
    """Safely convert any cell value to a float."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s in ('-', '—', '- ', ' - ', 'None', 'null', 'nan'):
        return 0.0
    # Strip Excel non-breaking spaces and similar artefacts
    s = s.replace('\xa0', '').replace('\u202f', '')
    try:
        return float(s)
    except ValueError:
        cleaned = re.sub(r'[^0-9,.\-]', '', s).replace(',', '.')
        if not cleaned or cleaned in ('-', '.', '-.'):
            return 0.0
        try:
            return float(cleaned)
        except ValueError:
            return 0.0


_EMPTY_MARKS = frozenset({'', '0', 'нет', 'yoq', 'йук', 'йок', 'none', 'null', 'nan'})


def optional_text(v: Any) -> Optional[str]:
    """Cell text, or None for placeholders: reports fill "no penalty" cells with 0 or "-"."""
    s = sval(v)
    return None if norm(s) in _EMPTY_MARKS else s


def sval(v: Any) -> str:
    """Safely convert a cell value to a stripped string, removing Excel artefacts."""
    if v is None:
        return ''
    if isinstance(v, (datetime, date)):
        return v.strftime('%d.%m.%Y')
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip().replace('\xa0', ' ').replace('\u202f', ' ')
    # Remove leading/trailing apostrophes that openpyxl sometimes prepends
    s = s.strip("'")
    return s.strip()


# Keywords used to classify operation names into BEK (back-office) or FRONT groups.
# "банкомат" — kassirning BEK operatsiyasi (Excel ustun nomi), ATM monitoring emas.
BACK_KEYWORDS = (
    'амалга оширилган операциялар сони',
    'банкомат',
    'касса мудири',
    'кечки кассир',
    'назоратчи',
    'купюра санаш',
)
FRONT_KEYWORDS = (
    'ваш', 'валюта', 'коммунал', 'пластик', 'накд', 'кирим чиким хужжати',
)
SUMMARY_KEYWORDS = (
    'жами бек', 'jami bek', 'жами фронт', 'jami front',
    'жами амалиетлар', 'jami amaliyotlar',
    'бек фарк', 'фронт фарк', 'bek fark', 'front fark',
)

# Canonical display names for recognised operation types (used in metrics_json)
BACK_GROUPS = {
    'Амалга оширилган операциялар сони (кирим-чиқим)',
    'Банкоматга пул қўйиш',
    'Касса мудири',
    'Кечки кассир',
    'Назоратчи кассир ролини бажарганда',
    'Купюра санаш',
}
FRONT_GROUPS = {
    'Кирим, чиқим ҳужжатини текшириш, расмийлаштириш',
    'ВАЛЮТА 100$',
    'ВАЛЮТА 100,01–1000$',
    'ВАЛЮТА 1000,01–5000$',
    'ВАЛЮТА 10000$',
    'ВАЛЮТА 5000,01–10000$',
    'Коммунал тўловлар (кирим-чиқим)',
    'Пластик карта тарқатиш',
    'Пластикдан нақд пул ечиш',
}
EXCLUDED_GROUPS = {
    'БЕК фарқ', 'ФРОНТ фарқ', 'Жами (БЕК)', 'Жами (ФРОНТ)',
    'Жами амалиётлар', 'Жами',
    'БЕК фарк', 'ФРОНТ фарк', 'Жами БЕК', 'Жами ФРОНТ',
    'Жами (Бек офис)', 'Жами (Фронт офис)',
    'Бек офис', 'Фронт офис',
}


def parse_hr_status(raw_note: str, position: str = '', full_name: str = '') -> Dict[str, Any]:
    """Parse HR status code and label from raw note string, position, and full_name."""
    raw_note = str(raw_note or '').strip()
    position = str(position or '').strip()
    full_name = str(full_name or '').strip()
    n_str = norm(f"{raw_note} {position} {full_name}").replace(' ', '')
    # "bo'sh"/"бўш" (empty seat) only as a separate word in the note or name: "Bosh kassir"
    # is a chief cashier and surnames like "Boboshev" contain the same letters.
    empty_seat = re.search(r'\b(bosh|bush|бош|буш)\b', norm(f"{raw_note} {full_name}")) is not None

    if empty_seat or any(w in n_str for w in ('vacant', 'вакант', 'вакансия', 'свобод', 'vakant', 'vakansiya')):
        code, label = 'vacant', '⚪ Вакансия (Свободная ставка)'
    elif any(w in n_str for w in ('dekret', 'декрет', 'деккас', 'dekkas')):
        # "(дек. кас.)" — декрет касаллик: maternity leave on a sick-leave certificate.
        code, label = 'maternity', '🟣 В декрете'
    elif any(w in n_str for w in ('mexnat', 'мехнат', 'отпуск', 'tatil', 'татил')):
        code, label = 'vacation', '🔵 В отпуске (Трудовой отпуск)'
    elif any(w in n_str for w in ('vakt', 'вакт', 'вактинча', 'замещ', 'вактинчалик', 'zamesh')):
        code, label = 'temporary', '🟡 Временный сотрудник'
    elif any(w in n_str for w in ('kasal', 'касал', 'больн')):
        code, label = 'sick', '🔴 Болен (Больничный)'
    else:
        code, label = 'active', '🟢 Работает'

    return {
        'status_code': code,
        'status_label': label,
        'has_replacement': 1 if code == 'active' else 0,
        'replacing_full_name': None,
        'replaced_by_full_name': None,
        'raw_note': raw_note,
    }
