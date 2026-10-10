"""Команды, кнопки, свободный текст."""
import csv
import io
import re
from datetime import date, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
                           TelegramObject)

from app import ai, repo, services, texts
from app.config import get_settings

router = Router()

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=texts.BTN_TODAY), KeyboardButton(text=texts.BTN_STATS)],
        [KeyboardButton(text=texts.BTN_WEEK), KeyboardButton(text=texts.BTN_SHOP)],
    ],
    resize_keyboard=True,
)


class OnlyOwner(BaseMiddleware):
    """Сообщения и нажатия от всех, кроме владельца, молча игнорируются."""

    def __init__(self, user_id: int):
        self.user_id = user_id

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.user_id:
            return None
        return await handler(event, data)


def undo_kb(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Отменить", callback_data=f"u:{entry_id}")]])


def meal_kb(d: date, slot: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Съел", callback_data=f"m:e:{d.isoformat()}:{slot}"),
        InlineKeyboardButton(text="⏭ Пропустил", callback_data=f"m:s:{d.isoformat()}:{slot}"),
    ]])


def split_text(text: str, limit: int = 4000) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


async def answer_long(m: Message, text: str, **kw) -> Message:
    msg = None
    for part in split_text(text):
        msg = await m.answer(part, **kw)
    return msg


async def ctx() -> tuple[Any, str, date]:
    """Профиль, часовой пояс и «сегодня» по часовому поясу из настроек."""
    prof = await repo.get_profile()
    tz = prof.settings.get("tz", get_settings().tz)
    return prof, tz, services.local_now(tz).date()


async def day_meals(d: date) -> list[dict]:
    await repo.ensure_week(d)
    plan = await repo.get_plan_for(d)
    return plan.data["days"].get(services.day_key(d), []) if plan else []


async def remaining_text(d: date) -> str:
    prof = await repo.get_profile()
    logs = await repo.day_logs(d)
    totals = services.sum_items([{k: getattr(e, k) for k in services.MACROS} for e in logs])
    return services.remaining_line(totals, services.targets_for(prof.targets, d)[1])


# ---------- служебное ----------

@router.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(texts.START, reply_markup=KEYBOARD)


@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(texts.HELP, reply_markup=KEYBOARD)


@router.message(Command("profile"))
async def cmd_profile(m: Message):
    prof, tz, today = await ctx()
    variant, t = services.targets_for(prof.targets, today)
    s = prof.settings
    await answer_long(m, texts.PROFILE.format(
        text=prof.profile_text, kcal=t["kcal"], p=t["protein"], f=t["fat"], c=t["carbs"],
        fiber=t.get("fiber", 0), water=t.get("water_ml", 0) / 1000,
        variant=services.VARIANT_LABEL.get(variant, variant),
        remind="вкл" if s.get("remind") else "выкл", before=s.get("remind_before"),
        evening=s.get("evening"), tz=tz))


@router.message(Command("reseed"))
async def cmd_reseed(m: Message):
    _, tz, today = await ctx()
    try:
        await repo.load_seed(get_settings().seed_dir, tz, today)
    except (OSError, ValueError) as e:
        await m.answer(f"Не получилось загрузить seed/: {e}")
        return
    await repo.update_settings(draft_plan_id=None)
    await m.answer(texts.RESEED_OK)


@router.message(Command("export"))
async def cmd_export(m: Message):
    _, tz, _ = await ctx()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "дата", "время", "приём", "статус", "источник", "описание",
                "ккал", "белки", "жиры", "углеводы", "клетчатка"])
    for e in await repo.all_logs():
        w.writerow([e.id, e.date.isoformat(), e.eaten_at.astimezone(ZoneInfo(tz)).strftime("%H:%M"),
                    e.slot or "", e.status, e.source, e.description,
                    *[round(getattr(e, k), 1) for k in services.MACROS]])
    data = buf.getvalue().encode("utf-8-sig")
    await m.answer_document(BufferedInputFile(data, filename="food_log.csv"), caption="Дневник питания")


# ---------- план ----------

@router.message(F.text == texts.BTN_TODAY)
async def btn_today(m: Message):
    _, _, today = await ctx()
    await show_day(m, today)


@router.message(Command("day"))
async def cmd_day(m: Message, command: CommandObject):
    _, _, today = await ctx()
    d = services.parse_day(command.args or "сегодня", today)
    if d is None:
        await m.answer("Не понял день. Пример: /day пт")
        return
    await show_day(m, d)


async def show_day(m: Message, d: date):
    prof = await repo.get_profile()
    meals = await day_meals(d)
    if not meals:
        await m.answer("На этот день плана нет.")
        return
    await answer_long(m, services.day_view(d, meals, await repo.day_logs(d), prof.targets))


