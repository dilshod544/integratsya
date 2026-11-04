"""ИИ-помощник страницы «Экономия» (OpenRouter, z-ai/glm-5.3-flash).

Главное правило: никакие данные проекта не уходят в модель.

  * Модель не видит ни одного числа и ни одного названия. Ей даётся только
    список плейсхолдеров вида {{saving_month}} с описанием, что они значат.
    Модель пишет ответ с плейсхолдерами, а реальные значения подставляются
    локально (в браузере) из словаря `values`, который в модель не отправляется.
    Список плейсхолдеров одинаковый всегда — даже то, заданы ли параметры
    банка, модель не узнаёт.
  * Сообщения пользователя обезличиваются перед отправкой: любые числа,
    e-mail, телефоны, ссылки и все названия из базы (TID, адреса, филиалы,
    ФИО, логины) заменяются метками [число], [скрыто] и т.п.
  * В OpenRouter запрос идёт только к провайдерам без сбора данных и с
    нулевым хранением (data_collection=deny, zdr=true), без user-id,
    Referer и названия приложения.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from . import savings
from .db import connect

log = logging.getLogger(__name__)

MODEL = "z-ai/glm-5.3-flash"          # строго эта модель, не настраивается
PROVIDER = "relace/fp4"               # строго этот провайдер ($0.0269 / $0.0996 за 1M), без запасных
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_HISTORY = 20
MAX_MESSAGE_CHARS = 2000
ENTITY_TTL_S = 600


class AIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ═══════════════════════════════════════════════════════════
# Плейсхолдеры: модель знает только ключ и описание, значение — никогда
# ═══════════════════════════════════════════════════════════

def _money(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    a = abs(v)
    if a >= 1e9:
        s = f"{v / 1e9:.2f}".replace(".", ",") + " млрд"
    elif a >= 1e6:
        s = f"{round(v / 1e6):,}".replace(",", " ") + " млн"
    elif a >= 1e3:
        s = f"{round(v / 1e3):,}".replace(",", " ") + " тыс"
    else:
        s = f"{round(v):,}".replace(",", " ")
    return s + " сум"


def _num(v: Optional[float], d: int = 0) -> Optional[str]:
    if v is None:
        return None
    s = f"{v:,.{d}f}".replace(",", " ").replace(".", ",")
    return s


def _pct(part: Optional[float], whole: Optional[float]) -> Optional[str]:
    if part is None or not whole:
        return None
    return _num(part / whole * 100, 1) + " %"


NO_MONEY = "не посчитано — задайте стоимость выезда и стоимость денег"


def _no_saving(d: Dict[str, Any]) -> str:
    return NO_MONEY if d["potential"]["saving"] is None else "нет данных"

# (ключ, описание для модели, функция значения от summary)
PLACEHOLDERS: List[Tuple[str, str, Callable[[Dict[str, Any]], Optional[str]]]] = [
    ("period_days", "длина выбранного периода, дней",
     lambda d: _num(d["period_days"])),
    ("cycles", "сколько циклов инкассации оценено",
     lambda d: _num(d["potential"]["cycles"])),
    ("atms", "сколько банкоматов в оценке",
     lambda d: _num(d["potential"]["atms"])),
    ("saving_period", "экономия в сумах за период (выезды + стоимость денег)",
     lambda d: _money(d["potential"]["saving"]) or NO_MONEY),
    ("saving_month", "экономия в сумах в месяц по сети",
     lambda d: _money(d["potential"].get("saving_per_month")) or _no_saving(d)),
    ("saving_year", "экономия в сумах в год по сети (месячная × 12)",
     lambda d: _money(d["potential"]["saving_per_month"] * 12) if d["potential"].get("saving_per_month") is not None else _no_saving(d)),
    ("freed_cash", "сколько наличных можно освободить из банкоматов (в среднем по сети)",
     lambda d: _money(d["potential"]["freed_cash"])),
    ("freed_cash_pct", "на сколько процентов меньше наличных держала бы система",
     lambda d: _pct(d["potential"]["freed_cash"], d["potential"]["bank_avg_cash"])),
    ("bank_avg_cash", "сколько наличных банк держит в банкоматах сейчас (в среднем по сети)",
     lambda d: _money(d["potential"]["bank_avg_cash"])),
    ("system_avg_cash", "сколько наличных хватило бы по плану системы (в среднем по сети)",
     lambda d: _money(d["potential"]["system_avg_cash"])),
    ("bank_trips", "выездов инкассации у банка за период (факт)",
     lambda d: _num(d["potential"]["bank_trips"])),
    ("system_trips", "выездов по плану системы на том же спросе",
     lambda d: _num(d["potential"]["system_trips"], 1)),
    ("trips_change_pct", "на сколько процентов меньше выездов у системы (отрицательное — больше)",
     lambda d: _pct((d["potential"]["bank_trips"] or 0) - (d["potential"]["system_trips"] or 0), d["potential"]["bank_trips"])),
    ("bank_cost", "затраты банка за период, сум",
     lambda d: _money(d["potential"]["bank_cost"]) or NO_MONEY),
    ("system_cost", "затраты по плану системы за период, сум",
     lambda d: _money(d["potential"]["system_cost"]) or NO_MONEY),
    ("carrying_saving", "экономия на стоимости хранения лишних денег, сум",
     lambda d: _money(d["potential"]["carrying_saving"]) or "не посчитано — задайте стоимость денег"),
    ("cashout_hours", "сколько часов банкоматы стояли без денег (факт), число без единицы — пиши «часов» после",
     lambda d: _num(d["potential"]["cashout_hours"], 1)),
    ("lost_withdrawals", "сколько снятий не выдано из-за простоя без денег",
     lambda d: _money(d["potential"]["lost_withdrawals"])),
    ("lost_income", "потерянный доход из-за простоя, сум",
     lambda d: _money(d["potential"]["lost_income"]) or "не посчитано — задайте доход со снятия"),
    ("realized", "реализованная экономия по принятым планам",
     lambda d: (_money(d["realized"]["saving"]) if d["realized"].get("saving") is not None
                else (_money(d["realized"].get("freed_cash")) + " меньше наличных" if d["realized"].get("freed_cash") is not None
                      else "пока нет — ещё не было инкассаций по принятому плану"))),
    ("recorded_cycles", "циклов, оценённых по записанным рекомендациям",
     lambda d: _num(d["potential"]["by_method"]["recorded"])),
    ("backtest_cycles", "циклов, оценённых бэктестом по прошлому спросу",
     lambda d: _num(d["potential"]["by_method"]["backtest"])),
    ("missing_params", "какие параметры банка не заданы",
     lambda d: ", ".join(i["label"].lower() for i in d["settings"]["items"] if i["key"] in d["settings"]["missing"]) or "все заданы"),
]
for _i in (1, 2, 3):
    PLACEHOLDERS += [
        (f"top{_i}_atm", f"банкомат №{_i} по лишним деньгам (TID и адрес)",
         lambda d, i=_i: (lambda a: f"{a['tid']}" + (f" ({a['address']})" if a.get("address") else "") if a else None)(
             (d["atms"][i - 1] if len(d["atms"]) >= i else None))),
        (f"top{_i}_freed", f"сколько денег можно освободить в банкомате №{_i}",
         lambda d, i=_i: _money(d["atms"][i - 1]["freed_cash"]) if len(d["atms"]) >= i else None),
    ]
PLACEHOLDER_KEYS = [k for k, _, _ in PLACEHOLDERS]


def placeholder_values(summary: Dict[str, Any]) -> Dict[str, str]:
    """Реальные значения — только для браузера, в модель не уходят."""
    out = {}
    for key, _, fn in PLACEHOLDERS:
        try:
            out[key] = fn(summary) or "нет данных"
        except Exception:  # noqa: BLE001 — пустая база, нет циклов и т.п.
            out[key] = "нет данных"
    return out


SYSTEM_PROMPT = """Ты — помощник на странице «Экономия инкассации» банковской платформы мониторинга банкоматов. Отвечай по-русски, коротко, дружелюбно и по делу.

