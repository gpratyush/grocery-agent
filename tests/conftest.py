from __future__ import annotations

import pytest

from grocery_agent.config import Band, Preferences, Settings
from grocery_agent.normalize import IngredientEntry, Normalizer, seed_store
from grocery_agent.store import Recipe, Store


class FakeOracle:
    """Stands in for the worker model: answers any unknown name with fixed facts."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def describe(self, names, known, currency):
        self.calls.append(list(names))
        return [IngredientEntry(name=n, canonical=n, category="other", each_g=100, kcal_100g=100,
                                protein_100g=5, carbs_100g=10, fat_100g=5, package_g=500, package_price=3.0)
                for n in names]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    seed_store(s)
    yield s
    s.close()


@pytest.fixture
def prefs():
    return Preferences(
        meals=3, servings=2,
        macros_per_serving={"calories": Band(min=300, max=900), "protein_g": Band(min=20)},
        cuisine_mix={"thai": 2, "italian": 1},
        staples=["salt", "olive oil"],
        adventurousness=Band(min=0.0, max=1.0),
        max_total_time_min=60,
    )


@pytest.fixture
def settings():
    return Settings()


def make_recipe(url, title, cuisine, ingredients, servings=4, time=30, nutrition=None):
    return Recipe(url=url, title=title, cuisine=cuisine, ingredients=ingredients, servings=servings,
                  total_time=time, nutrition=nutrition or {})


THAI_1 = make_recipe("https://a.com/thai-basil-chicken", "Thai Basil Chicken", "thai",
                     ["1.5 lb chicken thighs", "2 tbsp fish sauce", "1 tbsp soy sauce", "3 cloves garlic",
                      "1 cup basil", "2 cups jasmine rice", "1 tbsp vegetable oil"])
THAI_2 = make_recipe("https://a.com/green-curry", "Green Curry", "thai",
                     ["1 lb chicken breast", "1 can coconut milk", "2 tbsp fish sauce", "1 bell pepper",
                      "1 cup basil", "2 cups jasmine rice"])
ITALIAN_1 = make_recipe("https://b.com/pasta", "Chicken Pasta", "italian",
                        ["1 lb pasta", "1 lb chicken breast", "1 can diced tomatoes", "4 cloves garlic",
                         "1/2 cup parmesan cheese", "2 tbsp olive oil", "salt to taste"])
HEAVY = make_recipe("https://b.com/butter-bomb", "Butter Bomb", "italian",
                    ["2 lb butter", "1 lb pasta"], nutrition={"calories": 2500, "protein_g": 10})


@pytest.fixture
def pool(store):
    recipes = [THAI_1, THAI_2, ITALIAN_1, HEAVY]
    for r in recipes:
        store.add_recipe(r)
    Normalizer(store, FakeOracle()).normalize(recipes)
    return recipes
