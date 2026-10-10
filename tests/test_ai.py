import copy
from datetime import date, datetime
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from app import ai, repo, services

TODAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 14, 0)


def usage():
    return NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=900, cache_creation_input_tokens=0)


def tool(name, args, i=1):
    return NS(type="tool_use", id=f"t{i}", name=name, input=args)


def resp(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop, usage=usage())


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    async def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        r = self.responses.pop(0)
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
        resp(tool("log_food", {"description": "гречка и грудка", "items": items}), stop="tool_use"),
        resp(NS(type="text", text="Записал: гречка и грудка, 470 ккал")),
    ])
    res = await ai.run_agent("съел 200 г гречки и куриную грудку", NOW, client)
    assert res.text.startswith("Записал")
    assert res.entry_ids == [1]
    assert db["logs"][0].kcal == 470 and db["logs"][0].protein == 54   # сумму считает код
    assert len(db["usage"]) == 2 and db["usage"][0][3] == 900          # учёт токенов каждого вызова
    first = client.calls[0]
    assert first["max_tokens"] == ai.MAX_TOKENS
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Контекст" in first["system"][1]["text"] and "cache_control" not in first["system"][1]
    assert db["msgs"][0] == ("user", "съел 200 г гречки и куриную грудку")


async def test_invalid_values_go_back_to_model(db):
    bad = [{"name": "шаурма", "kcal": -500, "protein": 20, "fat": 20, "carbs": 40}]
    client = FakeClient([
        resp(tool("log_food", {"description": "шаурма", "items": bad}), stop="tool_use"),
        resp(NS(type="text", text="Не смог записать")),
    ])
    res = await ai.run_agent("съел шаурму", NOW, client)
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert db["logs"] == [] and res.entry_ids == []


async def test_loop_is_limited(db, monkeypatch):
    client = FakeClient([resp(tool("get_stats", {"days": 1}, i), stop="tool_use") for i in range(10)])
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
    assert client.calls[-1]["tool_choice"] == {"type": "none"}
    assert res.text


async def test_plan_request_gets_big_budget(db):
    client = FakeClient([resp(NS(type="text", text="ок"))])
    await ai.run_agent("составь план на следующую неделю", NOW, client)
    assert client.calls[0]["max_tokens"] == ai.MAX_TOKENS_PLAN


async def test_retry_on_max_tokens(db):
    client = FakeClient([resp(NS(type="text", text="обрезано"), stop="max_tokens"),
                         resp(NS(type="text", text="полный ответ"))])
    res = await ai.run_agent("вопрос", NOW, client)
    assert [c["max_tokens"] for c in client.calls] == [ai.MAX_TOKENS, ai.MAX_TOKENS_PLAN]
    assert res.text == "полный ответ"


async def test_profile_limit_enforced(db):
    client = FakeClient([
        resp(tool("update_profile", {"text": "я" * 2000}), stop="tool_use"),
        resp(tool("update_profile", {"text": "не любит творог"}, 2), stop="tool_use"),
        resp(NS(type="text", text="Запомнил: не любишь творог")),
    ])
    await ai.run_agent("не люблю творог", NOW, client)
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    assert db["profile"] == "не любит творог"


async def test_api_error_is_friendly(db):
    err = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com"))
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
