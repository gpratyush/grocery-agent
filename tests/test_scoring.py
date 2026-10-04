from conftest import HEAVY, ITALIAN_1, THAI_1, THAI_2

from grocery_agent.scoring import Evaluator, greedy_plan


def test_feasible_plan_has_costed_grocery_list(store, prefs, pool):
    ev = Evaluator(store, prefs).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    assert ev.feasible, ev.violations
    names = {g.canonical for g in ev.grocery}
    assert {"chicken breast", "jasmine rice", "fish sauce"} <= names
    assert "olive oil" in ev.pantry and "salt" in " ".join(ev.pantry)
    breast = next(g for g in ev.grocery if g.canonical == "chicken breast")
    assert breast.used_by == ["Green Curry", "Chicken Pasta"]
    assert breast.packages == 1  # 2 x (1 lb scaled to 2 servings of 4) = 453.6 g -> one 680 g pack
    assert ev.total_cost > 0
    assert ev.cuisine_counts == {"thai": 2, "italian": 1}


def test_violations_are_reported(store, prefs, pool):
    prefs.budget = 5
    ev = Evaluator(store, prefs).evaluate([HEAVY.id, THAI_1.id])
    text = " | ".join(ev.violations)
    assert "Butter Bomb: calories 2500 outside 300–900" in text
    assert "plan has 2 meals, need 3" in text
    assert "need 1 more thai" in text
    assert "over budget" in text


def test_repeats_and_novelty_use_history(store, prefs, pool):
    store.record_plan("p1", [THAI_1.id], "x.md", {})
    ev = Evaluator(store, prefs)
    assert ev.evaluate_recipe(THAI_1).recently_suggested
    plan = ev.evaluate([THAI_2.id, ITALIAN_1.id, THAI_1.id])
    assert plan.new_fraction == 2 / 3
    assert any("suggested within" in v for v in plan.violations)


def test_allergens_flagged(store, prefs, pool):
    prefs.allergies = ["fish sauce"]
    assert any("allergen" in v for v in Evaluator(store, prefs).evaluate_recipe(THAI_2).violations)


def test_greedy_plan_fills_targets_with_clean_recipes(store, prefs, pool):
    ids = greedy_plan(store, prefs)
    assert sorted(ids) == sorted([THAI_1.id, THAI_2.id, ITALIAN_1.id])
