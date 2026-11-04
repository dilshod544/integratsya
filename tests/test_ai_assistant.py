"""ИИ-помощник: в модель не должно уходить ни одного значения или названия из проекта."""
import json
import re

import pytest

from api.core import ai_assistant as ai

SECRET_NAMES = ["Чиланзар-7 квартал", "S1A9X77", "Каримов Азиз", "BXM Юнусобод"]


def _summary():
    return {
        "period_days": 90,
        "settings": {"items": [{"key": "trip_cost_uzs", "label": "Стоимость выезда"}], "missing": []},
        "potential": {
            "cycles": 4321, "atms": 187, "saving": 987654321.0, "saving_per_month": 55443322.0,
            "freed_cash": 7777777777.0, "bank_avg_cash": 31313131313.0, "system_avg_cash": 23535353536.0,
            "bank_trips": 6543, "system_trips": 4567.8, "bank_cost": 123123123.0, "system_cost": 45645645.0,
            "carrying_saving": 9191919.0, "cashout_hours": 862.4, "lost_withdrawals": 3456789.0,
            "lost_income": 34567.0, "by_method": {"recorded": 1212, "backtest": 3109},
        },
        "realized": {"cycles": 3, "saving": 4242424.0},
        "atms": [{"tid": "S1A9X77", "address": "Чиланзар-7 квартал", "freed_cash": 818181818.0}],
    }


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(ai, "_load_entities", lambda: sorted(
        set(SECRET_NAMES) | {"Каримов", "Азиз"}, key=len, reverse=True))
    ai._entity_cache.update({"at": 0.0, "re": None})
    monkeypatch.setattr(ai.savings, "savings_summary", lambda days: _summary())
    sent = {}

    class Resp:
        status_code, ok = 200, True

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "Экономия {{saving_month}}, освободить {{freed_cash}}, лидер {{top1_atm}}."}}]}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, payload=json, headers=headers)
        return Resp()

    monkeypatch.setattr(ai.requests, "post", post)
    return sent


def _secret_strings():
    vals = ai.placeholder_values(_summary())
    raw_numbers = re.findall(r"\d{3,}", json.dumps(_summary()))
    return set(raw_numbers) | {v for v in vals.values() if re.search(r"\d{3}|\d \d", v)} | set(SECRET_NAMES) | {"Каримов", "Азиз"}


def test_nothing_from_project_reaches_model(fake):
    question = ("Сколько сэкономим на S1A9X77 (Чиланзар-7 квартал)? Каримов Азиз говорит 1 500 000 сум за 3 месяца, "
                "пишите на boss@sqb.uz или +998 90 123 45 67, BXM Юнусобод")
    out = ai.ask([{"role": "user", "content": question},
                  {"role": "assistant", "content": "Было 987 654 321 сум в {{saving_period}}"},
                  {"role": "user", "content": question}], days=90)

    payload = json.dumps(fake["payload"], ensure_ascii=False)
    for s in _secret_strings():
        assert s not in payload, s
    user_texts = [m["content"] for m in fake["payload"]["messages"] if m["role"] != "system"]
    for t in user_texts:
        assert not re.search(r"\d", re.sub(r"\{\{[a-z0-9_]+\}\}", "", t)), t
    assert "boss@sqb.uz" not in payload and "[email]" in payload
    assert "{{saving_period}}" in payload               # плейсхолдеры в истории сохраняются

    assert fake["payload"]["model"] == "z-ai/glm-5.3-flash"
    assert fake["payload"]["provider"] == {"only": ["relace/fp4"], "allow_fallbacks": False,
                                           "data_collection": "deny", "zdr": True}
    assert "user" not in fake["payload"]
    assert set(fake["headers"]) == {"Authorization", "Content-Type"}

    # значения подставляются локально и возвращаются только браузеру
    assert out["values"]["saving_month"] == "55 млн сум"
    assert out["values"]["top1_atm"] == "S1A9X77 (Чиланзар-7 квартал)"
    assert out["raw"].startswith("Экономия {{saving_month}}")
    assert all(not re.search(r"\d", s) for s in out["sent"])


def test_explain_sends_only_fixed_prompt(fake):
    ai.ask([{"role": "user", "content": "S1A9X77 1234"}], days=30, explain=True)
    msgs = fake["payload"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[1]["content"] == ai.EXPLAIN_PROMPT


def test_system_prompt_is_the_same_whatever_the_data(fake):
    assert ai.system_prompt() == ai.system_prompt()
    for key in ai.PLACEHOLDER_KEYS:
        assert "{{" + key + "}}" in ai.system_prompt()


def test_sanitize_keeps_ordinary_words():
    ai._entity_cache.update({"at": 1e18, "re": None})
    assert ai.sanitize("за 90 дней, 12.5% и 1 200 000 сум") == "за [число] дней, [число] и [число] сум"
    assert ai.sanitize("Почему системе хватает меньше наличных?") == "Почему системе хватает меньше наличных?"


def test_without_key_nothing_is_sent(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: pytest.fail("не должно быть запроса"))
    with pytest.raises(ai.AIError) as e:
        ai.ask([{"role": "user", "content": "привет"}], days=90)
    assert e.value.status == 503
