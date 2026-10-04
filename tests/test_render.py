from conftest import ITALIAN_1, THAI_1, THAI_2

from grocery_agent.render import parse_feedback, render_plan
from grocery_agent.scoring import Evaluator


def test_render_and_feedback_roundtrip(store, prefs, pool):
    ev = Evaluator(store, prefs).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    md = render_plan("abc123", ev, prefs, notes="Two Thai, one Italian.")
    assert "## Grocery list" in md and "Thai Basil Chicken" in md and "Two Thai" in md
    md = md.replace(f"- [ ] cooked [ ] liked · Green Curry", "- [x] cooked [x] liked · Green Curry")
    rows = parse_feedback(md)
    assert ("abc123", THAI_2.id, True, True) in rows
    assert ("abc123", THAI_1.id, False, False) in rows
