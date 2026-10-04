"""Dietary restrictions are hard rules: a vegetarian plan can never contain beef."""

from __future__ import annotations

import json

import pytest
from conftest import ITALIAN_1, THAI_1, THAI_2, FakeOracle, make_recipe
from langchain_core.messages import ToolMessage
from test_planner import ScriptedModel, call, make_ctx

from grocery_agent.agent.planner import run_planner
from grocery_agent.agent.tools import build_tools, diet_query
from grocery_agent.config import Preferences
from grocery_agent.diet import Item, diet_violations, forbidden_term
from grocery_agent.normalize import Normalizer
from grocery_agent.scoring import Evaluator, greedy_plan

BEEF = make_recipe("https://c.com/beef-ragu", "Slow Cooker Ragu", "italian",
                   ["1 lb ground beef", "1 lb pasta", "1 can diced tomatoes", "2 cloves garlic"])
VEG_ITALIAN = make_recipe("https://c.com/pasta-e-ceci", "Pasta e Ceci", "italian",
                          ["1 lb pasta", "1 can chickpeas", "1 can diced tomatoes", "2 cloves garlic",
                           "1/2 cup parmesan cheese"])
TOFU_THAI = make_recipe("https://c.com/tofu-basil", "Thai Basil Tofu", "thai",
                        ["14 oz tofu", "2 tbsp soy sauce", "3 cloves garlic", "1 cup basil", "2 cups jasmine rice"])
TOFU_CURRY = make_recipe("https://c.com/tofu-curry", "Tofu Green Curry", "thai",
                         ["14 oz tofu", "1 can coconut milk", "1 bell pepper", "1 cup basil", "2 cups jasmine rice"])


@pytest.fixture
def veg_pool(store, pool):
    extra = [BEEF, VEG_ITALIAN, TOFU_THAI, TOFU_CURRY]
    for r in extra:
        store.add_recipe(r)
    Normalizer(store, FakeOracle()).normalize(extra)
    return pool + extra


@pytest.fixture
def veg(prefs):
    prefs.diet = ["vegetarian"]
    return prefs


@pytest.mark.parametrize("line,group,term", [
    ("1 lb ground beef", "meat", "beef"),
    ("2 cups chicken stock", "meat", "chicken"),
    ("1 packet gelatin", "meat", "gelatin"),
    ("2 tbsp fish sauce", "seafood", "fish"),
    ("4 anchovies, chopped", "seafood", "anchovies"),
    ("1 tbsp Worcestershire sauce", "seafood", "worcestershire"),
    ("2 eggs", "other_animal", "eggs"),
    ("1/2 cup heavy cream", "dairy", "cream"),
    ("8 oz spaghetti", "gluten", "spaghetti"),
])
def test_forbidden_words_are_caught(line, group, term):
    assert forbidden_term(line, group) == term


@pytest.mark.parametrize("line,group", [
    ("2 cups vegetable broth", "meat"),
    ("3 celery ribs, diced", "meat"),
    ("4 oz goat cheese", "meat"),
    ("2 tbsp vegan fish sauce", "seafood"),
    ("8 oz oyster mushrooms", "seafood"),
    ("1 can coconut milk", "dairy"),
    ("2 tbsp vegan butter", "dairy"),
    ("1/4 cup peanut butter", "dairy"),
    ("1 large eggplant", "other_animal"),
    ("8 oz rice noodles", "gluten"),
    ("2 tbsp tamari", "gluten"),
    ("12 oz gluten-free pasta", "gluten"),
])
def test_substitutes_are_allowed(line, group):
    assert forbidden_term(line, group) is None


def test_qualifier_only_covers_the_word_it_precedes():
    # "vegan" here describes the butter, not the chicken.
    assert diet_violations(["vegetarian"], "Chicken", [Item("2 tbsp vegan butter")]) == ["not vegetarian: chicken (title)"]
    assert diet_violations(["vegan"], "Stew", [Item("1 lb chicken, cooked in vegan butter")]) == \
        ["not vegan: chicken (1 lb chicken, cooked in vegan butter)"]


def test_category_catches_unlisted_meat():
    assert diet_violations(["vegetarian"], "Braise", [Item("2 lb short rib", "meat")]) == \
        ["not vegetarian: meat (2 lb short rib)"]
    assert diet_violations(["vegan"], "Curry", [Item("1 can coconut milk", "dairy")]) == []


