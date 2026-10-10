import copy
import json
from datetime import date, datetime
from types import SimpleNamespace as NS

import httpx2
import openai
import pytest

from app import ai, repo, services

TODAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 14, 0)


def usage():
    # у OpenAI input_tokens включает кэшированные
    return NS(input_tokens=1000, output_tokens=20,
              input_tokens_details=NS(cached_tokens=900, cache_write_tokens=0))


class Item(NS):
    def model_dump(self, **kw):
        return {k: v for k, v in vars(self).items()}


def tool(name, args, i=1):
    return Item(type="function_call", id=f"fc{i}", call_id=f"c{i}", name=name, arguments=json.dumps(args))


def text(t):
    return Item(type="message", id="m1", role="assistant", content=[NS(type="output_text", text=t)])


def resp(*items, incomplete=False):
    return NS(output=list(items), status="incomplete" if incomplete else "completed",
              incomplete_details=NS(reason="max_output_tokens") if incomplete else None, usage=usage())


class FakeClient:
    def __init__(self, responses):
        self.queue = list(responses)
        self.calls = []
        self.responses = NS(create=self.create)

    async def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def db(monkeypatch):
    _, targets, plan = services.load_seed("seed")
    state = {"logs": [], "usage": [], "msgs": [], "profile": "профиль"}
    prof = NS(profile_text="профиль", targets=targets, settings={})

    async def get_profile():
        return prof

    async def get_plan_for(d):
        return NS(data=plan, week_start=services.week_start(d))

    async def day_logs(d):
        return state["logs"]

    async def recent_messages(n):
        return []

    async def add_usage(*a):
        state["usage"].append(a)

    async def add_messages(*pairs):
        state["msgs"].extend(pairs)

    async def add_food(d, description, items, **kw):
        e = NS(id=len(state["logs"]) + 1, date=d, description=description, items=items,
               slot=kw.get("slot"), status=kw.get("status", "eaten"), **services.sum_items(items))
        state["logs"].append(e)
        return e

    async def update_profile_text(text):
        state["profile"] = services.check_profile(text)

    for name, fn in [("get_profile", get_profile), ("get_plan_for", get_plan_for), ("day_logs", day_logs),
                     ("recent_messages", recent_messages), ("add_usage", add_usage),
                     ("add_messages", add_messages), ("add_food", add_food),
                     ("update_profile_text", update_profile_text)]:
        monkeypatch.setattr(repo, name, fn)
    return state


async def test_log_food_flow(db):
    items = [{"name": "гречка", "grams": 200, "kcal": 220, "protein": 8, "fat": 2, "carbs": 42},
             {"name": "куриная грудка", "grams": 150, "kcal": 250, "protein": 46, "fat": 5, "carbs": 0}]
    client = FakeClient([
        resp(tool("log_food", {"description": "гречка и грудка", "items": items})),
        resp(text("Записал: гречка и грудка, 470 ккал")),
    ])
    res = await ai.run_agent("съел 200 г гречки и куриную грудку", NOW, client)
    assert res.text.startswith("Записал")
    assert res.entry_ids == [1]
    assert db["logs"][0].kcal == 470 and db["logs"][0].protein == 54   # сумму считает код
    assert db["usage"][0][1:] == (100, 20, 900, 0)                     # обычный вход = 1000 − 900 из кэша
    assert len(db["usage"]) == 2                                       # учёт токенов каждого вызова
    first = client.calls[0]
    assert first["max_output_tokens"] == ai.MAX_TOKENS
    assert first["reasoning"] == {"effort": "none"}
    assert first["prompt_cache_options"] == {"mode": "explicit"}
    static, ctx = first["input"][0], first["input"][1]
    assert static["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}   # кэш — только статика
    assert ctx["content"].startswith("Контекст")
    second = client.calls[1]["input"]
    assert second[-2]["type"] == "function_call" and "id" not in second[-2]
    assert second[-1] == {"type": "function_call_output", "call_id": "c1", "output": second[-1]["output"]}
    assert db["msgs"][0] == ("user", "съел 200 г гречки и куриную грудку")


async def test_invalid_values_go_back_to_model(db):
    bad = [{"name": "шаурма", "kcal": -500, "protein": 20, "fat": 20, "carbs": 40}]
    client = FakeClient([
        resp(tool("log_food", {"description": "шаурма", "items": bad})),
        resp(text("Не смог записать")),
    ])
    res = await ai.run_agent("съел шаурму", NOW, client)
    result = client.calls[1]["input"][-1]
    assert result["output"].startswith("ОШИБКА")
    assert db["logs"] == [] and res.entry_ids == []


async def test_loop_is_limited(db, monkeypatch):
    client = FakeClient([resp(tool("get_stats", {"days": 1}, i)) for i in range(10)])
    async def stats(*a, **k):
        return []
    async def weights(*a):
        return []
    async def plans(*a):
        return {}
    monkeypatch.setattr(repo, "logs_between", stats)
    monkeypatch.setattr(repo, "weights", weights)
    monkeypatch.setattr(repo, "plans_by_date", plans)
    res = await ai.run_agent("как я ел?", NOW, client)
    assert len(client.calls) == ai.MAX_ITER
    assert client.calls[-1]["tool_choice"] == "none"
    assert res.text


async def test_plan_request_gets_big_budget(db):
    client = FakeClient([resp(text("ок"))])
    await ai.run_agent("составь план на следующую неделю", NOW, client)
    assert client.calls[0]["max_output_tokens"] == ai.MAX_TOKENS_PLAN


async def test_retry_on_max_tokens(db):
    client = FakeClient([resp(text("обрезано"), incomplete=True),
                         resp(text("полный ответ"))])
    res = await ai.run_agent("вопрос", NOW, client)
    assert [c["max_output_tokens"] for c in client.calls] == [ai.MAX_TOKENS, ai.MAX_TOKENS_PLAN]
    assert res.text == "полный ответ"


async def test_profile_limit_enforced(db):
    client = FakeClient([
        resp(tool("update_profile", {"text": "я" * 2000})),
        resp(tool("update_profile", {"text": "не любит творог"}, 2)),
        resp(text("Запомнил: не любишь творог")),
    ])
    await ai.run_agent("не люблю творог", NOW, client)
    assert client.calls[1]["input"][-1]["output"].startswith("ОШИБКА")
    assert db["profile"] == "не любит творог"


async def test_api_error_is_friendly(db):
    err = openai.APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com"))
    client = FakeClient([err])
    res = await ai.run_agent("привет", NOW, client)
    assert "не ответил вовремя" in res.text


def test_history_starts_with_user():
    rows = [NS(role="assistant", content="a"), NS(role="user", content="u1"),
            NS(role="user", content="u2"), NS(role="assistant", content="b")]
    assert ai.history_messages(rows) == [{"role": "user", "content": "u1\nu2"},
                                         {"role": "assistant", "content": "b"}]


def test_plan_regex():
    assert ai.PLAN_RE.search("составь план на следующую неделю")
    assert ai.PLAN_RE.search("сделай новый план")
    assert not ai.PLAN_RE.search("съел 200 г гречки")