@router.message(F.text == texts.BTN_WEEK)
async def btn_week(m: Message):
    _, _, today = await ctx()
    await repo.ensure_week(today)
    plan = await repo.get_plan_for(today)
    if not plan:
        await m.answer("Плана на неделю нет.")
        return
    ws = services.week_start(today)
    by_date: dict[date, list] = {}
    for e in await repo.logs_between(ws, ws + timedelta(days=6)):
        by_date.setdefault(e.date, []).append(e)
    await answer_long(m, "🗓 Неделя\n\n" + services.week_view(ws, plan.data["days"], by_date))


# ---------- итоги и отметки ----------

@router.message(F.text == texts.BTN_STATS)
async def btn_stats(m: Message):
    prof, tz, today = await ctx()
    await answer_long(m, services.stats_view(today, await repo.day_logs(today), prof.targets, tz))


@router.message(Command("del"))
async def cmd_del(m: Message, command: CommandObject):
    _, _, today = await ctx()
    logs = await repo.day_logs(today)
    try:
        n = int((command.args or "").strip())
        if n < 1:
            raise IndexError
        entry = logs[n - 1]
    except (ValueError, IndexError):
        await m.answer("Укажи номер записи из «📊 Итоги». Пример: /del 3")
        return
    await repo.delete_food(entry.id)
    await m.answer(f"Удалено: {entry.description}\n{await remaining_text(today)}")


@router.message(Command("ate", "skip"))
async def cmd_mark(m: Message, command: CommandObject):
    _, _, today = await ctx()
    meals = await day_meals(today)
    slot = services.find_slot(command.args or "", meals)
    if slot is None:
        names = ", ".join(x["name"].lower() for x in meals)
        await m.answer(f"Не понял приём. Есть: {names}. Пример: /{command.command} обед")
        return
    await mark_and_reply(m, today, meals, slot, "eaten" if command.command == "ate" else "skipped")


async def mark_and_reply(m: Message, d: date, meals: list[dict], slot: str, status: str):
    meal = next(x for x in meals if x["slot"] == slot)
    e = await repo.mark_meal(d, meal, status)
    head = f"✅ {meal['name']}: {round(e.kcal)} ккал" if status == "eaten" else f"⏭ {meal['name']} пропущен"
    await m.answer(f"{head}\n{await remaining_text(d)}", reply_markup=undo_kb(e.id))


@router.callback_query(F.data.startswith("m:"))
async def cb_meal(c: CallbackQuery):
    _, st, iso, slot = c.data.split(":", 3)
    d = date.fromisoformat(iso)
    meals = await day_meals(d)
    meal = next((x for x in meals if x["slot"] == slot), None)
    if meal is None:
        await c.answer("Этого приёма уже нет в плане")
        return
    status = "eaten" if st == "e" else "skipped"
    e = await repo.mark_meal(d, meal, status)
    mark = "✅ Съел" if status == "eaten" else "⏭ Пропустил"
    await c.message.edit_text(f"{c.message.text}\n\n{mark}\n{await remaining_text(d)}",
                              reply_markup=undo_kb(e.id))
    await c.answer()


@router.callback_query(F.data.startswith("u:"))
async def cb_undo(c: CallbackQuery):
    e = await repo.delete_food(int(c.data[2:]))
    if e is None:
        await c.answer("Запись уже удалена")
        return
    await c.message.edit_text(f"{c.message.text}\n\n↩️ Отменено\n{await remaining_text(e.date)}")
    await c.answer()


# ---------- покупки ----------

@router.message(F.text == texts.BTN_SHOP)
@router.message(Command("shop"))
async def cmd_shop(m: Message, command: CommandObject | None = None):
    _, _, today = await ctx()
    arg = (command.args or "").strip() if command else ""
    n = int(arg) if arg.isdigit() and 1 <= int(arg) <= 14 else 7
    days = [await day_meals(today + timedelta(days=i)) for i in range(n)]
    title = f"🛒 Покупки на {n} дн. ({today:%d.%m}–{today + timedelta(days=n - 1):%d.%m})"
    await answer_long(m, services.shopping_list(days, title))


# ---------- избранное ----------

@router.message(Command("fav"))
async def cmd_fav(m: Message, command: CommandObject):
    favs = await repo.favorites()
    arg = (command.args or "").strip()
    if not arg:
        if not favs:
            await m.answer("Избранного пока нет. Напиши, например: «сохрани это в избранное».")
            return
        lines = [f"{i}. {f.name} — {services.fmt_macros(f.__dict__)}" for i, f in enumerate(favs, 1)]
        await answer_long(m, "⭐ Избранное\n" + "\n".join(lines) + "\n\nЗаписать: /fav номер")
        return
    if not arg.isdigit() or not 1 <= int(arg) <= len(favs):
        await m.answer("Нет такого номера. Список: /fav")
        return
    f = favs[int(arg) - 1]
    _, _, today = await ctx()
    e = await repo.add_food(today, f.name, f.items, source="favorite")
    await repo.use_favorite(f.id)
    await m.answer(f"⭐ {f.name}: {round(e.kcal)} ккал\n{await remaining_text(today)}",
                   reply_markup=undo_kb(e.id))