def test_diet_preference_accepts_aliases_and_rejects_unknown():
    assert Preferences(diet="Vegetarian, gluten free").diet == ["vegetarian", "gluten-free"]
    assert Preferences.model_validate({"dietary_restrictions": ["pescetarian"]}).diet == ["pescatarian"]
    with pytest.raises(ValueError, match="unknown diet 'keto'"):
        Preferences(diet=["keto"])


def test_beef_recipe_is_excluded_for_vegetarians(store, veg, veg_pool):
    ev = Evaluator(store, veg)
    beef = ev.evaluate_recipe(BEEF)
    assert beef.excluded == ["not vegetarian: beef (1 lb ground beef)"]
    assert ev.evaluate_recipe(VEG_ITALIAN).excluded == []
    plan = ev.evaluate([BEEF.id, TOFU_THAI.id, TOFU_CURRY.id])
    assert plan.excluded_ids == [BEEF.id]
    assert "Slow Cooker Ragu: not vegetarian: beef (1 lb ground beef)" in plan.violations


def test_search_pool_never_returns_diet_breakers(store, veg, settings, veg_pool):
    tools = {t.name: t for t in build_tools(make_ctx(store, veg, settings))}
    out = json.loads(tools["search_pool"].invoke({"include_flagged": True, "limit": 50}))
    titles = {r["title"] for r in out["recipes"]}
    assert titles == {"Pasta e Ceci", "Thai Basil Tofu", "Tofu Green Curry", "Butter Bomb"}


def test_finalize_refuses_beef_and_planner_must_replace_it(store, veg, settings, veg_pool):
    model = ScriptedModel(script=[
        call("finalize_plan", {"recipe_ids": [BEEF.id, TOFU_THAI.id, TOFU_CURRY.id], "notes": "Ragu night."}, 1),
        call("finalize_plan", {"recipe_ids": [VEG_ITALIAN.id, TOFU_THAI.id, TOFU_CURRY.id], "notes": "Ceci."}, 2),
    ], seen=[])
    ctx = make_ctx(store, veg, settings)
    ids, notes = run_planner(ctx, model)
    refused = [m for m in model.seen[1] if isinstance(m, ToolMessage)][-1]
    assert '"ok":false' in refused.content and "Slow Cooker Ragu" in refused.content
    assert ids == [VEG_ITALIAN.id, TOFU_THAI.id, TOFU_CURRY.id] and notes == "Ceci."


def test_final_check_swaps_out_a_breaker_that_slipped_through(store, veg, settings, veg_pool):
    from grocery_agent.agent.planner import enforce_hard_rules

    ctx = make_ctx(store, veg, settings)
    ctx.final_ids, ctx.final_notes = [BEEF.id, TOFU_THAI.id, TOFU_CURRY.id], "Picked."
    ids = enforce_hard_rules(ctx)
    assert BEEF.id not in ids and VEG_ITALIAN.id in ids and len(ids) == 3
    assert "Removed for breaking your diet or allergies: Slow Cooker Ragu." in ctx.final_notes


def test_fallback_plan_is_vegetarian(store, veg, veg_pool):
    ids = greedy_plan(store, veg)
    assert sorted(ids) == sorted([VEG_ITALIAN.id, TOFU_THAI.id, TOFU_CURRY.id])
    assert not {THAI_1.id, THAI_2.id, ITALIAN_1.id, BEEF.id} & set(ids)


def test_allergies_are_hard_too(store, prefs, veg_pool):
    prefs.allergies = ["peanut"]
    r = make_recipe("https://c.com/satay", "Satay", "thai", ["1/2 cup peanut butter", "14 oz tofu"])
    store.add_recipe(r)
    assert Evaluator(store, prefs).evaluate_recipe(r).excluded == ["contains allergen 'peanut'"]


def test_sourcing_queries_carry_the_diet():
    assert diet_query("thai basil stir fry", ["vegetarian"]) == "vegetarian thai basil stir fry"
    assert diet_query("vegetarian lasagna", ["vegetarian"]) == "vegetarian lasagna"
    assert diet_query("lasagna", []) == "lasagna"


def test_opening_message_states_the_diet(store, veg, settings, veg_pool):
    from grocery_agent.agent.planner import opening_message

    assert "Diet (hard rule): vegetarian." in opening_message(make_ctx(store, veg, settings))
