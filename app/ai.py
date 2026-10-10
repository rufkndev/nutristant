"""Агент: клиент OpenAI (Responses API), статичный промпт, tools, цикл вызова функций."""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import openai

from app import repo, services, texts
from app.config import get_settings

log = logging.getLogger("ai")

MAX_ITER = 4
MAX_TOKENS = 600
MAX_TOKENS_PLAN = 4000
PLAN_RE = re.compile(r"(состав|сдела|созда|нов\w*|друг\w*).{0,30}план|план.{0,25}(на|для)\s+(след|нов|эту|всю)", re.I)

# Статичный промпт — кэшируется вместе с tools. Ничего динамического сюда не добавлять.
SYSTEM = """Ты — личный ассистент по питанию одного пользователя в Telegram. Его профиль, цели, план и записи за сегодня — в блоке «Контекст».

Стиль: по-русски, практично, дружелюбно, без морализаторства. Коротко: 1–4 строки, если не просят подробно. Обычный текст без Markdown. КБЖУ — приблизительная оценка. По медицинским вопросам — «лучше обсудить с врачом».

Как действовать:
- Пользователь что-то съел → log_food. КБЖУ оценивай по каждой позиции отдельно (граммы, ккал, Б, Ж, У, клетчатка); суммы считает код. Ответ — одна строка «Записал: …». Остаток на день бот допишет сам, не пиши его.
- Съел вместо приёма из плана → log_food со slot этого приёма и status=replaced, плюс одна строка, как поправить остаток дня.
- Съел приём ровно по плану → mark_meal (status=eaten); пропустил → mark_meal (status=skipped).
- «Что поесть?» → 2 варианта под остаток КБЖУ, предпочтения и то, что готовит он раз в неделю.
- В ресторане / выбор из списка → посоветуй 1–2 позиции под остаток дня.
- Новые предпочтения, нелюбимые продукты, расписание, ограничения → update_profile: перепиши профиль целиком, сжато, до 1500 символов, сохранив прежние факты; ответь «Запомнил: …».
- Правка плана → get_plan, затем update_plan_day для каждого изменённого дня. Сохраняй структуру и ингредиенты с граммами (для списка покупок).
- Новый план на неделю → get_plan (взять текущий за основу), затем save_week_plan. План сохраняется черновиком: кратко опиши его (3–5 строк) и спроси «Сохранить?». Не говори, что план уже сохранён.
- «Удали последнюю запись» → delete_entry. «Сохрани в избранное» → add_favorite (по умолчанию последняя запись).
- Вопросы о прошлых днях и динамике → get_stats.
- Не выдумывай данные: чего нет в контексте — бери инструментами. Если инструмент вернул ошибку — исправь вызов или коротко объясни пользователю.

Учитывай профиль: аллергии и нелюбимые продукты никогда не предлагай."""

_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "grams": {"type": "number", "description": "вес порции, г"},
        "kcal": {"type": "number"}, "protein": {"type": "number"}, "fat": {"type": "number"},
        "carbs": {"type": "number"}, "fiber": {"type": "number"},
    },
    "required": ["name", "kcal", "protein", "fat", "carbs"],
}
_DISH = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "kcal": {"type": "number"}, "protein": {"type": "number"}, "fat": {"type": "number"},
        "carbs": {"type": "number"},
        "ingredients": {"type": "array", "items": {"type": "array"},
                        "description": "[[\"Продукт\", граммы], ...] — сырой вес для покупки"},
        "note": {"type": "string", "description": "кратко как готовить"},
    },
    "required": ["name", "kcal", "protein", "fat", "carbs", "ingredients"],
}
_MEALS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "slot": {"type": "string", "description": "breakfast/shake/lunch/snack/dinner/late или свой латиницей"},
            "name": {"type": "string"}, "time": {"type": "string", "description": "ЧЧ:ММ"},
            "dishes": {"type": "array", "items": {}, "description": "id блюда из get_plan (\"d3\") или объект блюда"},
        },
        "required": ["slot", "name", "time", "dishes"],
    },
}
_WEEK = {"type": "string", "enum": ["current", "next"]}
_DATE = {"type": "string", "description": "YYYY-MM-DD, по умолчанию сегодня"}