КОНФИДЕНЦИАЛЬНОСТЬ. Ты не видишь ни одной цифры и ни одного названия банка. Вместо значений используй плейсхолдеры в двойных фигурных скобках — система сама подставит реальные значения на стороне пользователя. Правила:
- Никогда не пиши числа о банке, банкоматах, деньгах, выездах или сроках — только плейсхолдеры из списка ниже, ровно в таком написании: {{ключ}}.
- Не придумывай плейсхолдеры, которых нет в списке. Если нужного значения нет — так и скажи.
- Денежные значения и проценты уже содержат единицы («сум», «млн», «%») — не дописывай их после плейсхолдера.
- Не делай выводов «много/мало/хорошо/плохо» по значениям — ты их не знаешь.
- Метки [число], [скрыто], [email], [телефон], [ссылка] в сообщениях пользователя — это скрытые для приватности данные. Не пытайся их угадать; отвечай в общем виде.
- Если значение может быть «не посчитано» (например, не заданы параметры банка), формулируй так, чтобы фраза читалась и с числом, и с этим текстом.

ДОСТУПНЫЕ ПЛЕЙСХОЛДЕРЫ:
{placeholders}

КАК СЧИТАЕТСЯ ЭКОНОМИЯ (методика страницы):
- Цикл — время между двумя фактическими загрузками банкомата.
- Банк (факт): средний доступный остаток наличных за цикл, один выезд за цикл, часы простоя без денег.
- Система: сумма загрузки из рекомендации, записанной до загрузки; для старых циклов — бэктест той же формулой только по спросу за две недели до начала цикла. Эта сумма проверяется на реальном спросе цикла: хватит на T = (L − S) / w дней, средний остаток S + (L − S) / 2 (L — загрузка, S — страховой запас, w — спрос в день).
- Затраты в день: K / T + r · средний остаток (K — стоимость выезда, r — дневная стоимость денег). Экономия = (затраты банка − затраты системы) × длина цикла.
- «Освобождаемые деньги» — насколько меньше наличных держали бы банкоматы; не зависит от ставок.
- «Реализовано» — циклы по планам, которые бригада приняла на странице «План инкассаций», сравниваются с медианой циклов того же банкомата без плана.
- Без стоимости выезда и стоимости денег экономия в сумах не считается — только натуральные величины. Их задаёт администратор внизу этой страницы в «Параметрах банка».

