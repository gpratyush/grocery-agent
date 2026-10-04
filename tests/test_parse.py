import pytest

from grocery_agent.parse import parse_line, to_grams


@pytest.mark.parametrize("line,qty,unit,name", [
    ("2 1/2 cups finely chopped onion (about 2)", 2.5, "cup", "onion"),
    ("1 (14 oz) can coconut milk", 1, "can", "coconut milk"),
    ("3 cloves garlic, minced", 3, "clove", "garlic"),
    ("1½ lb boneless skinless chicken thighs", 1.5, "lb", "chicken thighs"),
    ("1-2 tbsp fish sauce", 1.5, "tbsp", "fish sauce"),
    ("200g basmati rice", 200, "g", "basmati rice"),
    ("2 large eggs", 2, "", "eggs"),
    ("Salt and pepper to taste", None, "", "salt and pepper"),
    ("4 carrots, peeled", 4, "", "carrots"),
])
def test_parse_line(line, qty, unit, name):
    p = parse_line(line)
    assert p.quantity == (pytest.approx(qty) if qty is not None else None)
    assert p.unit == unit
    assert p.name == name


def test_to_grams_by_unit_kind():
    assert to_grams(parse_line("1 lb chicken"), None, None) == pytest.approx(453.6)
    assert to_grams(parse_line("1 cup rice"), None, 0.85) == pytest.approx(204)
    assert to_grams(parse_line("3 cloves garlic"), None, None) == 15
    assert to_grams(parse_line("2 eggs"), 50, None) == 100
    assert to_grams(parse_line("2 eggs"), None, None) is None
    assert to_grams(parse_line("salt to taste"), None, None) is None
