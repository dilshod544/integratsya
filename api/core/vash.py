"""ВАШ (валюта айирбошлаш шохобчалари) — alohida parser va filiallarga bog'lash.

Excel: № | ВАШ рақами | БХМ коди | БХМ номи | Манзил | Локация (lat, lon)

Jadval varaqning istalgan qator/ustunidan boshlanishi mumkin; sarlavha bir necha
qatorli va birlashtirilgan (merged) bo'lishi mumkin; sarlavha umuman bo'lmasa,
ustunlar mazmuni bo'yicha aniqlanadi. Filialga bog'lash — БХМ коди ↔
branches.local_code; kod yo'q bo'lsa — БХМ номи bo'yicha. Har yuklash alohida
raqamlangan import; xarita oxirgisini ochadi. Filial reestri o'zgarmaydi.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote

import openpyxl
from openpyxl.utils import get_column_letter

from .cashier.helpers import search_fold
from .dataset_imports import (
    KIND_VASH_ZONES,
    adopt_unassigned,
    create_dataset_import_tables,
    delete_dataset_import,
    ensure_import_column,
    list_dataset_imports,
    resolve_import,
    restore_original_import_time,
    start_import,
)
from .db import connect, ensure_schema

# Uzbekistan bounding box (a little wider than the border). Latitude and longitude
# ranges do not overlap, so a swapped pair is detected reliably.
UZ_LAT = (37.0, 45.7)
UZ_LON = (55.9, 73.3)

FIELDS = ("row_no", "vash_number", "bxm_code", "bxm_name", "address", "lat", "lon",
          "location_raw", "sheet", "source_row")

HEADER_SCAN_ROWS = 40
_CORE_FIELDS = {"row_no", "vash_number", "bxm_code", "bxm_name", "address", "location", "lat", "lon"}
_MULTI_FIELDS = {"location"}

_BXM_KW = ("bxm", "bxo", "filial", "lokal", "mfo", "branch")
_LOC_KW = ("lokats", "lokac", "location", "koordinat", "coordinat", "geolok", "geoloc", "gps",
           "joylashuv", "xarita", "map")
_ADDR_KW = ("manzil", "adres", "address", "joylashgan")
_VASH_KW = ("vash", "obmen", "exchange", "ayirbosh", "almashtir")
_TOTAL_RE = re.compile(r"^(jami|itogo|vsego|total|hammasi)\b")
_ROW_NO_RE = re.compile(r"^(№|#|n|no|nr)\s*\.?\s*(п/п|p/p|пп)?\.?$|^(т/р|t/r|п/п|p/p|тр|tr)\.?$", re.I)
_CODE_RE = re.compile(r"(?<!\d)\d{4,6}(?!\d)")
_NUM_DOT = re.compile(r"(?<![\d.])-?\d{1,3}\.\d+")
_NUM_COMMA = re.compile(r"(?<![\d,])-?\d{1,3},\d+")
_DMS = re.compile(
    r"(\d{1,3}(?:[.,]\d+)?)\s*[°º˚]\s*"
    r"(?:(\d{1,2}(?:[.,]\d+)?)\s*['′’ʼ]\s*)?"
    r"(?:(\d{1,2}(?:[.,]\d+)?)\s*(?:\"|″|”|''|’’)\s*)?"
    r"([NSEWСЮВЗ])?",
    re.I,
)


# ── Header recognition ──────────────────────────────────────

def _tokens(s: str) -> List[str]:
    return s.split()


def _resolve_header(value: Any) -> Optional[str]:
    """Header cell text → field name (or None). Cyrillic and Latin spellings fold to one form."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if _ROW_NO_RE.match(raw.lower()):
        return "row_no"
    s = search_fold(raw)
    if not s:
        return None
    toks = _tokens(s)
    has = lambda keys: any(k in s for k in keys)
    tok_starts = lambda prefixes: any(t.startswith(p) for t in toks for p in prefixes)

    is_vash = has(_VASH_KW)
    if is_vash and tok_starts(("soni", "son", "kolich", "kol", "count", "jami")):
        return "vash_count"

    lat_kw = tok_starts(("lat", "shirot", "kenglik"))
    lon_kw = tok_starts(("lon", "lng", "dolgot", "uzunlik"))
    if lat_kw and lon_kw:
        return "location"
    if lat_kw:
        return "lat"
    if lon_kw:
        return "lon"
    if has(_LOC_KW):
        return "location"

    if has(_ADDR_KW):
        return "address" if is_vash or not has(_BXM_KW) else "branch_address"

    is_code = tok_starts(("kod", "code")) or "mfo" in toks
    is_name = tok_starts(("nomi", "naimen", "nazv", "name")) or "nom" in toks
    if has(_BXM_KW) and not is_vash:
        if is_code or "lokal" in s:
            return "bxm_code"
        return "bxm_name"
    if is_vash:
        if has(_BXM_KW) and is_code:
            return "bxm_code"
        if has(_BXM_KW):
            return "bxm_name"
        return "vash_name" if is_name else "vash_number"
    if is_code:
        return "bxm_code"
    if is_name:
        return "bxm_name"
    return None


