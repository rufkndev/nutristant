from app import services as s


def meal(*ings):
    return {"slot": "lunch", "name": "Обед", "time": "13:00",
            "dishes": [{"name": "x", "kcal": 1, "protein": 0, "fat": 0, "carbs": 0,
                        "ingredients": [{"name": n, "grams": g} for n, g in ings]}]}


def test_sum_and_categories():
    days = [[meal(("Рис", 120), ("Куриные бёдра", 267))], [meal(("рис", 80), ("Тофу", 100))]]
    out = s.shopping_list(days, "T")
    assert "Рис — 200 г" in out
    assert "Куриные бёдра — 267 г" in out
    assert out.index("🥩 Мясо и рыба") < out.index("Куриные бёдра")
    assert out.index("📦 Прочее") < out.index("Тофу")


def test_units_and_skip():
    days = [[meal(("Яйца", 220), ("Молоко 3,2%", 1500), ("Свободный ужин (кафе)", 1), ("Картофель", 2000))]]
    out = s.shopping_list(days, "T")
    assert "Яйца — 4 шт" in out
    assert "Молоко 3,2% — 1,5 л" in out
    assert "Картофель — 2 кг" in out
    assert "Свободный" not in out


def test_seed_week_matches_purchase():
    _, _, plan = s.load_seed("seed")
    out = s.shopping_list([plan["days"][k] for k in s.DAY_KEYS], "T")
    assert "Свиная лопатка — 1 кг" in out
    assert "Картофель — 2 кг" in out
    assert "Фарш свино-говяжий — 800 г" in out


def test_days_limit():
    _, _, plan = s.load_seed("seed")
    three = s.shopping_list([plan["days"][k] for k in s.DAY_KEYS[:3]], "T")
    assert "Горбуша" not in three          # горбуша только в чт
    assert "Минтая" in three or "минтая" in three
