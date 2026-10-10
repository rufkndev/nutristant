import pytest

from app import services as s


def test_sum_items():
    items = [{"name": "гречка", "kcal": 330, "protein": 12, "fat": 3, "carbs": 62, "fiber": 10},
             {"name": "грудка", "kcal": 165.5, "protein": 31, "fat": 3.6, "carbs": 0}]
    assert s.sum_items(items) == {"kcal": 495.5, "protein": 43.0, "fat": 6.6, "carbs": 62.0, "fiber": 10.0}


def test_sum_empty():
    assert s.sum_items([]) == {m: 0 for m in s.MACROS}


def test_validate_ok():
    out = s.validate_items([{"name": "рис", "grams": 100, "kcal": 350, "protein": 7, "fat": 1, "carbs": 77}])
    assert out[0]["kcal"] == 350 and out[0]["fiber"] == 0


@pytest.mark.parametrize("item", [
    {"name": "x", "kcal": -10, "protein": 0, "fat": 0, "carbs": 0},          # отрицательное
    {"name": "x", "kcal": 100, "protein": -1, "fat": 0, "carbs": 0},
    {"name": "x", "kcal": 50000, "protein": 0, "fat": 0, "carbs": 0},       # абсурдно много
    {"name": "x", "grams": 10, "kcal": 500, "protein": 0, "fat": 0, "carbs": 0},  # 5000 ккал/100 г
    {"name": "x", "grams": 100, "kcal": 400, "protein": 80, "fat": 30, "carbs": 0},  # БЖУ > веса
    {"name": "x", "kcal": 100, "protein": 50, "fat": 50, "carbs": 50},      # БЖУ не сходятся с ккал
    {"name": "", "kcal": 100, "protein": 1, "fat": 1, "carbs": 1},
])
def test_validate_rejects(item):
    with pytest.raises(ValueError):
        s.validate_items([item])


def test_validate_empty_list():
    with pytest.raises(ValueError):
        s.validate_items([])


def test_remaining_and_targets():
    from datetime import date
    _, targets, _ = s.load_seed("seed")
    assert s.targets_for(targets, date(2026, 10, 12))[0] == "training"   # пн
    assert s.targets_for(targets, date(2026, 10, 18))[0] == "rest"       # вс
    rem = s.remaining({"kcal": 1000, "protein": 50, "fat": 0, "carbs": 0, "fiber": 0},
                      targets["training"])
    assert rem["kcal"] == targets["training"]["kcal"] - 1000


def test_profile_limit():
    assert s.check_profile("  ок  ") == "ок"
    s.check_profile("а" * s.PROFILE_LIMIT)
    with pytest.raises(ValueError):
        s.check_profile("а" * (s.PROFILE_LIMIT + 1))
    with pytest.raises(ValueError):
        s.check_profile("   ")