def _assign(headers: Sequence[Any]) -> Dict[int, str]:
    """Column index (0-based) → field; first column wins, «location» may span columns."""
    out: Dict[int, str] = {}
    taken: set[str] = set()
    for idx, cell in enumerate(headers):
        field = _resolve_header(cell)
        if not field:
            continue
        if field in taken and field not in _MULTI_FIELDS:
            continue
        out[idx] = field
        taken.add(field)
    return out


def _score(mapping: Dict[int, str]) -> int:
    fields = set(mapping.values()) & _CORE_FIELDS
    if fields & {"bxm_code", "bxm_name"}:
        return len(fields)
    # ВАШ list grouped under branch title rows — the sheet has no БХМ column at all
    if "vash_number" in fields and len(fields) >= 3:
        return len(fields) - 1
    return 0


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _row_is_data_like(row: Sequence[Any]) -> bool:
    for v in row:
        if _is_blank(v):
            continue
        if _code_from(v):
            return True
        if isinstance(v, float) and (UZ_LAT[0] <= v <= UZ_LAT[1] or UZ_LON[0] <= v <= UZ_LON[1]):
            return True
    return False


# ── Value parsing ───────────────────────────────────────────

def _clean_text(v: Any) -> Optional[str]:
    if _is_blank(v):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def _code_from(v: Any) -> Optional[str]:
    """«10463», 10463, 10463.0, «10463 Юнусобод» → '10463'. Postal codes in addresses are not read."""
    if _is_blank(v) or isinstance(v, bool):
        return None
    if isinstance(v, float):
        if not v.is_integer():
            return None
        v = int(v)
    s = re.sub(r"(?<=\d)\.0+\b", "", str(v).strip())
    m = _CODE_RE.search(s)
    return m.group(0) if m else None


