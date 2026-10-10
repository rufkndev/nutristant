"""Все запросы к БД."""
from datetime import date, timedelta

from sqlalchemy import delete, func, select, update

from app import db, services
from app.db import AiUsage, ChatMessage, Favorite, FoodLog, Plan, Profile, SentEvent, WeightLog


# ---------- профиль ----------

async def get_profile() -> Profile:
    async with db.Session() as s:
        return await s.get(Profile, 1)


async def load_seed(seed_dir: str, default_tz: str, today: date, keep_settings: bool = True) -> None:
    """Загрузить профиль, цели и план из seed/. Дневник не трогаем."""
    text, targets, plan = services.load_seed(seed_dir)
    async with db.Session() as s:
        prof = await s.get(Profile, 1)
        if prof is None:
            prof = Profile(id=1, settings={**services.DEFAULT_SETTINGS, "tz": default_tz})
            s.add(prof)
        elif not keep_settings:
            prof.settings = {**services.DEFAULT_SETTINGS, "tz": default_tz}
        prof.profile_text = text
        prof.targets = targets
        ws = services.week_start(today)
        await s.execute(update(Plan).where(Plan.week_start >= ws).values(active=False))
        s.add(Plan(week_start=ws, data=plan, active=True))
        await s.commit()


async def ensure_seed(seed_dir: str, default_tz: str, today: date) -> bool:
    if await get_profile() is None:
        await load_seed(seed_dir, default_tz, today)
        return True
    return False


async def update_profile_text(text: str) -> None:
    text = services.check_profile(text)
    async with db.Session() as s:
        prof = await s.get(Profile, 1)
        prof.profile_text = text
        await s.commit()


async def update_settings(**changes) -> dict:
    async with db.Session() as s:
        prof = await s.get(Profile, 1)
        prof.settings = {**services.DEFAULT_SETTINGS, **prof.settings, **changes}
        await s.commit()
        return prof.settings


# ---------- план ----------

async def get_plan_for(d: date) -> Plan | None:
    """Активный план недели, в которую попадает дата (или последний до неё)."""
    async with db.Session() as s:
        return await s.scalar(
            select(Plan).where(Plan.active, Plan.week_start <= services.week_start(d))
            .order_by(Plan.week_start.desc(), Plan.id.desc()).limit(1)
        )


async def ensure_week(d: date) -> bool:
    """Новая неделя: если плана на неё нет — копируем прошлый. True, если скопировали."""
    ws = services.week_start(d)
    async with db.Session() as s:
        cur = await s.scalar(select(Plan.id).where(Plan.active, Plan.week_start == ws).limit(1))
        if cur:
            return False
        prev = await s.scalar(
            select(Plan).where(Plan.active, Plan.week_start < ws)
            .order_by(Plan.week_start.desc(), Plan.id.desc()).limit(1)
        )
        if prev is None:
            return False
        s.add(Plan(week_start=ws, data=services.copy_plan(prev.data), active=True))
        await s.commit()
        return True


async def save_plan(ws: date, data: dict, active: bool = True) -> int:
    """Сохранить план недели. Активный заменяет прежние планы этой и следующих недель."""
    services.validate_plan(data)
    async with db.Session() as s:
        if active:
            await s.execute(update(Plan).where(Plan.week_start >= ws).values(active=False))
        p = Plan(week_start=ws, data=data, active=active)
        s.add(p)
        await s.commit()
        return p.id


async def activate_plan(plan_id: int) -> Plan | None:
    async with db.Session() as s:
        p = await s.get(Plan, plan_id)
        if p is None:
            return None
        await s.execute(update(Plan).where(Plan.week_start >= p.week_start).values(active=False))
        p.active = True
        await s.commit()
        return p


async def set_plan_day(d: date, day_meals: list) -> None:
    """Заменить один день в плане недели (создаёт план недели, если его ещё нет)."""
    await ensure_week(d)
    ws = services.week_start(d)
    async with db.Session() as s:
        p = await s.scalar(select(Plan).where(Plan.active, Plan.week_start == ws)
                           .order_by(Plan.id.desc()).limit(1))
        data = services.copy_plan(p.data)
        data["days"][services.day_key(d)] = day_meals
        services.validate_plan(data)
        p.data = data
        await s.commit()


# ---------- дневник ----------

async def add_food(d: date, description: str, items: list, *, slot: str | None = None,
                   source: str = "text", status: str = "eaten") -> FoodLog:
    totals = services.sum_items(items)
    async with db.Session() as s:
        e = FoodLog(date=d, slot=slot, description=description, items=items,
                    source=source, status=status, **totals)
        s.add(e)
        await s.commit()
        return e


async def mark_meal(d: date, meal: dict, status: str) -> FoodLog:
    """Отметить приём по плану: прежние отметки этого слота за день заменяются."""
    async with db.Session() as s:
        await s.execute(delete(FoodLog).where(FoodLog.date == d, FoodLog.slot == meal["slot"],
                                              FoodLog.source == "plan"))
        await s.commit()
    items = services.meal_items(meal) if status == "eaten" else []
    desc = f"{meal['name']}: " + " + ".join(x["name"] for x in meal["dishes"])
    return await add_food(d, desc, items, slot=meal["slot"], source="plan", status=status)