TOOLS = [
    {"name": "log_food", "description": "Записать съеденное в дневник.",
     "input_schema": {"type": "object", "properties": {
         "description": {"type": "string", "description": "кратко, что съел"},
         "items": {"type": "array", "items": _ITEM},
         "slot": {"type": "string", "description": "slot приёма из плана, если еда относится к нему"},
         "status": {"type": "string", "enum": ["eaten", "replaced"]},
         "date": _DATE},
         "required": ["description", "items"]}},
    {"name": "delete_entry", "description": "Удалить запись дневника по id (из контекста) или последнюю.",
     "input_schema": {"type": "object", "properties": {
         "entry_id": {"type": ["integer", "string"], "description": "id или \"last\""}},
         "required": ["entry_id"]}},
    {"name": "mark_meal", "description": "Отметить приём из плана как съеденный по плану или пропущенный.",
     "input_schema": {"type": "object", "properties": {
         "slot": {"type": "string"}, "status": {"type": "string", "enum": ["eaten", "skipped"]}, "date": _DATE},
         "required": ["slot", "status"]}},
    {"name": "update_profile", "description": "Переписать профиль пользователя целиком (до 1500 символов).",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "get_plan", "description": "План недели в компактном виде: dishes {id: блюдо} и days {mon..sun: приёмы со ссылками на id}.",
     "input_schema": {"type": "object", "properties": {"week": _WEEK}}},
    {"name": "update_plan_day", "description": "Заменить приёмы одного дня в плане (сразу сохраняется).",
     "input_schema": {"type": "object", "properties": {
         "week": _WEEK, "day": {"type": "string", "enum": services.DAY_KEYS}, "meals": _MEALS},
         "required": ["day", "meals"]}},
    {"name": "save_week_plan", "description": "Сохранить новый план недели черновиком (активируется после «да» пользователя). Блюда — один раз в dishes, дни ссылаются на их id.",
     "input_schema": {"type": "object", "properties": {
         "week": _WEEK,
         "dishes": {"type": "object", "additionalProperties": _DISH},
         "days": {"type": "object", "properties": {k: _MEALS for k in services.DAY_KEYS},
                  "required": services.DAY_KEYS}},
         "required": ["dishes", "days"]}},
    {"name": "add_favorite", "description": "Сохранить блюдо в избранное: из записи дневника (по умолчанию последней) или из items.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "entry_id": {"type": "integer"}, "items": {"type": "array", "items": _ITEM}},
         "required": ["name"]}},
    {"name": "get_stats", "description": "Итоги по дням за последние N дней (КБЖУ, соблюдение плана, вес).",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 31}},
                      "required": ["days"]}},
]
# Формат Responses API: {"type": "function", "name", "description", "parameters"}
TOOLS = [{"type": "function", "name": t["name"], "description": t["description"],
          "parameters": t["input_schema"], "strict": False} for t in TOOLS]
TOOL_NAMES = {t["name"] for t in TOOLS}


class ToolError(Exception):
    pass


@dataclass
class Result:
    text: str
    entry_ids: list[int] = field(default_factory=list)
    draft_saved: bool = False