def _row_no_from(v: Any) -> Optional[int]:
    if _is_blank(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else None
    m = re.fullmatch(r"\s*(\d{1,6})\s*\.?\s*", str(v))
    return int(m.group(1)) if m else None


def _in_uz(lat: float, lon: float) -> bool:
    return UZ_LAT[0] <= lat <= UZ_LAT[1] and UZ_LON[0] <= lon <= UZ_LON[1]


def _pick_pair(nums: Sequence[float]) -> Optional[Tuple[float, float, bool]]:
    for a, b in zip(nums, nums[1:]):
        if _in_uz(a, b):
            return a, b, False
        if _in_uz(b, a):
            return b, a, True
    return None


def _dms_values(text: str) -> List[float]:
    out: List[float] = []
    for m in _DMS.finditer(text):
        deg, mins, secs, hemi = m.groups()
        val = float(deg.replace(",", "."))
        if mins:
            val += float(mins.replace(",", ".")) / 60
        if secs:
            val += float(secs.replace(",", ".")) / 3600
        if hemi and hemi.upper() in ("S", "W", "Ю", "З"):
            val = -val
        out.append(val)
    return out


def parse_location(*values: Any) -> Dict[str, Any]:
    """Coordinates from one or more cells: «41.31, 69.24», two numeric cells, comma decimals,
    swapped order, Google/Yandex links, 41°18'39.9"N 69°14'26.0"E.

    → {lat, lon, raw, error, swapped, coarse}
    """
    parts: List[str] = []
    for v in values:
        if _is_blank(v) or isinstance(v, bool):
            continue
        parts.append(repr(float(v)) if isinstance(v, (int, float)) else str(v).strip())
    res: Dict[str, Any] = {"lat": None, "lon": None, "raw": None, "error": None, "swapped": False, "coarse": False}
    if not parts:
        return res
    raw = " ".join(parts)
    res["raw"] = raw[:500]
    text = unquote(raw).replace("−", "-").replace("\u00a0", " ")

    pair = None
    if re.search(r"[°º˚]", text):
        pair = _pick_pair(_dms_values(text))
    if not pair:
        pair = _pick_pair([float(x) for x in _NUM_DOT.findall(text)])
    if not pair:
        pair = _pick_pair([float(x.replace(",", ".")) for x in _NUM_COMMA.findall(text)])
    if pair:
        lat, lon, swapped = pair
        # A yandex «ll=lon,lat» link is in lon,lat order by design — not a data error.
        res.update(lat=round(lat, 7), lon=round(lon, 7), swapped=swapped and "ll=" not in text)
        res["coarse"] = all(abs(x * 100 - round(x * 100)) < 1e-6 for x in (lat, lon))
        return res
    if re.search(r"goo\.gl|maps\.app|yandex\.\w+/maps/-/", text):
        res["error"] = "Короткая ссылка без координат — укажите числа (широта, долгота)"
    elif re.search(r"\d", text):
        res["error"] = f"Координаты вне Узбекистана или неполные: «{text[:80]}»"
    else:
        res["error"] = f"Не удалось распознать координаты: «{text[:80]}»"
    return res


# ── Sheet reading ───────────────────────────────────────────

def _sheet_grid(ws) -> List[List[Any]]:
    """Cell values with merged ranges filled — a merged БХМ cell belongs to every row it spans."""
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    width = max((len(r) for r in grid), default=0)
    for r in grid:
        r.extend([None] * (width - len(r)))
    for rng in ws.merged_cells.ranges:
        value = ws.cell(rng.min_row, rng.min_col).value
        for ri in range(rng.min_row - 1, min(rng.max_row, len(grid))):
            for ci in range(rng.min_col - 1, min(rng.max_col, width)):
                grid[ri][ci] = value
    return grid


def _find_header(grid: List[List[Any]]) -> Tuple[Optional[int], Dict[int, str]]:
    best_row, best_map, best_score = None, {}, 1
    for ri in range(min(HEADER_SCAN_ROWS, len(grid))):
        mapping = _assign(grid[ri])
        sc = _score(mapping)
        if sc > best_score:
            best_row, best_map, best_score = ri, mapping, sc
    if best_row is None:
        return None, {}

    # Two-row header: «Локация» over «Кенглик | Узунлик», «БХМ» over «коди | номи».
    nxt = best_row + 1
    if nxt < len(grid) and not _row_is_data_like(grid[nxt]) and any(isinstance(v, str) and v.strip() for v in grid[nxt]):
        def joined(c: int) -> Optional[str]:
            top, sub = _clean_text(grid[best_row][c]), _clean_text(grid[nxt][c])
            if top and sub and top != sub:        # equal = one cell merged over both rows
                return f"{top} {sub}"
            return top or sub
        combined = [joined(c) for c in range(len(grid[best_row]))]
        cmap = _assign(combined)
        if _score(cmap) >= best_score and len(set(cmap.values())) > len(set(best_map.values())):
            return nxt, cmap
    return best_row, best_map


def _data_rows(grid: List[List[Any]], start: int, limit: int = 300) -> List[List[Any]]:
    return [r for r in grid[start:start + limit] if any(not _is_blank(v) for v in r)]


def _detect_code_column(rows: List[List[Any]], skip: set[int], known_codes: Optional[set[str]]) -> Optional[int]:
    width = max((len(r) for r in rows), default=0)
    best, best_key = None, None
    for c in range(width):
        if c in skip:
            continue
        vals = [r[c] for r in rows if c < len(r) and not _is_blank(r[c])]
        if len(vals) < 2:
            continue
        codes = [x for x in (_code_from(v) for v in vals) if x]
        if len(codes) < 0.6 * len(vals):
            continue
        # Floats like 41.31 are coordinates, not codes.
        if any(isinstance(v, float) and not v.is_integer() for v in vals):
            continue
        known = sum(1 for x in codes if known_codes and x in known_codes)
        repeats = len(codes) - len(set(codes))
        key = (known, repeats)
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best


def _detect_location_columns(rows: List[List[Any]], skip: set[int]) -> List[int]:
    width = max((len(r) for r in rows), default=0)
    cand = [c for c in range(width) if c not in skip]
    for c in cand:
        vals = [r[c] for r in rows if c < len(r) and not _is_blank(r[c])]
        if len(vals) >= 2 and sum(1 for v in vals if parse_location(v)["lat"] is not None) >= 0.5 * len(vals):
            return [c]
    for c in cand:
        if c + 1 not in cand:
            continue
        pairs = [(r[c], r[c + 1]) for r in rows if c + 1 < len(r) and not _is_blank(r[c]) and not _is_blank(r[c + 1])]
        if len(pairs) >= 2 and sum(1 for a, b in pairs if parse_location(a, b)["lat"] is not None) >= 0.5 * len(pairs):
            return [c, c + 1]
    return []


def _headerless_mapping(grid: List[List[Any]], known_codes: Optional[set[str]]) -> Tuple[int, Dict[int, str]]:
    """No header row: anchor on the БХМ code column, the rest follows the agreed column order
    (№ | ВАШ | БХМ коди | БХМ номи | Манзил | Локация)."""
    first = next((i for i, r in enumerate(grid) if any(not _is_blank(v) for v in r)), 0)
    rows = _data_rows(grid, first)
    code_col = _detect_code_column(rows, set(), known_codes)
    if code_col is None:
        return first, {}
    mapping = {code_col: "bxm_code"}
    if code_col >= 1:
        mapping[code_col - 1] = "vash_number"
    if code_col >= 2:
        mapping[code_col - 2] = "row_no"
    mapping[code_col + 1] = "bxm_name"
    mapping[code_col + 2] = "address"
    for c in _detect_location_columns(rows, set(mapping)):
        mapping[c] = "location"
    return first - 1, mapping


def _columns_info(mapping: Dict[int, str]) -> Dict[str, str]:
    return {get_column_letter(c + 1): f for c, f in sorted(mapping.items())}


def _record_key(rec: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
    if rec.get("vash_number"):
        return ("vash", re.sub(r"\s+", "", str(rec["vash_number"])).upper())
    if rec.get("address") or rec.get("lat") is not None:
        return ("addr", rec.get("bxm_code") or search_fold(rec.get("bxm_name")),
                search_fold(rec.get("address")), rec.get("lat"), rec.get("lon"))
    return None


def _parse_sheet(ws, known_codes: Optional[set[str]]) -> Dict[str, Any]:
    sheet = ws.title.strip()
    info: Dict[str, Any] = {"sheet": sheet, "records": [], "errors": [], "warnings": [],
                            "header_row": None, "columns": {}, "skipped": None,
                            "coords_swapped": 0, "coords_coarse": 0, "skipped_rows": 0}
    if getattr(ws, "sheet_state", "visible") != "visible":
        info["skipped"] = "скрытый лист"
        return info
    grid = _sheet_grid(ws)
    if not grid:
        info["skipped"] = "пустой лист"
        return info

    header_idx, mapping = _find_header(grid)
    headerless = header_idx is None
    if headerless:
        header_idx, mapping = _headerless_mapping(grid, known_codes)
    fields = set(mapping.values())
    if "vash_count" in fields and not fields & {"vash_number", "address", "location", "lat", "lon"}:
        info["skipped"] = "сводная таблица (количество ВАШ по филиалам), а не список ВАШ"
        return info

    start = header_idx + 1
    rows_for_detect = _data_rows(grid, start)
    if "bxm_code" not in fields:
        col = _detect_code_column(rows_for_detect, {c for c, f in mapping.items() if f != "bxm_name"}, known_codes)
        if col is not None:
            mapping[col] = "bxm_code"
    if not fields & {"location", "lat", "lon"}:
        for c in _detect_location_columns(rows_for_detect, set(mapping)):
            mapping[c] = "location"
    fields = set(mapping.values())

    if not fields & {"bxm_code", "bxm_name", "vash_number"}:
        info["skipped"] = "не найдены колонки «БХМ коди» / «БХМ номи»"
        return info
    if not fields & {"vash_number", "address", "location", "lat", "lon"}:
        info["skipped"] = "не найдены колонки ВАШ (рақами / манзил / локация)"
        return info

    info["header_row"] = None if headerless else header_idx + 1
    info["columns"] = _columns_info(mapping)
    cols: Dict[str, List[int]] = {}
    for c, f in sorted(mapping.items()):
        cols.setdefault(f, []).append(c)

    def get(row: List[Any], field: str) -> Any:
        c = cols.get(field)
        return row[c[0]] if c and c[0] < len(row) else None

    header_rows = [grid[i] for i in range(max(0, header_idx - 1), header_idx + 1)] if not headerless else []
    header_folds = [{c: search_fold(hr[c]) for c in mapping if c < len(hr) and not _is_blank(hr[c])} for hr in header_rows]

    def is_header_repeat(row: List[Any]) -> bool:
        return any(sum(1 for c, h in hf.items() if c < len(row) and search_fold(row[c]) == h) >= 2 for hf in header_folds)

    section: Optional[Tuple[Optional[str], Optional[str]]] = None
    for ri in range(start, len(grid)):
        row = grid[ri]
        excel_row = ri + 1
        if all(_is_blank(v) for v in row):
            continue
        if is_header_repeat(row):               # header repeated on a printed page
            continue
        filled = [v for v in row if not _is_blank(v)]
        if len(filled) >= 3 and all(isinstance(v, int) and not isinstance(v, bool) for v in filled) \
                and filled == list(range(filled[0], filled[0] + len(filled))):
            continue                            # column numbering under the header
        if len(filled) == 1 or len({_clean_text(v) for v in filled}) == 1:
            # The whole row is one (merged) cell: a group title such as «10463 Юнусобод БХМ»
            # or «Тошкент шаҳри» — never a ВАШ itself.
            text = _clean_text(filled[0]) or ""
            folded = search_fold(text)
            if isinstance(filled[0], str) and not _TOTAL_RE.match(folded):
                tcode = _code_from(text) if _CODE_RE.match(text) else None
                if tcode or any(k in folded for k in _BXM_KW):
                    section = (tcode, text)
                else:
                    info["warnings"].append({"row": excel_row, "sheet": sheet,
                                             "warning": f"Строка-заголовок пропущена: «{text[:80]}»"})
            info["skipped_rows"] += 1
            continue

        raw_code = get(row, "bxm_code")
        name = _clean_text(get(row, "bxm_name"))
        code = _code_from(raw_code) or (_code_from(name) if name and _CODE_RE.match(name) else None)
        vash_no = _clean_text(get(row, "vash_number"))
        address = _clean_text(get(row, "address"))
        row_no = _row_no_from(get(row, "row_no"))
        if "lat" in cols or "lon" in cols:
            loc = parse_location(get(row, "lat"), get(row, "lon"), *[row[c] for c in cols.get("location", []) if c < len(row)])
        else:
            loc = parse_location(*[row[c] for c in cols.get("location", []) if c < len(row)])
        has_details = bool(vash_no or address or loc["raw"])

        if not code and any(_TOTAL_RE.match(search_fold(v)) for v in row if isinstance(v, str) and v.strip()):
            info["skipped_rows"] += 1           # «Жами» / «Итого»
            continue
        if not _is_blank(raw_code) and not code and str(raw_code).strip() not in ("-", "—", "–"):
            info["errors"].append({"row": excel_row, "sheet": sheet, "error": f"БХМ коди не распознан: «{raw_code}»"})

        if (code or name) and not has_details and row_no is None:
            section = (code, name)              # «10463 Юнусобод БХМ» group title above its ВАШ rows
            continue
        if not code and not name:
            if section and has_details:
                code, name = section
            elif has_details:
                info["errors"].append({"row": excel_row, "sheet": sheet, "vash_number": vash_no,
                                       "error": "Нет БХМ кода и названия — ВАШ не к чему привязать, строка пропущена"})
                continue
            else:
                continue

        if loc["error"]:
            info["errors"].append({"row": excel_row, "sheet": sheet, "vash_number": vash_no, "error": loc["error"]})
        if loc["swapped"]:
            info["coords_swapped"] += 1
        if loc["coarse"]:
            info["coords_coarse"] += 1
            info["warnings"].append({"row": excel_row, "sheet": sheet, "vash_number": vash_no,
                                     "warning": "Координаты с точностью до 2 знаков (~1 км)"})
        if not has_details:
            info["warnings"].append({"row": excel_row, "sheet": sheet,
                                     "warning": "Нет ни номера ВАШ, ни адреса, ни координат"})

        info["records"].append({
            "row_no": row_no,
            "vash_number": vash_no,
            "bxm_code": code,
            "bxm_name": name,
            "address": address,
            "lat": loc["lat"],
            "lon": loc["lon"],
            "location_raw": loc["raw"],
            "sheet": sheet,
            "source_row": excel_row,
        })
    return info


def parse_vash_xlsx(path: str | Path, known_codes: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Reads every visible sheet that holds a ВАШ list; copies of an already read sheet are skipped."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    known = {str(c) for c in known_codes} if known_codes else None
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:  # noqa: BLE001 — corrupted / password-protected / not an xlsx
        raise ValueError(f"Не удалось открыть Excel: {exc}") from exc

    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    sheets: List[Dict[str, Any]] = []
    seen: Dict[Tuple[Any, ...], Tuple[str, int]] = {}
    swapped = coarse = skipped_rows = 0
    try:
        for ws in wb.worksheets:
            info = _parse_sheet(ws, known)
            recs = info.pop("records")
            sheet_meta = {k: info[k] for k in ("sheet", "header_row", "columns", "skipped")}
            keys = [_record_key(r) for r in recs]
            dup = sum(1 for k in keys if k and k in seen)
            if recs and dup >= 0.9 * len(recs):
                first_sheet = seen[next(k for k in keys if k and k in seen)][0]
                sheet_meta["skipped"] = f"копия листа «{first_sheet}»"
                sheet_meta["records"] = 0
                sheets.append(sheet_meta)
                continue
            added = 0
            for rec, key in zip(recs, keys):
                if key and key in seen:
                    first_sheet, first_row = seen[key]
                    errors.append({"row": rec["source_row"], "sheet": rec["sheet"], "vash_number": rec["vash_number"],
                                   "error": f"Дубликат ВАШ (уже есть: лист «{first_sheet}», стр. {first_row}) — пропущен"})
                    continue
                if key:
                    seen[key] = (rec["sheet"], rec["source_row"])
                records.append(rec)
                added += 1
            errors.extend(info["errors"])
            warnings.extend(info["warnings"])
            swapped += info["coords_swapped"]
            coarse += info["coords_coarse"]
            skipped_rows += info["skipped_rows"]
            sheet_meta["records"] = added
            sheets.append(sheet_meta)
    finally:
        wb.close()

    if not records:
        reasons = "; ".join(f"«{s['sheet']}»: {s['skipped'] or 'нет строк с ВАШ'}" for s in sheets)
        raise ValueError(
            "В файле не найден список ВАШ. Нужны колонки «БХМ коди» (или «БХМ номи») и хотя бы одна из: "
            f"«ВАШ рақами», «Манзил», «Локация». Листы — {reasons}"
        )
    errors.sort(key=lambda e: (e.get("sheet") or "", e.get("row") or 0))
    return {
        "records": records,
        "errors": errors,
        "warnings": warnings,
        "sheets": sheets,
        "coords_swapped": swapped,
        "coords_coarse": coarse,
        "skipped_total_rows": skipped_rows,
    }


# ── Storage ─────────────────────────────────────────────────

def create_vash_tables(conn) -> None:
    create_dataset_import_tables(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vash_zones (
            id           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            row_no       INTEGER,
            vash_number  TEXT,
            bxm_code     TEXT,
            bxm_name     TEXT,
            address      TEXT,
            lat          DOUBLE PRECISION,
            lon          DOUBLE PRECISION,
            location_raw TEXT,
            sheet        TEXT,
            source_row   INTEGER,
            imported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vash_bxm ON vash_zones(bxm_code)")
    ensure_import_column(conn, "vash_zones")
    adopt_unassigned(conn, KIND_VASH_ZONES, "vash_zones")
    restore_original_import_time(conn, KIND_VASH_ZONES, "vash_zones")


def known_branch_codes() -> set[str]:
    ensure_schema()
    with connect() as conn:
        return {str(r["local_code"]).strip() for r in conn.execute("SELECT local_code FROM branches").fetchall()
                if r["local_code"]}


def list_vash_zone_imports() -> List[Dict[str, Any]]:
    return list_dataset_imports(KIND_VASH_ZONES)


def delete_vash_zone_import(import_id: int) -> bool:
    return delete_dataset_import(KIND_VASH_ZONES, import_id)


def replace_vash_zones(records: List[Dict[str, Any]], filename: Optional[str] = None) -> Dict[str, Any]:
    """Yangi fayl alohida raqamlangan import. Eski shohobchalar o'chmaydi va qo'shilmaydi."""
    ensure_schema()
    with connect() as conn:
        imp = start_import(conn, KIND_VASH_ZONES, filename, len(records), None, None)
        cols = FIELDS + ("import_id",)
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO vash_zones ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                [tuple(rec.get(f) for f in FIELDS) + (imp["id"],) for rec in records],
            )
    data = vash_by_branch(imp["id"])
    return {
        "saved": len(records),
        "matched": data["matched"],
        "unmatched": data["total"] - data["matched"],
        "branches_matched": len(data["branches"]),
        "with_coords": data["with_coords"],
        "import_id": imp["id"],
        "seq": imp["seq"],
        "label": imp["label"],
    }


def clear_vash_zones() -> int:
    ensure_schema()
    with connect() as conn:
        n = int(conn.execute("SELECT COUNT(*) AS n FROM vash_zones").fetchone()["n"])
        conn.execute("DELETE FROM dataset_imports WHERE kind = %s", (KIND_VASH_ZONES,))
        conn.execute("DELETE FROM vash_zones WHERE import_id IS NULL")
    return n


def count_vash_zones(import_id: Optional[int] = None) -> int:
    current = resolve_import(KIND_VASH_ZONES, import_id)
    if current is None:
        return 0
    with connect() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM vash_zones WHERE import_id = %s",
            (current["id"],),
        ).fetchone()["n"])


# ── Branch matching ─────────────────────────────────────────

_NAME_NOISE = {"bxm", "bxo", "filial", "filiali", "markazi", "markaz", "mintaqaviy", "ofis", "ofisi", "bank"}


def _name_key(name: Any) -> str:
    return " ".join(t for t in search_fold(name).split() if t not in _NAME_NOISE and not t.isdigit())


def _branch_name(address: Optional[str]) -> str:
    """Название филиала — начало адреса до первой точки («Юнусобод БХМ. 100084 …»)."""
    return str(address or "").split(".", 1)[0].strip()


def _match(code: Optional[str], name: Optional[str], codes: Dict[str, str], names: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """→ (local_code, method). A code that is not in the registry is never re-guessed by name."""
    if code:
        if code in codes:
            return code, "bxm_code"
        hits = [c for c in codes if c.lstrip("0") == code.lstrip("0")]
        return (hits[0], "bxm_code") if len(hits) == 1 else (None, None)
    key = _name_key(name)
    if not key:
        return None, None
    if key in names:
        return names[key], "name"
    hits = {v for k, v in names.items() if k and min(len(k), len(key)) >= 4 and (k in key or key in k)}
    if len(hits) == 1:
        return hits.pop(), "name"
    scored = sorted(((SequenceMatcher(None, key, k).ratio(), v) for k, v in names.items()), reverse=True)
    if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        return scored[0][1], "name"
    return None, None


def vash_by_branch(import_id: Optional[int] = None) -> Dict[str, Any]:
    """ВАШ по филиалам. По умолчанию — последний импорт списка шохобча."""
    current = resolve_import(KIND_VASH_ZONES, import_id)
    if import_id is not None and current is None:
        raise LookupError("Импорт не найден")
    if current is None:
        return {
            "branches": [], "unmatched": [], "total": 0, "matched": 0,
            "with_coords": 0, "branches_count": 0, "import": None,
        }
    with connect() as conn:
        branches = conn.execute("SELECT local_code, number, region, address, lat, lon FROM branches").fetchall()
        rows = conn.execute(
            "SELECT * FROM vash_zones WHERE import_id = %s ORDER BY id",
            (current["id"],),
        ).fetchall()
    by_code = {str(b["local_code"]).strip(): b for b in branches if b.get("local_code")}
    codes = {c: c for c in by_code}
    names: Dict[str, str] = {}
    for c, b in by_code.items():
        key = _name_key(_branch_name(b.get("address")))
        if key:
            names.setdefault(key, c)

    groups: Dict[str, Dict[str, Any]] = {}
    unmatched: Dict[str, Dict[str, Any]] = {}
    with_coords = matched = 0
    for r in rows:
        local, method = _match(r.get("bxm_code"), r.get("bxm_name"), codes, names)
        item = {k: r.get(k) for k in ("id", "row_no", "vash_number", "bxm_code", "bxm_name", "address",
                                       "lat", "lon", "location_raw", "sheet", "source_row")}
        item["match_method"] = method
        item["local_code"] = local
        if r.get("lat") is not None and r.get("lon") is not None:
            with_coords += 1
        if local:
            matched += 1
            g = groups.get(local)
            if g is None:
                b = by_code[local]
                g = groups[local] = {
                    "local_code": local,
                    "matched": True,
                    "branch_name": _branch_name(b.get("address")),
                    "bxm_name": r.get("bxm_name"),
                    "number": b.get("number"),
                    "region": b.get("region"),
                    "address": b.get("address"),
                    "lat": b.get("lat"),
                    "lon": b.get("lon"),
                    "count": 0,
                    "with_coords": 0,
                    "items": [],
                }
        else:
            ukey = r.get("bxm_code") or f"name:{_name_key(r.get('bxm_name'))}"
            g = unmatched.get(ukey)
            if g is None:
                g = unmatched[ukey] = {
                    "local_code": None,
                    "matched": False,
                    "bxm_code": r.get("bxm_code"),
                    "bxm_name": r.get("bxm_name"),
                    "count": 0,
                    "with_coords": 0,
                    "items": [],
                }
        g["count"] += 1
        if item["lat"] is not None and item["lon"] is not None:
            g["with_coords"] += 1
        g["items"].append(item)

    return {
        "branches": sorted(groups.values(), key=lambda g: (-g["count"], g["local_code"])),
        "unmatched": sorted(unmatched.values(), key=lambda g: -g["count"]),
        "total": len(rows),
        "matched": matched,
        "with_coords": with_coords,
        "branches_count": len(groups),
        "import": current,
    }


def vash_counts_by_local_code() -> Dict[str, int]:
    return {g["local_code"]: g["count"] for g in vash_by_branch()["branches"]}
