from conftest import THAI_1, FakeOracle

from grocery_agent.normalize import Normalizer


def test_seeded_names_resolve_without_model(store):
    oracle = FakeOracle()
    mapping = Normalizer(store, oracle).resolve_names({"chicken thighs", "eggs", "tomatoes", "fish sauce"})
    assert mapping == {"chicken thighs": "chicken thigh", "eggs": "egg", "tomatoes": "tomato",
                       "fish sauce": "fish sauce"}
    assert oracle.calls == []


def test_unknown_names_hit_model_once(store):
    oracle = FakeOracle()
    n = Normalizer(store, oracle)
    n.resolve_names({"galangal"})
    n.resolve_names({"galangal"})
    assert oracle.calls == [["galangal"]]
    assert store.facts("galangal").source == "llm"


def test_normalize_writes_items_with_grams(store):
    store.add_recipe(THAI_1)
    Normalizer(store, FakeOracle()).normalize([THAI_1])
    items = {i["canonical"]: i["grams"] for i in store.items(THAI_1.id)}
    assert items["chicken thigh"] > 600
    assert items["garlic"] == 15
