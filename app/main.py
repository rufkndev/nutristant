"""Точка входа: миграции, первичная загрузка seed, бот, планировщик."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from alembic import command
from alembic.config import Config
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import db, handlers, repo, services
from app.config import get_settings

log = logging.getLogger("bot")
ROOT = Path(__file__).resolve().parent.parent


def run_migrations() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")


async def tick(bot: Bot) -> None:
    """Раз в минуту: автокопирование плана, напоминания, вечерний итог, недельный отчёт."""
    cfg = get_settings()
    Path(cfg.heartbeat_file).touch()
    try:
        prof = await repo.get_profile()
        st = {**services.DEFAULT_SETTINGS, **prof.settings}
        now = datetime.now(timezone.utc)
        local = services.local_now(st["tz"], now)
        today = local.date()
        if await repo.ensure_week(today):
            log.info("План скопирован на новую неделю")
        plan = await repo.get_plan_for(today)
        meals = plan.data["days"].get(services.day_key(today), []) if plan else []
        logs = await repo.day_logs(today)
        events = services.due_events(now, st, meals, services.slot_statuses(logs), await repo.sent_keys())
        for ev in events:
            await send_event(bot, cfg.allowed_user_id, ev, prof, meals, logs, local)
            await repo.mark_sent(ev["key"])
    except Exception:
        log.exception("Ошибка в планировщике")


async def send_event(bot: Bot, chat_id: int, ev: dict, prof, meals, logs, local) -> None:
    d = ev["date"]
    if ev["type"] == "remind":
        meal = next(m for m in meals if m["slot"] == ev["slot"])
        h, mi = services.hm(meal["time"])
        start = local.replace(hour=h, minute=mi, second=0, microsecond=0)
        minutes = max(1, round((start - local).total_seconds() / 60))
        await bot.send_message(chat_id, services.remind_text(meal, minutes),
                               reply_markup=handlers.meal_kb(d, meal["slot"]))
    elif ev["type"] == "evening":
        await bot.send_message(chat_id, services.evening_report(d, logs, meals, prof.targets, local))
    elif ev["type"] == "weekly":
        ws = services.week_start(d)
        plan = await repo.get_plan_for(d)
        week_logs = await repo.logs_between(ws, ws + timedelta(days=6))
        weights = await repo.weights(ws - timedelta(days=7))
        await bot.send_message(chat_id, services.weekly_report(
            ws, plan.data["days"] if plan else {}, week_logs, prof.targets, weights))


async def main() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    await asyncio.to_thread(run_migrations)
    log.info("Миграции применены")
    db.init_db(cfg.database_url)
    today = services.local_now(cfg.tz).date()
    if await repo.ensure_seed(cfg.seed_dir, cfg.tz, today):
        log.info("Начальные данные загружены из %s", cfg.seed_dir)

    bot = Bot(cfg.telegram_bot_token)
    dp = Dispatcher()
    owner = handlers.OnlyOwner(cfg.allowed_user_id)
    dp.message.outer_middleware(owner)
    dp.callback_query.outer_middleware(owner)
    dp.include_router(handlers.router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(tick, "interval", seconds=60, args=[bot], max_instances=1, coalesce=True)
    scheduler.start()
    await tick(bot)

    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