Форматирование: короткие абзацы, можно **жирный** и маркированные списки «- ». Без заголовков и таблиц."""

EXPLAIN_PROMPT = ("Объясни простыми словами, сколько экономит система и за счёт чего: экономия в сумах, "
                  "освобождаемые наличные, выезды, простой без денег, что уже реализовано. "
                  "Если чего-то не хватает для расчёта — скажи, что задать. До 150 слов.")


def system_prompt() -> str:
    lines = "\n".join(f"- {{{{{k}}}}} — {desc}" for k, desc, _ in PLACEHOLDERS)
    return SYSTEM_PROMPT.replace("{placeholders}", lines)


# ═══════════════════════════════════════════════════════════
# Обезличивание текста пользователя
# ═══════════════════════════════════════════════════════════

# (таблица, колонка, дробить на слова) — всё, что может назвать банкомат, филиал или человека
_ENTITY_COLUMNS = [
    ("atm_monitor.atms", "tid", False), ("atm_monitor.atms", "serial", False),
    ("atm_monitor.atms", "address", False), ("atm_monitor.atms", "place", False),
    ("atm_monitor.atms", "terminal_id", False), ("atm_monitor.atms", "merchant_id", False),
    ("atm_monitor.atms", "mfo", False), ("atm_monitor.atms", "branch_number", False),
    ("public.atms", "terminal_id", False), ("public.atms", "atm_number", False),
    ("public.atms", "branch", False), ("public.atms", "address", False),
    ("public.atms", "serial", False), ("public.atms", "merchant_id", False),
    ("public.atms", "local_code", False), ("public.atms", "branch_code", False),
    ("public.branches", "name", False), ("public.branches", "address", False),
    ("public.branches", "local_code", False), ("public.branches", "number", False),
    ("public.branch_balances", "branch_name", False),
    ("public.cashier_reports", "full_name", True), ("public.cashier_reports", "branch_name", False),
    ("public.users", "username", False), ("public.users", "full_name", True),
    ("public.vash_zones", "vash_number", False), ("public.vash_zones", "bxm_name", False),
    ("public.vash_zones", "address", False),
]

_entity_lock = threading.Lock()
_entity_cache: Dict[str, Any] = {"at": 0.0, "re": None}

_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    (re.compile(r"(?:https?://|www\.)\S+", re.I), "[ссылка]"),
    (re.compile(r"\+\d[\d\s()-]{7,}\d|\b998[\d\s()-]{7,}\d"), "[телефон]"),
    # любые числа, в т.ч. 1 200 000 / 1,5 / 12.06 / 10%; и слова с цифрами (TID, коды)
    (re.compile(r"\w*\d\w*(?:[\s.,:/-]+\w*\d\w*)*%?"), "[число]"),
]


def _load_entities() -> List[str]:
    with connect() as conn:
        existing = {(r["table_schema"] + "." + r["table_name"], r["column_name"]) for r in conn.execute(
            "SELECT table_schema, table_name, column_name FROM information_schema.columns "
            "WHERE table_schema IN ('public', 'atm_monitor')").fetchall()}
        names: set = set()
        for table, col, split in _ENTITY_COLUMNS:
            if (table, col) not in existing:
                continue
            for r in conn.execute(f"SELECT DISTINCT {col} AS v FROM {table} WHERE {col} IS NOT NULL").fetchall():
                v = str(r["v"]).strip()
                if len(v) >= 3:
                    names.add(v)
                if split:
                    names.update(w for w in re.split(r"[\s.,]+", v) if len(w) >= 4)
    return sorted(names, key=len, reverse=True)


def _entity_re() -> Optional[re.Pattern]:
    with _entity_lock:
        if _entity_cache["re"] is None or time.time() - _entity_cache["at"] > ENTITY_TTL_S:
            try:
                names = _load_entities()
            except Exception as e:  # noqa: BLE001 — нет базы: числа/почта всё равно вычищаются
                log.warning("ИИ: не удалось загрузить названия для обезличивания: %s", e)
                names = []
            _entity_cache["re"] = (re.compile("|".join(re.escape(n) for n in names), re.I) if names else None)
            _entity_cache["at"] = time.time()
        return _entity_cache["re"]


def sanitize(text: str) -> str:
    """Убрать из текста всё, что может быть данными банка."""
    text = (text or "")[:MAX_MESSAGE_CHARS]
    ent = _entity_re()
    if ent is not None:
        text = ent.sub("[скрыто]", text)
    for rx, label in _PATTERNS:
        text = rx.sub(label, text)
    return text


_PH_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


def sanitize_assistant(text: str) -> str:
    """Ответы модели в истории оставляем с плейсхолдерами; если клиент прислал
    что-то с цифрами/названиями (подставленные значения) — тоже вычищаем,
    не трогая сами плейсхолдеры."""
    parts = _PH_RE.split(text or "")
    out = []
    for i, p in enumerate(parts):
        out.append("{{" + p + "}}" if i % 2 else sanitize(p))
    return "".join(out)[:MAX_MESSAGE_CHARS * 2]


# ═══════════════════════════════════════════════════════════
# Запрос к модели
# ═══════════════════════════════════════════════════════════

def build_payload(messages: List[Dict[str, str]], explain: bool) -> Tuple[Dict[str, Any], List[str]]:
    """Ровно то, что уйдёт в OpenRouter. Данных проекта тут нет по построению."""
    history = []
    sent_user = []
    for m in messages[-MAX_HISTORY:]:
        role = m.get("role")
        if role == "user":
            clean = sanitize(str(m.get("content", "")))
            sent_user.append(clean)
            history.append({"role": "user", "content": clean})
        elif role == "assistant":
            history.append({"role": "assistant", "content": sanitize_assistant(str(m.get("content", "")))})
    if explain:
        history = [{"role": "user", "content": EXPLAIN_PROMPT}]
    if not history or history[-1]["role"] != "user":
        raise AIError(400, "Нет вопроса")
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system_prompt()}] + history,
        "temperature": 0.3,
        "max_tokens": 2000,
        # У модели размышление обязательно; low + exclude — короткое и не возвращается,
        # иначе оно съедает лимит и ответ обрывается.
        "reasoning": {"effort": "low", "exclude": True},
        "provider": {"only": [PROVIDER], "allow_fallbacks": False, "data_collection": "deny", "zdr": True},
    }
    return payload, sent_user


def ask(messages: List[Dict[str, str]], days: int, explain: bool = False) -> Dict[str, Any]:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise AIError(503, "ИИ не подключён: добавьте OPENROUTER_API_KEY в .env и перезапустите сервер.")
    payload, sent_user = build_payload(messages, explain)
    try:
        resp = requests.post(OPENROUTER_URL, json=payload, timeout=90,
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except requests.RequestException:
        raise AIError(502, "Не удалось связаться с OpenRouter. Проверьте интернет.") from None
    if resp.status_code == 404:
        raise AIError(502, "Сейчас нет провайдера модели без хранения данных — запрос отклонён ради приватности. Попробуйте позже.")
    if resp.status_code in (401, 403):
        raise AIError(502, "OpenRouter не принял ключ OPENROUTER_API_KEY.")
    if resp.status_code == 402:
        raise AIError(502, "На счёте OpenRouter закончились средства.")
    if resp.status_code == 429:
        raise AIError(429, "Слишком много запросов к ИИ — подождите немного.")
    if not resp.ok:
        raise AIError(502, f"OpenRouter вернул ошибку {resp.status_code}.")
    try:
        raw = resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError(502, "Модель вернула пустой ответ.") from None

    values = placeholder_values(savings.savings_summary(days))   # локально, после ответа модели
    return {"raw": raw.strip(), "values": values, "sent": sent_user, "model": MODEL, "provider": PROVIDER}
