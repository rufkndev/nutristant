"""Вся логика без ИИ и без БД: план, дневник, итоги, покупки, напоминания, отчёты."""
import copy
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

PROFILE_LIMIT = 1500
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_SETTINGS = {
    "remind": True,
    "remind_before": 10,
    "evening": "23:00",
    "tz": "Europe/Moscow",
    "quiet_start": "23:30",
    "quiet_end": "07:00",
}


def local_now(tz: str, now_utc: datetime | None = None) -> datetime:
    return (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def day_key(d: date) -> str:
    return DAY_KEYS[d.weekday()]


def check_profile(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("Профиль пустой")
    if len(text) > PROFILE_LIMIT:
        raise ValueError(f"Профиль {len(text)} символов, лимит {PROFILE_LIMIT}")
    return text


def load_seed(seed_dir: str) -> tuple[str, dict, dict]:
    base = Path(seed_dir)
    profile = check_profile((base / "profile.md").read_text(encoding="utf-8"))
    targets = json.loads((base / "targets.json").read_text(encoding="utf-8"))
    plan = json.loads((base / "plan.json").read_text(encoding="utf-8"))
    validate_plan(plan)
    return profile, targets, plan


def validate_plan(plan: dict) -> None:
    days = plan.get("days", {})
    missing = [k for k in DAY_KEYS if k not in days]
    if missing:
        raise ValueError(f"В плане нет дней: {', '.join(missing)}")
    for k in DAY_KEYS:
        for meal in days[k]:
            for key in ("slot", "name", "time", "dishes"):
                if key not in meal:
                    raise ValueError(f"{k}: у приёма нет поля {key}")
            for dish in meal["dishes"]:
                if not dish.get("ingredients"):
                    raise ValueError(f"{k} {meal['slot']}: у блюда «{dish.get('name')}» нет ингредиентов")


def copy_plan(data: dict) -> dict:
    return copy.deepcopy(data)


# ---------- КБЖУ ----------

MACROS = ("kcal", "protein", "fat", "carbs", "fiber")


class FoodItem(BaseModel):
    """Одна позиция еды. Абсурдные значения отклоняются."""
    name: str = Field(min_length=1, max_length=200)
    grams: float | None = Field(None, gt=0, le=5000)
    kcal: float = Field(ge=0, le=3000)
    protein: float = Field(0, ge=0, le=300)
    fat: float = Field(0, ge=0, le=300)
    carbs: float = Field(0, ge=0, le=600)
    fiber: float = Field(0, ge=0, le=100)

    @model_validator(mode="after")
    def _sane(self):
        if self.grams:
            if self.kcal / self.grams * 100 > 950:
                raise ValueError(f"{self.name}: больше 950 ккал на 100 г")
            if self.protein + self.fat + self.carbs + self.fiber > self.grams * 1.05:
                raise ValueError(f"{self.name}: БЖУ больше веса порции")
        if self.protein * 4 + self.fat * 9 + self.carbs * 4 > self.kcal * 1.5 + 50:
            raise ValueError(f"{self.name}: БЖУ не сходятся с калориями")
        return self


def validate_items(raw: list[dict]) -> list[dict]:
    if not raw:
        raise ValueError("Пустой список позиций")
    return [FoodItem(**r).model_dump(exclude_none=True) for r in raw]


def sum_items(items: list[dict]) -> dict:
    return {m: round(sum(float(i.get(m) or 0) for i in items), 1) for m in MACROS}


def remaining(totals: dict, target: dict) -> dict:
    return {m: round(target.get(m, 0) - totals.get(m, 0)) for m in MACROS}


# Клетчатка на 100 г — в плане её нет, считаем по ингредиентам
FIBER_100G = {
    "овсян": 10, "гречк": 10, "хлеб": 3, "рис": 2, "макарон": 3, "мука": 3.5, "картоф": 2,
    "огур": 1, "яблок": 2.4, "мандарин": 1.8, "банан": 2.6, "ягод": 3, "арахис": 6,
    "томатн": 4, "нутелл": 3.4, "джем": 1,
}


def dish_fiber(dish: dict) -> float:
    if "fiber" in dish:
        return float(dish["fiber"])
    total = 0.0
    for ing in dish.get("ingredients", []):
        name = ing["name"].lower()
        for key, per100 in FIBER_100G.items():
            if key in name:
                total += ing["grams"] * per100 / 100
                break
    return round(total, 1)


def dish_item(dish: dict) -> dict:
    return {"name": dish["name"], "kcal": dish["kcal"], "protein": dish["protein"],
            "fat": dish["fat"], "carbs": dish["carbs"], "fiber": dish_fiber(dish)}


def meal_items(meal: dict) -> list[dict]:
    return [dish_item(d) for d in meal["dishes"]]


def targets_for(targets: dict, d: date) -> tuple[str, dict]:
    key = day_key(d)
    variants = {k: v for k, v in targets.items() if isinstance(v, dict) and "kcal" in v}
    for name, v in variants.items():
        if key in v.get("days", []):
            return name, v
    name = next(iter(variants))
    return name, variants[name]


# ---------- план и статусы ----------

DAY_SHORT = {"mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт", "fri": "Пт", "sat": "Сб", "sun": "Вс"}
DAY_ALIASES = {
    "mon": ["пн", "пон", "mon"], "tue": ["вт", "вто", "tue"], "wed": ["ср", "сре", "wed"],
    "thu": ["чт", "чет", "thu"], "fri": ["пт", "пят", "fri"], "sat": ["сб", "суб", "sat"],
    "sun": ["вс", "вос", "sun"],
}
SLOT_ALIASES = {
    "breakfast": ["завтрак"], "shake": ["коктейль", "лёгкий коктейль", "легкий коктейль"],
    "lunch": ["обед"], "snack": ["перекус", "полдник"], "dinner": ["ужин"],
    "late": ["перед сном", "сон", "на ночь"],
}
STATUS_ICON = {"eaten": "✅", "skipped": "⏭", "replaced": "🔄"}
VARIANT_LABEL = {"training": "тренировка", "rest": "отдых"}


def parse_day(arg: str, today: date) -> date | None:
    """«пт» → дата пятницы текущей недели; «сегодня», «завтра», «вчера» тоже понимаем."""
    a = arg.strip().lower()
    rel = {"сегодня": 0, "завтра": 1, "вчера": -1}
    if a in rel:
        return today + timedelta(days=rel[a])
    for key, aliases in DAY_ALIASES.items():
        if any(a.startswith(x) for x in aliases):
            return week_start(today) + timedelta(days=DAY_KEYS.index(key))
    return None


def find_slot(arg: str, day_meals: list[dict]) -> str | None:
    a = arg.strip().lower()
    if not a:
        return None
    for meal in day_meals:
        if meal["slot"] == a or meal["name"].lower() == a:
            return meal["slot"]
    for slot, aliases in SLOT_ALIASES.items():
        if any(x.startswith(a) or a.startswith(x) for x in aliases):
            if any(m["slot"] == slot for m in day_meals):
                return slot
    return None


def slot_statuses(logs: list) -> dict[str, str]:
    """Последний статус по каждому слоту (logs отсортированы по времени)."""
    st = {}
    for e in logs:
        if e.slot:
            st[e.slot] = e.status
    return st


def hm(t: str) -> tuple[int, int]:
    h, m = t.split(":")
    return int(h), int(m)


def fmt_macros(x: dict) -> str:
    return f"{round(x['kcal'])} ккал · Б{round(x['protein'])} Ж{round(x['fat'])} У{round(x['carbs'])}"


def day_plan_totals(day_meals: list[dict]) -> dict:
    return sum_items([i for m in day_meals for i in meal_items(m)])


def day_view(d: date, day_meals: list[dict], logs: list, targets: dict) -> str:
    variant, target = targets_for(targets, d)
    st = slot_statuses(logs)
    lines = [f"📅 {DAY_SHORT[day_key(d)]} {d:%d.%m} · {VARIANT_LABEL.get(variant, variant)} · цель {target['kcal']} ккал", ""]
    for meal in sorted(day_meals, key=lambda m: m["time"]):
        icon = STATUS_ICON.get(st.get(meal["slot"]), "—")
        lines.append(f"{icon} {meal['time']} {meal['name']}")
        for dish in meal["dishes"]:
            lines.append(f"   {dish['name']} — {fmt_macros(dish)}")
            if dish.get("note"):
                lines.append(f"   ↳ {dish['note']}")
    lines.append("")
    lines.append(f"По плану: {fmt_macros(day_plan_totals(day_meals))}")
    return "\n".join(lines)


def week_view(ws: date, plan_days: dict, logs_by_date: dict[date, list]) -> str:
    out = []
    for i, key in enumerate(DAY_KEYS):
        d = ws + timedelta(days=i)
        meals = sorted(plan_days.get(key, []), key=lambda m: m["time"])
        st = slot_statuses(logs_by_date.get(d, []))
        out.append(f"{DAY_SHORT[key]} {d:%d.%m} · {round(day_plan_totals(meals)['kcal'])} ккал")
        for m in meals:
            names = " + ".join(x["name"] for x in m["dishes"])
            if len(names) > 45:
                names = names[:44] + "…"
            out.append(f"{STATUS_ICON.get(st.get(m['slot']), '—')} {m['time']} {names}")
        out.append("")
    return "\n".join(out).strip()


def entry_line(n: int, e, tz: str) -> str:
    t = e.eaten_at.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    icon = STATUS_ICON.get(e.status, "•")
    desc = e.description if len(e.description) <= 50 else e.description[:49] + "…"
    tail = "пропуск" if e.status == "skipped" else f"{round(e.kcal)} ккал"
    return f"{n}. {t} {icon} {desc} — {tail}"


def totals_block(totals: dict, target: dict) -> str:
    names = [("kcal", "Ккал"), ("protein", "Белки"), ("fat", "Жиры"), ("carbs", "Углеводы"), ("fiber", "Клетчатка")]
    rows = []
    for key, label in names:
        t, g = round(totals[key]), target.get(key, 0)
        rows.append(f"{label}: {t} / {g} · осталось {max(g - t, 0)}" if t <= g else f"{label}: {t} / {g} · перебор {t - g}")
    return "\n".join(rows)


def remaining_line(totals: dict, target: dict) -> str:
    r = remaining(totals, target)
    return f"Осталось на день: {max(r['kcal'], 0)} ккал · Б{max(r['protein'], 0)} Ж{max(r['fat'], 0)} У{max(r['carbs'], 0)}"


def stats_view(d: date, logs: list, targets: dict, tz: str) -> str:
    _, target = targets_for(targets, d)
    totals = sum_items([{m: getattr(e, m) for m in MACROS} for e in logs])
    lines = [f"📊 Итоги за {d:%d.%m}", totals_block(totals, target)]
    if logs:
        lines += ["", "Записи:"] + [entry_line(i + 1, e, tz) for i, e in enumerate(logs)]
        lines.append("\nУдалить запись: /del номер")
    else:
        lines.append("\nЗаписей пока нет.")
    return "\n".join(lines)


# ---------- покупки ----------

CATEGORIES = [
    ("🥩 Мясо и рыба", ["кур", "свин", "фарш", "говя", "минта", "горбуш", "рыб", "индей", "печен", "лосос", "тунец"]),
    ("🥛 Молочное и яйца", ["молок", "сыр", "яйц", "кефир", "йогурт", "сметан", "сливочн"]),
    ("🥒 Овощи и фрукты", ["огур", "картоф", "банан", "яблок", "мандарин", "ягод", "помидор", "морков", "лук", "апельсин", "груш"]),
    ("🍞 Крупы, хлеб, макароны", ["рис", "гречк", "овсян", "макарон", "мука", "хлеб", "лаваш", "булгур", "батон"]),
    ("🥫 Бакалея и соусы", ["паста", "нутелл", "джем", "сахар", "масло", "соус", "кетчуп", "томат", "протеин", "специ"]),
]
SHOP_SKIP = ["свободный ужин", "кафе"]
PIECES = {"яйц": 55, "банан": 120, "огур": 150, "яблок": 150, "мандарин": 75}
LIQUIDS = ["молок", "кефир", "сок"]


def category(name: str) -> str:
    n = name.lower()
    for cat, keys in CATEGORIES:
        if any(k in n for k in keys):
            return cat
    return "📦 Прочее"


def fmt_amount(name: str, grams: float) -> str:
    n = name.lower()
    for key, per in PIECES.items():
        if key in n:
            return f"{math.ceil(grams / per - 0.05)} шт (~{fmt_weight(grams)})"
    if any(k in n for k in LIQUIDS):
        return f"{grams / 1000:.1f} л".replace(".", ",") if grams >= 1000 else f"{round(grams)} мл"
    return fmt_weight(grams)


def fmt_weight(grams: float) -> str:
    if grams >= 1000:
        return f"{grams / 1000:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " кг"
    return f"{round(grams)} г"


def shopping_list(days: list[list[dict]], title: str) -> str:
    total: dict[str, float] = {}
    display: dict[str, str] = {}
    for day_meals in days:
        for meal in day_meals:
            for dish in meal["dishes"]:
                for ing in dish.get("ingredients", []):
                    name = ing["name"].strip()
                    if any(s in name.lower() for s in SHOP_SKIP):
                        continue
                    key = name.lower()
                    display.setdefault(key, name)
                    total[key] = total.get(key, 0) + float(ing["grams"])
    if not total:
        return "Список пуст — в плане нет ингредиентов."
    groups: dict[str, list[str]] = {}
    for key in sorted(total):
        name = display[key]
        groups.setdefault(category(name), []).append(f"• {name} — {fmt_amount(name, total[key])}")
    order = [c for c, _ in CATEGORIES] + ["📦 Прочее"]
    out = [title]
    for cat in order:
        if cat in groups:
            out += ["", cat] + groups[cat]
    return "\n".join(out)


# ---------- вес ----------

def weight_view(rows: list[tuple[date, float]], today: date) -> str:
    """rows — (дата, вес) по возрастанию даты."""
    if not rows:
        return "Записей веса нет. Запиши: /w 78.4"
    last_d, last_w = rows[-1]
    lines = ["⚖️ Вес"] + [f"{d:%d.%m} — {w:.1f} кг" for d, w in rows[-7:]]

    def change(days: int, label: str):
        border = today - timedelta(days=days)
        past = [(d, w) for d, w in rows if d <= border]
        if past:
            diff = last_w - past[-1][1]
            lines.append(f"За {label}: {diff:+.1f} кг (с {past[-1][0]:%d.%m})")

    lines.append("")
    change(7, "неделю")
    change(30, "месяц")
    if len(lines) == len(rows[-7:]) + 2:
        lines.append("Для динамики нужно больше записей.")
    return "\n".join(lines)


# ---------- напоминания и отчёты ----------

def _in_quiet(t: time, start: str, end: str) -> bool:
    qs, qe = time(*hm(start)), time(*hm(end))
    return (t >= qs or t < qe) if qs > qe else (qs <= t < qe)


def due_events(now_utc: datetime, settings: dict, day_meals: list[dict],
               statuses: dict[str, str], sent: set[str]) -> list[dict]:
    """Что нужно отправить прямо сейчас. Чистая функция: время, план, отметки, уже отправленное.

    Окно «догонки» после рестарта: напоминание — до начала приёма,
    вечерний итог и недельный отчёт — в течение часа после своего времени.
    """
    s = {**DEFAULT_SETTINGS, **settings}
    tz = ZoneInfo(s["tz"])
    local = now_utc.astimezone(tz)
    today = local.date()
    events = []

    def at(t: str) -> datetime:
        return datetime.combine(today, time(*hm(t)), tzinfo=tz)

    if s["remind"] and not _in_quiet(local.time(), s["quiet_start"], s["quiet_end"]):
        before = timedelta(minutes=int(s["remind_before"]))
        for meal in day_meals:
            key = f"r:{today}:{meal['slot']}"
            start = at(meal["time"])
            if start - before <= local < start and meal["slot"] not in statuses and key not in sent:
                events.append({"type": "remind", "key": key, "date": today, "slot": meal["slot"]})

    ev = at(s["evening"])
    key = f"e:{today}"
    if ev <= local < ev + timedelta(hours=1) and key not in sent:
        events.append({"type": "evening", "key": key, "date": today})

    wk = at("20:00")
    key = f"w:{today}"
    if today.weekday() == 6 and wk <= local < wk + timedelta(hours=1) and key not in sent:
        events.append({"type": "weekly", "key": key, "date": today})
    return events


def remind_text(meal: dict, minutes: int) -> str:
    lines = [f"⏰ Через {minutes} мин — {meal['name']} ({meal['time']})"]
    for d in meal["dishes"]:
        lines.append(f"{d['name']} — {round(d['kcal'])} ккал")
        if d.get("note"):
            lines.append(f"↳ {d['note']}")
    return "\n".join(lines)


def evening_report(d: date, logs: list, day_meals: list[dict], targets: dict, now_local: datetime) -> str:
    _, target = targets_for(targets, d)
    totals = sum_items([{m: getattr(e, m) for m in MACROS} for e in logs])
    st = slot_statuses(logs)
    lines = [f"🌙 Итог дня {d:%d.%m}", totals_block(totals, target)]
    missed, ahead = [], []
    for meal in sorted(day_meals, key=lambda m: m["time"]):
        if meal["slot"] in st:
            continue
        (ahead if meal["time"] > now_local.strftime("%H:%M") else missed).append(f"{meal['time']} {meal['name']}")
    if missed:
        lines += ["", "Не отмечены: " + ", ".join(missed), "Отметить: /ate обед или /skip обед"]
    if ahead:
        lines += ["", "Ещё впереди: " + ", ".join(ahead)]
    if not missed and not ahead:
        lines += ["", "Все приёмы отмечены 👍"]
    return "\n".join(lines)


def weekly_report(ws: date, plan_days: dict, logs: list, targets: dict,
                  weights: list[tuple[date, float]]) -> str:
    by_date: dict[date, list] = {}
    for e in logs:
        by_date.setdefault(e.date, []).append(e)
    days_with = sorted(by_date)
    lines = [f"📈 Неделя {ws:%d.%m}–{ws + timedelta(days=6):%d.%m}"]
    if days_with:
        sums = [sum_items([{m: getattr(e, m) for m in MACROS} for e in by_date[d]]) for d in days_with]
        avg = {m: sum(x[m] for x in sums) / len(sums) for m in MACROS}
        tgt = [targets_for(targets, d)[1] for d in days_with]
        avg_t = {m: sum(t.get(m, 0) for t in tgt) / len(tgt) for m in MACROS}
        lines.append(f"Дней с записями: {len(days_with)}/7")
        lines.append(f"В среднем: {fmt_macros(avg)}")
        lines.append(f"Цель:      {fmt_macros(avg_t)}")
    else:
        lines.append("Записей за неделю нет.")

    planned = eaten = replaced = skipped = 0
    for i, key in enumerate(DAY_KEYS):
        d = ws + timedelta(days=i)
        st = slot_statuses(by_date.get(d, []))
        for meal in plan_days.get(key, []):
            planned += 1
            s = st.get(meal["slot"])
            eaten += s == "eaten"
            replaced += s == "replaced"
            skipped += s == "skipped"
    if planned:
        lines.append(f"Соблюдение плана: {round(eaten / planned * 100)}% "
                     f"(✅ {eaten}, 🔄 {replaced}, ⏭ {skipped}, без отметки {planned - eaten - replaced - skipped})")

    week_w = [(d, w) for d, w in weights if ws <= d <= ws + timedelta(days=6)]
    if week_w:
        avg_w = sum(w for _, w in week_w) / len(week_w)
        prev = [(d, w) for d, w in weights if ws - timedelta(days=7) <= d < ws]
        line = f"Вес: среднее {avg_w:.1f} кг"
        if prev:
            diff = avg_w - sum(w for _, w in prev) / len(prev)
            line += f" ({diff:+.1f} кг к прошлой неделе, цель +0,25…+0,45)"
        lines.append(line)
    else:
        lines.append("Вес за неделю не записан — /w 78.4")
    return "\n".join(lines)


# ---------- компактный план для ИИ (блюда описаны один раз, дни ссылаются на id) ----------

class Dish(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kcal: float = Field(ge=0, le=3000)
    protein: float = Field(ge=0, le=300)
    fat: float = Field(ge=0, le=300)
    carbs: float = Field(ge=0, le=600)
    ingredients: list[tuple[str, float]] = Field(min_length=1)
    note: str = Field("", max_length=300)

    @model_validator(mode="after")
    def _sane(self):
        for name, grams in self.ingredients:
            if not name.strip() or not 0 < grams <= 3000:
                raise ValueError(f"{self.name}: ингредиент «{name}» {grams} г — неверный вес")
        return self


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _dish_compact(d: dict) -> dict:
    out = {k: d[k] for k in ("name", "kcal", "protein", "fat", "carbs")}
    out["ingredients"] = [[i["name"], i["grams"]] for i in d["ingredients"]]
    if d.get("note"):
        out["note"] = d["note"]
    return out


def compact_plan(data: dict) -> dict:
    dishes: dict[str, dict] = {}
    index: dict[str, str] = {}
    days = {}
    for key in DAY_KEYS:
        meals = []
        for meal in data["days"].get(key, []):
            refs = []
            for d in meal["dishes"]:
                c = _dish_compact(d)
                sig = json.dumps(c, ensure_ascii=False, sort_keys=True)
                if sig not in index:
                    index[sig] = f"d{len(index) + 1}"
                    dishes[index[sig]] = c
                refs.append(index[sig])
            meals.append({"slot": meal["slot"], "name": meal["name"], "time": meal["time"], "dishes": refs})
        days[key] = meals
    return {"dishes": dishes, "days": days}


def expand_dish(raw, dishes: dict) -> dict:
    if isinstance(raw, str):
        if raw not in dishes:
            raise ValueError(f"Нет блюда с id {raw}")
        raw = dishes[raw]
    d = Dish(**raw)
    out = d.model_dump()
    out["ingredients"] = [{"name": n.strip(), "grams": g} for n, g in d.ingredients]
    return out


def expand_meals(meals: list, dishes: dict) -> list[dict]:
    out = []
    for m in meals:
        slot, time_ = str(m.get("slot", "")).strip(), str(m.get("time", "")).strip()
        if not re.fullmatch(r"[a-z_0-9]{1,20}", slot):
            raise ValueError(f"Неверный slot: {slot!r}")
        if not TIME_RE.match(time_):
            raise ValueError(f"Неверное время {time_!r} (нужно ЧЧ:ММ)")
        if not m.get("dishes"):
            raise ValueError(f"В приёме {slot} нет блюд")
        out.append({"slot": slot, "name": str(m.get("name") or slot)[:40], "time": time_,
                    "dishes": [expand_dish(x, dishes) for x in m["dishes"]]})
    slots = [m["slot"] for m in out]
    if len(slots) != len(set(slots)):
        raise ValueError("Повторяющиеся slot в одном дне")
    return sorted(out, key=lambda m: m["time"])


def expand_plan(compact: dict) -> dict:
    dishes = compact.get("dishes") or {}
    days = compact.get("days") or {}
    data = {"days": {k: expand_meals(days.get(k, []), dishes) for k in DAY_KEYS}}
    validate_plan(data)
    if any(not data["days"][k] for k in DAY_KEYS):
        raise ValueError("В каждом дне должен быть хотя бы один приём")
    return data


# ---------- контекст и статистика для ИИ ----------

def ai_context(now_local: datetime, profile_text: str, targets: dict, day_meals: list[dict], logs: list) -> str:
    d = now_local.date()
    variant, t = targets_for(targets, d)
    st = slot_statuses(logs)
    totals = sum_items([{m: getattr(e, m) for m in MACROS} for e in logs])
    plan = "; ".join(
        f"{STATUS_ICON.get(st.get(m['slot']), '—')} {m['time']} {m['name']} [{m['slot']}]: "
        + " + ".join(x["name"] for x in m["dishes"]) + f" {round(sum(x['kcal'] for x in m['dishes']))} ккал"
        for m in sorted(day_meals, key=lambda m: m["time"])) or "нет"
    entries = "; ".join(f"#{e.id} {e.description[:40]} {round(e.kcal)} ккал ({e.status})" for e in logs) or "нет"
    return (
        f"Сейчас: {DAY_SHORT[day_key(d)]} {d:%d.%m.%Y} {now_local:%H:%M}, {VARIANT_LABEL.get(variant, variant)}\n"
        f"Профиль: {profile_text}\n"
        f"Цель дня: {t['kcal']} ккал Б{t['protein']} Ж{t['fat']} У{t['carbs']} клетч.{t.get('fiber', 0)}\n"
        f"План сегодня: {plan}\n"
        f"Съедено: {fmt_macros(totals)}. {remaining_line(totals, t)}\n"
        f"Записи сегодня: {entries}"
    )


def stats_text(d1: date, d2: date, logs: list, plans_by_date: dict[date, list], targets: dict,
               weights: list[tuple[date, float]]) -> str:
    by_date: dict[date, list] = {}
    for e in logs:
        by_date.setdefault(e.date, []).append(e)
    lines, sums = [], []
    d = d1
    while d <= d2:
        dl = by_date.get(d, [])
        st = slot_statuses(dl)
        meals = plans_by_date.get(d, [])
        cnt = {k: sum(1 for m in meals if st.get(m["slot"]) == k) for k in STATUS_ICON}
        none = len(meals) - sum(cnt.values())
        if dl:
            s = sum_items([{m: getattr(e, m) for m in MACROS} for e in dl])
            sums.append(s)
            body = fmt_macros(s)
        else:
            body = "нет записей"
        lines.append(f"{DAY_SHORT[day_key(d)]} {d:%d.%m}: {body}; план ✅{cnt['eaten']} 🔄{cnt['replaced']} "
                     f"⏭{cnt['skipped']} —{none}")
        d += timedelta(days=1)
    if sums:
        avg = {m: sum(x[m] for x in sums) / len(sums) for m in MACROS}
        lines.append(f"Среднее за {len(sums)} дн. с записями: {fmt_macros(avg)}")
    for variant, t in targets.items():
        if isinstance(t, dict) and "kcal" in t:
            lines.append(f"Цель ({VARIANT_LABEL.get(variant, variant)}): {t['kcal']} ккал Б{t['protein']} Ж{t['fat']} У{t['carbs']}")
    if weights:
        lines.append("Вес: " + ", ".join(f"{wd:%d.%m} {w:.1f}" for wd, w in weights[-6:]))
    return "\n".join(lines)
