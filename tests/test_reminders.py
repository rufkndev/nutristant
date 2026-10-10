from datetime import datetime, timezone

from app import services as s

MEALS = [{"slot": "lunch", "name": "Обед", "time": "13:30", "dishes": []},
         {"slot": "late", "name": "Поздний", "time": "23:50", "dishes": []}]
ST = {**s.DEFAULT_SETTINGS, "tz": "Europe/Moscow"}  # UTC+3


def due(h, m, settings=ST, statuses=None, sent=None, day=12, meals=MEALS):
    now = datetime(2026, 10, day, h, m, tzinfo=timezone.utc)
    return [e["key"] for e in s.due_events(now, settings, meals, statuses or {}, sent or set())]


def test_reminder_window():
    assert due(10, 19) == []                               # 13:19 МСК — рано
    assert due(10, 20) == ["r:2026-10-12:lunch"]           # 13:20 — за 10 мин
    assert due(10, 29) == ["r:2026-10-12:lunch"]           # догонка после рестарта
    assert due(10, 30) == []                               # приём начался — поздно


def test_reminder_not_sent_when_marked_or_sent():
    assert due(10, 20, statuses={"lunch": "eaten"}) == []
    assert due(10, 20, statuses={"lunch": "skipped"}) == []
    assert due(10, 20, sent={"r:2026-10-12:lunch"}) == []


def test_reminder_off_and_custom_before():
    assert due(10, 20, settings={**ST, "remind": False}) == []
    assert due(10, 15, settings={**ST, "remind_before": 15}) == ["r:2026-10-12:lunch"]


def test_no_reminders_at_night():
    keys = due(20, 40)                                     # 23:40 МСК — тихие часы
    assert not [k for k in keys if k.startswith("r:")]     # «Поздний» (23:50) не напоминаем


def test_timezone():
    warsaw = {**ST, "tz": "Europe/Warsaw"}                 # UTC+2 в октябре
    assert due(11, 20, settings=warsaw) == ["r:2026-10-12:lunch"]
    assert due(10, 20, settings=warsaw) == []


def test_evening_and_weekly():
    assert due(20, 0) == ["e:2026-10-12"]                  # 23:00 МСК
    assert due(20, 59) == ["e:2026-10-12"]                 # в течение часа
    assert due(21, 0) == []
    assert due(20, 0, sent={"e:2026-10-12"}) == []
    sunday = due(17, 0, day=18, meals=[])                  # вс 20:00 МСК
    assert sunday == ["w:2026-10-18"]
    assert due(17, 0, day=17, meals=[]) == []              # в сб отчёта нет