# ---------- вес ----------

@router.message(Command("w"))
async def cmd_w(m: Message, command: CommandObject):
    try:
        w = float((command.args or "").replace(",", ".").strip())
        assert 30 <= w <= 250
    except (ValueError, AssertionError):
        await m.answer("Пример: /w 78.4")
        return
    _, _, today = await ctx()
    await repo.set_weight(today, w)
    await m.answer(f"⚖️ Записал: {w:.1f} кг")


@router.message(Command("weight"))
async def cmd_weight(m: Message):
    _, _, today = await ctx()
    rows = await repo.weights(today - timedelta(days=60))
    await m.answer(services.weight_view(rows, today))


# ---------- настройки ----------

@router.message(Command("remind"))
async def cmd_remind(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg in ("on", "вкл"):
        await repo.update_settings(remind=True)
        await m.answer("🔔 Напоминания включены")
    elif arg in ("off", "выкл"):
        await repo.update_settings(remind=False)
        await m.answer("🔕 Напоминания выключены")
    elif arg.isdigit() and 1 <= int(arg) <= 120:
        await repo.update_settings(remind_before=int(arg), remind=True)
        await m.answer(f"🔔 Буду напоминать за {arg} мин")
    else:
        await m.answer("Пример: /remind on, /remind off, /remind 15")


@router.message(Command("evening"))
async def cmd_evening(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", arg):
        await m.answer("Пример: /evening 22:00")
        return
    h, mi = services.hm(arg)
    await repo.update_settings(evening=f"{h:02d}:{mi:02d}")
    await m.answer(f"🌙 Вечерний итог в {h:02d}:{mi:02d}")


@router.message(Command("tz"))
async def cmd_tz(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    try:
        ZoneInfo(arg)
        assert arg
    except Exception:
        await m.answer("Пример: /tz Europe/Moscow")
        return
    await repo.update_settings(tz=arg)
    await m.answer(f"🕒 Часовой пояс: {arg}, сейчас там {services.local_now(arg):%H:%M}")


# ---------- расход ИИ ----------

@router.message(Command("usage"))
async def cmd_usage(m: Message):
    _, _, today = await ctx()
    cfg = get_settings()

    def block(title: str, u: dict) -> str:
        cost = (u["input"] * cfg.price_input + u["output"] * cfg.price_output
                + u["cache_read"] * cfg.price_cache_read + u["cache_write"] * cfg.price_cache_write) / 1e6
        n = {k: f"{v:,}".replace(",", " ") for k, v in u.items()}
        return (f"{title}: {n['calls']} запросов, ~${cost:.3f}\n"
                f"  вход {n['input']} · выход {n['output']} · из кэша {n['cache_read']} · в кэш {n['cache_write']}")

    day = await repo.usage_sum(today, today)
    month = await repo.usage_sum(today.replace(day=1), today)
    await m.answer(f"🤖 Расход ИИ ({cfg.anthropic_model})\n{block('Сегодня', day)}\n{block('Месяц', month)}")


# ---------- свободный текст → ИИ ----------

YES_RE = re.compile(r"^(да|ага|угу|ок|окей|сохрани|сохраняй|давай|yes|ok)\b", re.I)
NO_RE = re.compile(r"^(нет|не надо|не сохраняй|отмена|отмени)\b", re.I)


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(m: Message):
    prof, tz, today = await ctx()
    text = m.text.strip()
    draft_id = prof.settings.get("draft_plan_id")
    if draft_id and YES_RE.match(text):
        p = await repo.activate_plan(draft_id)
        await repo.update_settings(draft_plan_id=None)
        await m.answer(f"✅ План сохранён на неделю с {p.week_start:%d.%m}." if p else "Черновик не найден.")
        return
    if draft_id and NO_RE.match(text):
        await repo.update_settings(draft_plan_id=None)
        await m.answer("Ок, план не сохраняю.")
        return

    await m.bot.send_chat_action(m.chat.id, "typing")
    res = await ai.run_agent(text, services.local_now(tz))
    reply, kb = res.text, None
    if res.entry_ids:
        reply += "\n" + await remaining_text(today)
        kb = undo_kb(res.entry_ids[-1])
    await answer_long(m, reply, reply_markup=kb)


@router.message()
async def other(m: Message):
    await m.answer("Не знаю такую команду, а голос и картинки не понимаю. /help — список команд.")
