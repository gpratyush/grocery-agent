"""Ingredient swaps: judged once by the model, applied by code only when they save money."""

from __future__ import annotations

import pytest
from conftest import ITALIAN_1, THAI_1, THAI_2, FakeOracle, make_recipe

from grocery_agent.config import Preferences
from grocery_agent.normalize import Normalizer
from grocery_agent.pantry import staples_key
from grocery_agent.scoring import Evaluator
from grocery_agent.substitute import SubEntry, SubOption, Substitutions, SwapFinder, locked

PLAN = [THAI_1.id, THAI_2.id, ITALIAN_1.id]  # thighs in one, breast in two
ROASTED = make_recipe("https://d.com/roast-veg", "Roasted Vegetables", "italian",
                      ["2 shallots", "1 lb pasta", "2 tbsp olive oil"])


def judge(store, prefs, a, b, quality):
    store.set_swap(staples_key(prefs.staples), a, b, quality)


def test_merges_chicken_onto_one_pack_size_when_cheaper(store, prefs, pool):
    before = Evaluator(store, prefs).evaluate(PLAN)
    judge(store, prefs, "chicken breast", "chicken thigh", "same")
    ev = Evaluator(store, prefs).evaluate(PLAN)
    names = {g.canonical: g for g in ev.grocery}
    assert "chicken breast" not in names
    assert names["chicken thigh"].used_by == ["Thai Basil Chicken", "Green Curry", "Chicken Pasta"]
    assert [s.describe() for s in ev.swaps] == ["Green Curry: chicken breast → chicken thigh",
                                                "Chicken Pasta: chicken breast → chicken thigh"]
    # 340 g thighs + 454 g breast was one pack each (5.50 + 7.50); 794 g of thighs is two packs (11.00)
    assert ev.swap_savings == pytest.approx(2.0)
    assert ev.total_cost == pytest.approx(before.total_cost - 2.0)
    assert ev.summary()["swaps"] and ev.summary()["swap_savings"] == 2.0


def test_only_items_already_on_the_list_are_merged(store, prefs, pool):
    judge(store, prefs, "chicken breast", "chicken thigh", "same")
    ev = Evaluator(store, prefs).evaluate([THAI_1.id])  # thighs only: buying breast instead saves nothing
    assert ev.swaps == [] and ev.swap_savings == 0


@pytest.mark.parametrize("level,applied", [("off", False), ("same", False), ("close", True), ("liberal", True)])
def test_level_controls_which_qualities_apply(store, prefs, pool, level, applied):
    judge(store, prefs, "chicken breast", "chicken thigh", "close")
    prefs.substitutions = Substitutions(level=level)
    assert bool(Evaluator(store, prefs).evaluate(PLAN).swaps) == applied


def test_never_and_allow_lists_override_the_model(store, prefs, pool):
    judge(store, prefs, "chicken breast", "chicken thigh", "same")
    prefs.substitutions = Substitutions(never=["Chicken Thigh = chicken breast"])
    assert Evaluator(store, prefs).evaluate(PLAN).swaps == []
    store.db.execute("DELETE FROM substitutes")
    prefs.substitutions = Substitutions(level="off", allow=["chicken breast = chicken thigh"])
    assert len(Evaluator(store, prefs).evaluate(PLAN).swaps) == 2


def test_swap_to_a_pantry_item_drops_it_from_the_list(store, prefs):
    store.add_recipe(ROASTED)
    Normalizer(store, FakeOracle()).normalize([ROASTED])
    prefs.staples = ["salt", "olive oil", "onion"]
    shallot = store.items(ROASTED.id)[0]["canonical"]
    judge(store, prefs, shallot, "onion", "close")
    ev = Evaluator(store, prefs).evaluate([ROASTED.id])
    assert shallot not in {g.canonical for g in ev.grocery}
    assert [s.describe() for s in ev.swaps] == [f"Roasted Vegetables: {shallot} → onion (from your pantry)"]
    assert ev.swap_savings > 0


def test_namesake_ingredients_are_locked():
    assert locked("Thai Basil Chicken", "basil", "cilantro")
    assert not locked("Thai Basil Chicken", "chicken thigh", "chicken breast")
    assert not locked("Green Curry", "basil", "cilantro")
    assert locked("Chicken Thighs with Rice", "chicken thigh", "tofu")


def test_substitute_must_obey_the_diet(store, prefs):
    tofu = make_recipe("https://d.com/tofu", "Crispy Tofu Bowl", "thai", ["14 oz tofu", "1 cup rice"])
    store.add_recipe(tofu)
    Normalizer(store, FakeOracle()).normalize([tofu])
    prefs.diet = ["vegetarian"]
    prefs.staples = ["chicken thigh"]  # silly, but would make the swap free
    judge(store, prefs, "tofu", "chicken thigh", "close")
    assert Evaluator(store, prefs).evaluate([tofu.id]).swaps == []


def test_pair_format_is_validated():
    with pytest.raises(ValueError, match="cilantro = parsley"):
        Preferences(substitutions={"allow": ["cilantro"]})
    assert Preferences(substitutions={"never": ["Butter=Olive Oil"]}).substitutions.never == ["butter = olive oil"]


class FakeSubOracle:
    def __init__(self):
        self.calls = []

    def suggest(self, names, candidates):
        self.calls.append({n: list(candidates[n]) for n in names})
        out = []
        for n in names:
            opts = [SubOption(name=c, quality="same") for c in candidates[n] if "chicken" in n and "chicken" in c]
            opts += [SubOption(name="made up", quality="same")]  # not a candidate: ignored
            out.append(SubEntry(name=n, substitutes=opts))
        return out


def test_finder_asks_once_per_ingredient_and_stores_both_directions(store, prefs, pool):
    oracle = FakeSubOracle()
    finder = SwapFinder(store, oracle, prefs.staples)
    finder.prepare_pool()
    asked = {k: v for call in oracle.calls for k, v in call.items()}
    assert "chicken thigh" in asked["chicken breast"]          # same category (meat)
    assert "jasmine rice" not in asked["chicken breast"]        # other categories are not candidates
    assert "olive oil" in asked["chicken breast"]               # pantry entries always are
    table = store.swaps(staples_key(prefs.staples))
    assert table["chicken breast"]["chicken thigh"] == "same" == table["chicken thigh"]["chicken breast"]
    assert "made up" not in table.get("chicken breast", {})
    n = len(oracle.calls)
    finder.prepare_pool()
    assert len(oracle.calls) == n
    SwapFinder(store, oracle, prefs.staples + ["onion"]).prepare_pool()  # new pantry list: judged again
    assert len(oracle.calls) > n