class Toolbox:
    """Исполнение tools. Суммы и проверки — в коде."""

    def __init__(self, today: date, profile):
        self.today = today
        self.profile = profile
        self.result = Result("")

    def _date(self, raw: str | None) -> date:
        if not raw:
            return self.today
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            raise ToolError("date: нужен формат YYYY-MM-DD")
        if not self.today - timedelta(days=14) <= d <= self.today:
            raise ToolError("date: можно только за последние 14 дней")
        return d

    def _ws(self, week: str | None) -> date:
        ws = services.week_start(self.today)
        return ws + timedelta(days=7) if week == "next" else ws

    async def _meals(self, d: date) -> list[dict]:
        p = await repo.get_plan_for(d)
        return p.data["days"].get(services.day_key(d), []) if p else []

    async def log_food(self, description, items, slot=None, status="eaten", date=None):
        d = self._date(date)
        try:
            items = services.validate_items(items)
        except ValueError as e:
            raise ToolError(str(e))
        if status not in ("eaten", "replaced"):
            raise ToolError("status: eaten или replaced")
        if slot and slot not in {m["slot"] for m in await self._meals(d)}:
            raise ToolError(f"slot {slot} нет в плане на {d}")
        e = await repo.add_food(d, description[:200], items, slot=slot, source="text", status=status)
        self.result.entry_ids.append(e.id)
        return f"Записано #{e.id}: {services.fmt_macros(e.__dict__)}"

    async def delete_entry(self, entry_id):
        if entry_id == "last":
            e = await repo.last_food()
            if e is None:
                raise ToolError("Дневник пуст")
            entry_id = e.id
        e = await repo.delete_food(int(entry_id))
        if e is None:
            raise ToolError(f"Записи #{entry_id} нет")
        return f"Удалено #{e.id}: {e.description}"

    async def mark_meal(self, slot, status, date=None):
        d = self._date(date)
        meal = next((m for m in await self._meals(d) if m["slot"] == slot), None)
        if meal is None:
            raise ToolError(f"slot {slot} нет в плане на {d}")
        if status not in ("eaten", "skipped"):
            raise ToolError("status: eaten или skipped")
        e = await repo.mark_meal(d, meal, status)
        self.result.entry_ids.append(e.id)
        return f"Отмечено #{e.id}: {meal['name']} — {status}"

    async def update_profile(self, text):
        try:
            await repo.update_profile_text(text)
        except ValueError as e:
            raise ToolError(f"{e}. Сократи и вызови снова.")
        return "Профиль обновлён"

    async def get_plan(self, week="current"):
        ws = self._ws(week)
        p = await repo.get_plan_for(ws)
        if not p:
            raise ToolError("Плана нет")
        return json.dumps(services.compact_plan(p.data), ensure_ascii=False, separators=(",", ":"))

    async def update_plan_day(self, day, meals, week="current"):
        ws = self._ws(week)
        d = ws + timedelta(days=services.DAY_KEYS.index(day))
        p = await repo.get_plan_for(d)
        dishes = services.compact_plan(p.data)["dishes"] if p else {}
        try:
            expanded = services.expand_meals(meals, dishes)
            if not expanded:
                raise ValueError("Пустой день")
            await repo.set_plan_day(d, expanded)
        except ValueError as e:
            raise ToolError(str(e))
        kcal = services.day_plan_totals(expanded)["kcal"]
        return f"День {day} {d:%d.%m} обновлён: {len(expanded)} приёмов, {round(kcal)} ккал"

    async def save_week_plan(self, dishes, days, week="next"):
        try:
            data = services.expand_plan({"dishes": dishes, "days": days})
        except (ValueError, TypeError) as e:
            raise ToolError(str(e))
        ws = self._ws(week)
        plan_id = await repo.save_plan(ws, data, active=False)
        await repo.update_settings(draft_plan_id=plan_id)
        self.result.draft_saved = True
        kcal = [round(services.day_plan_totals(data["days"][k])["kcal"]) for k in services.DAY_KEYS]
        return f"Черновик на неделю с {ws:%d.%m} готов, ккал по дням: {kcal}. Опиши кратко и спроси «Сохранить?»"

    async def add_favorite(self, name, entry_id=None, items=None):
        if items:
            try:
                items = services.validate_items(items)
            except ValueError as e:
                raise ToolError(str(e))
        else:
            e = await (repo.get_food(int(entry_id)) if entry_id else repo.last_food())
            if e is None or not e.items:
                raise ToolError("Нет записи с позициями для избранного")
            items = e.items
        f = await repo.add_favorite(name[:100], items)
        n = len(await repo.favorites())
        return f"В избранном под №{n}: {f.name}, {round(f.kcal)} ккал"

    async def get_stats(self, days):
        days = max(1, min(int(days), 31))
        d1 = self.today - timedelta(days=days - 1)
        return services.stats_text(d1, self.today, await repo.logs_between(d1, self.today),
                                   await repo.plans_by_date(d1, self.today), self.profile.targets,
                                   await repo.weights(d1 - timedelta(days=7)))

    async def run(self, name: str, args: dict) -> tuple[str, bool]:
        fn = getattr(self, name, None) if name in TOOL_NAMES else None
        if fn is None:
            return f"Нет инструмента {name}", True
        try:
            return await fn(**args), False
        except ToolError as e:
            return str(e), True
        except (TypeError, ValueError, KeyError) as e:
            return f"Неверные аргументы: {e}", True
        except Exception:
            log.exception("Ошибка инструмента %s", name)
            return "Внутренняя ошибка инструмента", True


_client: openai.AsyncOpenAI | None = None


def get_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        cfg = get_settings()
        http = openai.DefaultAsyncHttpxClient(proxy=cfg.proxy_url) if cfg.proxy_url else None
        _client = openai.AsyncOpenAI(api_key=cfg.openai_api_key, max_retries=3, timeout=90.0, http_client=http)
    return _client


def history_messages(rows) -> list[dict]:
    """Последние сообщения: начинаются с user, подряд идущие одинаковые роли склеиваются."""
    msgs: list[dict] = []
    for r in rows:
        if not msgs and r.role != "user":
            continue
        if msgs and msgs[-1]["role"] == r.role:
            msgs[-1]["content"] += "\n" + r.content
        else:
            msgs.append({"role": r.role, "content": r.content})
    return msgs