async def day_logs(d: date) -> list[FoodLog]:
    async with db.Session() as s:
        return list(await s.scalars(select(FoodLog).where(FoodLog.date == d)
                                    .order_by(FoodLog.eaten_at, FoodLog.id)))


async def logs_between(d1: date, d2: date) -> list[FoodLog]:
    async with db.Session() as s:
        return list(await s.scalars(select(FoodLog).where(FoodLog.date >= d1, FoodLog.date <= d2)
                                    .order_by(FoodLog.date, FoodLog.eaten_at, FoodLog.id)))


async def all_logs() -> list[FoodLog]:
    async with db.Session() as s:
        return list(await s.scalars(select(FoodLog).order_by(FoodLog.eaten_at, FoodLog.id)))


async def get_food(entry_id: int) -> FoodLog | None:
    async with db.Session() as s:
        return await s.get(FoodLog, entry_id)


async def last_food() -> FoodLog | None:
    async with db.Session() as s:
        return await s.scalar(select(FoodLog).order_by(FoodLog.eaten_at.desc(), FoodLog.id.desc()).limit(1))


async def delete_food(entry_id: int) -> FoodLog | None:
    async with db.Session() as s:
        e = await s.get(FoodLog, entry_id)
        if e:
            await s.delete(e)
            await s.commit()
        return e


# ---------- избранное ----------

async def favorites() -> list[Favorite]:
    async with db.Session() as s:
        return list(await s.scalars(select(Favorite).order_by(Favorite.id)))


async def add_favorite(name: str, items: list) -> Favorite:
    async with db.Session() as s:
        f = Favorite(name=name, items=items, uses=0, **services.sum_items(items))
        s.add(f)
        await s.commit()
        return f


async def use_favorite(fav_id: int) -> None:
    async with db.Session() as s:
        await s.execute(update(Favorite).where(Favorite.id == fav_id).values(uses=Favorite.uses + 1))
        await s.commit()


# ---------- вес ----------

async def set_weight(d: date, w: float) -> None:
    async with db.Session() as s:
        row = await s.scalar(select(WeightLog).where(WeightLog.date == d))
        if row:
            row.weight = w
        else:
            s.add(WeightLog(date=d, weight=w))
        await s.commit()


async def weights(since: date) -> list[tuple[date, float]]:
    async with db.Session() as s:
        rows = await s.execute(select(WeightLog.date, WeightLog.weight)
                               .where(WeightLog.date >= since).order_by(WeightLog.date))
        return [(r[0], r[1]) for r in rows]


# ---------- отправленные события ----------

async def sent_keys(days: int = 3) -> set[str]:
    async with db.Session() as s:
        since = db.utcnow() - timedelta(days=days)
        return set(await s.scalars(select(SentEvent.key).where(SentEvent.sent_at >= since)))


async def mark_sent(key: str) -> None:
    async with db.Session() as s:
        await s.merge(SentEvent(key=key, sent_at=db.utcnow()))
        await s.execute(delete(SentEvent).where(SentEvent.sent_at < db.utcnow() - timedelta(days=30)))
        await s.commit()


# ---------- чат и расход ИИ ----------

async def recent_messages(limit: int = 6) -> list[ChatMessage]:
    async with db.Session() as s:
        rows = list(await s.scalars(select(ChatMessage).order_by(ChatMessage.id.desc()).limit(limit)))
        return rows[::-1]


async def add_messages(*pairs: tuple[str, str], keep: int = 50) -> None:
    async with db.Session() as s:
        for role, content in pairs:
            s.add(ChatMessage(role=role, content=content[:4000]))
        await s.flush()
        border = await s.scalar(select(ChatMessage.id).order_by(ChatMessage.id.desc()).offset(keep - 1).limit(1))
        if border:
            await s.execute(delete(ChatMessage).where(ChatMessage.id < border))
        await s.commit()


async def add_usage(d: date, inp: int, out: int, cache_read: int, cache_write: int) -> None:
    async with db.Session() as s:
        s.add(AiUsage(date=d, input_tokens=inp, output_tokens=out,
                      cache_read_tokens=cache_read, cache_write_tokens=cache_write))
        await s.commit()


async def usage_sum(d1: date, d2: date) -> dict:
    async with db.Session() as s:
        row = (await s.execute(select(
            func.count(AiUsage.id), func.coalesce(func.sum(AiUsage.input_tokens), 0),
            func.coalesce(func.sum(AiUsage.output_tokens), 0), func.coalesce(func.sum(AiUsage.cache_read_tokens), 0),
            func.coalesce(func.sum(AiUsage.cache_write_tokens), 0),
        ).where(AiUsage.date >= d1, AiUsage.date <= d2))).one()
        return dict(zip(("calls", "input", "output", "cache_read", "cache_write"), row))


async def plans_by_date(d1: date, d2: date) -> dict[date, list]:
    out, d = {}, d1
    while d <= d2:
        p = await get_plan_for(d)
        out[d] = p.data["days"].get(services.day_key(d), []) if p else []
        d += timedelta(days=1)
    return out
