from grocery_agent import cli
from grocery_agent.store import Store


def test_plans_live_under_home_and_feedback_defaults_to_latest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GROCERY_AGENT_HOME", str(tmp_path / "home"))
    path = cli.plan_path("abc123")
    assert path.parent == tmp_path / "home" / "plans" and path.name.endswith("-abc123.md")

    path.parent.mkdir(parents=True)
    path.write_text("- [x] cooked [ ] liked · Soup <!-- plan:abc123 recipe:r1 -->\n")
    store = Store(tmp_path / "home" / "grocery.db")
    store.record_plan("abc123", ["r1"], str(path), {"total_cost": 12.5, "recipes": [{"title": "Soup"}]})
    store.close()

    assert cli.main(["history"]) == 0
    out = capsys.readouterr().out
    assert "abc123  12.50  Soup" in out and str(path) in out

    assert cli.main(["feedback"]) == 0
    assert "Recorded feedback for 1 recipes" in capsys.readouterr().out
    store = Store(tmp_path / "home" / "grocery.db")
    assert [(r["recipe_id"], r["cooked"], r["liked"]) for r in store.feedback_rows()] == [("r1", 1, 0)]