def error_text(e: Exception) -> str:
    if isinstance(e, openai.AuthenticationError):
        reason = "неверный OPENAI_API_KEY"
    elif isinstance(e, openai.RateLimitError):
        reason = "слишком много запросов или закончился баланс"
    elif isinstance(e, openai.APITimeoutError):
        reason = "не ответил вовремя"
    elif isinstance(e, openai.APIConnectionError):
        reason = "нет связи с сервером"
    elif isinstance(e, openai.APIStatusError) and e.status_code >= 500:
        reason = "сервис перегружен"
    else:
        reason = "ошибка запроса"
    return texts.ERR_AI.format(reason=reason)


def static_input() -> list[dict]:
    """Статичная часть: точка кэша сразу после неё — tools + промпт кэшируются, контекст дня нет."""
    return [{"role": "developer", "content": [{
        "type": "input_text", "text": SYSTEM, "prompt_cache_breakpoint": {"mode": "explicit"}}]}]


async def call_model(client, items: list, max_tokens: int, last: bool):
    return await client.responses.create(
        model=get_settings().openai_model,
        input=items,
        tools=TOOLS,
        tool_choice="none" if last else "auto",
        reasoning={"effort": "none"},  # без размышлений: весь лимит токенов — на ответ
        max_output_tokens=max_tokens,
        prompt_cache_options={"mode": "explicit"},
        store=False,
    )


def output_text(resp) -> str:
    parts = []
    for item in resp.output:
        if item.type == "message":
            parts += [c.text for c in item.content if c.type == "output_text"]
    return "\n".join(parts).strip()


def is_refusal(resp) -> bool:
    return any(item.type == "message" and any(c.type == "refusal" for c in item.content) for item in resp.output)


def usage_numbers(resp) -> tuple[int, int, int, int]:
    """(обычный вход, выход, чтение из кэша, запись в кэш). input_tokens у OpenAI включает кэш."""
    u = resp.usage
    if u is None:
        return 0, 0, 0, 0
    d = u.input_tokens_details
    cached = getattr(d, "cached_tokens", 0) or 0
    written = getattr(d, "cache_write_tokens", 0) or 0
    return max((u.input_tokens or 0) - cached - written, 0), u.output_tokens or 0, cached, written


async def run_agent(user_text: str, now_local: datetime, client=None) -> Result:
    client = client or get_client()
    today = now_local.date()
    prof = await repo.get_profile()
    plan = await repo.get_plan_for(today)
    meals = plan.data["days"].get(services.day_key(today), []) if plan else []
    context = services.ai_context(now_local, prof.profile_text, prof.targets, meals, await repo.day_logs(today))
    items = static_input() + [{"role": "developer", "content": "Контекст\n" + context}]
    items += history_messages(await repo.recent_messages(6)) + [{"role": "user", "content": user_text}]
    max_tokens = MAX_TOKENS_PLAN if PLAN_RE.search(user_text) else MAX_TOKENS
    tools = Toolbox(today, prof)
    text = ""

    for i in range(MAX_ITER):
        try:
            resp = await call_model(client, items, max_tokens, last=i == MAX_ITER - 1)
        except openai.APIError as e:
            log.warning("Ошибка OpenAI: %r", e)
            tools.result.text = error_text(e)
            return tools.result
        await repo.add_usage(today, *usage_numbers(resp))

        text = output_text(resp)
        calls = [o for o in resp.output if o.type == "function_call"]
        if is_refusal(resp):
            text = "С этим помочь не могу."
            break
        incomplete = resp.status == "incomplete" and getattr(resp.incomplete_details, "reason", "") == "max_output_tokens"
        if incomplete and max_tokens < MAX_TOKENS_PLAN:
            max_tokens = MAX_TOKENS_PLAN  # ответ не влез — повторяем с большим лимитом
            continue
        if not calls:
            break
        # возвращаем ответ модели в контекст (без id: с store=False они не хранятся на сервере)
        items += [{k: v for k, v in o.model_dump(exclude_none=True).items() if k != "id"}
                  for o in resp.output if o.type in ("message", "function_call")]
        for c in calls:
            try:
                args = json.loads(c.arguments or "{}")
                out, is_err = await tools.run(c.name, args if isinstance(args, dict) else {})
            except json.JSONDecodeError:
                out, is_err = "Аргументы — невалидный JSON", True
            log.info("tool %s → %s", c.name, out[:200])
            items.append({"type": "function_call_output", "call_id": c.call_id,
                          "output": f"ОШИБКА: {out}" if is_err else out})

    tools.result.text = text or ("Готово." if tools.result.entry_ids else "Не получилось ответить, попробуй переформулировать.")
    await repo.add_messages(("user", user_text), ("assistant", tools.result.text))
    return tools.result
