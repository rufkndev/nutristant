import json
from datetime import date

import pytest

from app import services as s


def test_week_start():
    assert s.week_start(date(2026, 10, 18)) == date(2026, 10, 12)
    assert s.week_start(date(2026, 10, 12)) == date(2026, 10, 12)


def test_copy_plan_is_deep():
    _, _, plan = s.load_seed("seed")
    c = s.copy_plan(plan)
    assert c == plan
    c["days"]["mon"][0]["dishes"][0]["name"] = "другое"
    assert plan["days"]["mon"][0]["dishes"][0]["name"] != "другое"


def test_parse_day_and_slot():
    today = date(2026, 10, 14)  # ср
    assert s.parse_day("пт", today) == date(2026, 10, 16)
    assert s.parse_day("понедельник", today) == date(2026, 10, 12)
    assert s.parse_day("завтра", today) == date(2026, 10, 15)
    assert s.parse_day("xyz", today) is None
    _, _, plan = s.load_seed("seed")
    assert s.find_slot("обед", plan["days"]["mon"]) == "lunch"
    assert s.find_slot("перед сном", plan["days"]["mon"]) == "late"
    assert s.find_slot("перекус", plan["days"]["sun"]) is None  # в вс перекуса нет


def test_compact_roundtrip():
    _, _, plan = s.load_seed("seed")
    c = s.compact_plan(plan)
    assert len(c["dishes"]) < sum(len(m["dishes"]) for d in plan["days"].values() for m in d)
    assert s.expand_plan(json.loads(json.dumps(c))) == plan


def test_expand_rejects_bad_input():
    good = {"name": "x", "kcal": 100, "protein": 5, "fat": 5, "carbs": 5, "ingredients": [["рис", 50]]}
    with pytest.raises(ValueError):
        s.expand_meals([{"slot": "lunch", "name": "Обед", "time": "25:00", "dishes": [good]}], {})
    with pytest.raises(ValueError):
        s.expand_meals([{"slot": "lunch", "name": "Обед", "time": "13:00", "dishes": ["d9"]}], {})
    with pytest.raises(ValueError):
        s.expand_meals([{"slot": "lunch", "name": "Обед", "time": "13:00",
                         "dishes": [{**good, "ingredients": []}]}], {})
    with pytest.raises(ValueError):
        s.expand_meals([{"slot": "lunch", "name": "Обед", "time": "13:00",
                         "dishes": [{**good, "kcal": -1}]}], {})


# ---------- seed ----------

def test_seed_plan_has_all_days_and_ingredients():
    plan = json.loads(open("seed/plan.json", encoding="utf-8").read())
    assert set(plan["days"]) == set(s.DAY_KEYS)
    for key, meals in plan["days"].items():
        assert meals, key
        for m in meals:
            assert s.TIME_RE.match(m["time"]), (key, m["time"])
            for d in m["dishes"]:
                assert d["ingredients"], (key, d["name"])
                assert all(i["grams"] > 0 and i["name"] for i in d["ingredients"])
                assert d["kcal"] > 0


def test_seed_plan_matches_targets():
    profile, targets, plan = s.load_seed("seed")
    assert len(profile) <= s.PROFILE_LIMIT
    for i, key in enumerate(s.DAY_KEYS):
        d = date(2026, 10, 12 + i)
        kcal = s.day_plan_totals(plan["days"][key])["kcal"]
        target = s.targets_for(targets, d)[1]["kcal"]
        assert abs(kcal - target) / target < 0.1, (key, kcal, target)
