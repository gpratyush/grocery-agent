from conftest import FakeOracle, ITALIAN_1, THAI_1, THAI_2

from grocery_agent import cli
from grocery_agent.config import load_preferences, save_preferences
from grocery_agent.delivery import telegram_messages
from grocery_agent.normalize import Normalizer
from grocery_agent.pantry import PantryMatch, PantryMatcher
from grocery_agent.render import render_plan
from grocery_agent.scoring import Evaluator
from grocery_agent.store import Store

PLAN = [THAI_1.id, THAI_2.id, ITALIAN_1.id]


class FakePantryOracle:
    """Stands in for the worker model: fish sauce is covered by 'thai pantry', basil maybe."""

    def __init__(self, answers=None):
        self.answers = answers or {"fish sauce": ("covered", "thai pantry"), "basil": ("maybe", "thai pantry")}
        self.calls: list[list[str]] = []

    def classify(self, names, staples):
        self.calls.append(list(names))
        return [PantryMatch(name=n, verdict=self.answers.get(n, ("no", None))[0],
                            pantry_entry=self.answers.get(n, ("no", None))[1]) for n in names]


def _plan(store, prefs, oracle=None):
    prefs.staples = ["salt", "olive oil", "thai pantry"]
    PantryMatcher(store, oracle or FakePantryOracle(), prefs.staples).prepare_pool()
    return Evaluator(store, prefs).evaluate(PLAN)


def test_broad_pantry_entries_cover_and_flag_ingredients(store, prefs, pool):
    ev = _plan(store, prefs)
    names = {g.canonical for g in ev.grocery}
    assert "fish sauce" not in names and "basil" not in names
    assert "fish sauce" in ev.pantry and ev.pantry_sources["fish sauce"] == "thai pantry"
    [basil] = ev.pantry_check
    assert basil.canonical == "basil" and basil.pantry_hint == "thai pantry"
    assert basil.used_by == ["Thai Basil Chicken", "Green Curry"]
    assert basil.cost and ev.total_if_buying_checks == ev.total_cost + basil.cost
    assert ev.summary()["pantry_checks"] == ["basil"]


def test_budget_uses_the_lower_total(store, prefs, pool):
    ev = _plan(store, prefs)
    prefs.budget = (ev.total_cost + ev.total_if_buying_checks) / 2
    assert not any("over budget" in v for v in Evaluator(store, prefs).evaluate(PLAN).violations)


def test_verdicts_are_cached_per_pantry_list(store, prefs, pool):
    oracle = FakePantryOracle()
    PantryMatcher(store, oracle, ["salt", "thai pantry"]).prepare_pool()
    asked = oracle.calls[0]
    assert "fish sauce" in asked and "salt" not in asked  # exact matches never reach the model
    PantryMatcher(store, oracle, ["thai pantry", "salt"]).prepare_pool()  # same list, other order
    assert len(oracle.calls) == 1
    PantryMatcher(store, oracle, ["salt", "thai pantry", "rice"]).prepare_pool()
    assert len(oracle.calls) == 2


def test_match_must_name_a_real_pantry_entry(store, prefs, pool):
    oracle = FakePantryOracle({"fish sauce": ("covered", "asian sauces")})
    ev = _plan(store, prefs, oracle)
    assert "fish sauce" in {g.canonical for g in ev.grocery}


def test_normalizer_checks_new_ingredients(store, prefs):
    oracle = FakePantryOracle()
    store.add_recipe(THAI_2)
    Normalizer(store, FakeOracle(), pantry=PantryMatcher(store, oracle, ["thai pantry"])).normalize([THAI_2])
    assert "fish sauce" in oracle.calls[0]


def test_outputs_show_both_totals_and_the_check_section(store, prefs, pool):
    ev = _plan(store, prefs)
    md = render_plan("p1", ev, prefs)
    assert f'({ev.total_if_buying_checks:.2f} USD if you need the 1 "check your pantry" item)' in md
    assert "**Check your pantry**" in md and "- [ ] basil:" in md and "thai pantry?" in md
    assert "**Assumed in your pantry:** olive oil, salt; fish sauce (thai pantry)" in md
    overview, _meals, shopping = telegram_messages(ev, prefs)
    assert "if you need the 1" in overview
    assert "<b>Check your pantry</b>" in shopping and "▫️ basil:" in shopping and "(thai pantry?)" in shopping
    assert "fish sauce (thai pantry)" in shopping


def test_pantry_command_lists_adds_and_removes(tmp_path, monkeypatch, capsys, prefs):
    monkeypatch.setenv("GROCERY_AGENT_HOME", str(tmp_path))
    save_preferences(prefs)
    assert cli.main(["pantry", "add", "indian", "spices,", "rice"]) == 0
    assert load_preferences().staples == ["salt", "olive oil", "indian spices", "rice"]
    assert cli.main(["pantry", "remove", "rice,", "saffron"]) == 0
    out = capsys.readouterr().out
    assert "Removed rice. Not in your pantry: saffron." in out
    assert load_preferences().staples == ["salt", "olive oil", "indian spices"]

    store = Store(tmp_path / "grocery.db")
    PantryMatcher(store, FakePantryOracle({"cumin": ("covered", "indian spices"),
                                           "ginger": ("maybe", "indian spices")}),
                  load_preferences().staples).prepare({"cumin", "ginger", "salt"})
    store.close()
    assert cli.main(["pantry"]) == 0
    assert "  - indian spices  (covers: cumin; maybe: ginger)" in capsys.readouterr().out
