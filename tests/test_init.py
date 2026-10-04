import pytest

from grocery_agent import cli
from grocery_agent.config import load_preferences


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROCERY_AGENT_HOME", str(tmp_path))
    for key in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def script(monkeypatch, answers, secret=None):
    """Feed answers to input() in order (then Enter); record prompts."""
    answers, prompts = list(answers), []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return answers.pop(0) if answers else ""

    def fake_getpass(prompt=""):
        prompts.append(prompt)
        if secret is None:
            raise AssertionError(f"unexpected secret prompt: {prompt}")
        return secret

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("getpass.getpass", fake_getpass)
    return prompts


def test_first_run_asks_everything_and_saves_key(home, monkeypatch):
    prompts = script(monkeypatch, [], secret="sk-test")
    assert cli.main(["init"]) == 0
    assert any(p.startswith("Meals to plan per run") for p in prompts)
    assert not any("Change this?" in p for p in prompts)
    assert "ANTHROPIC_API_KEY=sk-test" in (home / ".env").read_text()
    assert load_preferences(home / "preferences.yaml").meals == 5


def test_rerun_offers_to_skip_each_part(home, monkeypatch, capsys):
    script(monkeypatch, [], secret="sk-test")
    cli.main(["init"])
    monkeypatch.delenv("ANTHROPIC_API_KEY")  # a fresh process would load it from .env
    capsys.readouterr()

    prompts = script(monkeypatch, [])  # Enter everywhere: keep everything, no secret prompt
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert "✓ Meals: 5 meals × 2 servings" in out and "✓ ANTHROPIC_API_KEY is set." in out
    assert sum("Change this?" in p for p in prompts) == len(cli.PREF_SECTIONS)
    assert not any(p.startswith("Meals to plan per run") for p in prompts)


def test_rerun_changes_only_the_chosen_part(home, monkeypatch):
    script(monkeypatch, [], secret="sk-test")
    cli.main(["init"])
    prompts = script(monkeypatch, ["y", "7", "3"])  # change Meals: 7 meals, 3 servings
    cli.main(["init"])
    prefs = load_preferences(home / "preferences.yaml")
    assert (prefs.meals, prefs.servings) == (7, 3)
    assert prefs.cuisine_mix == {"italian": 1, "mexican": 1, "indian": 1, "thai": 1}
    assert not any(p.startswith("Cuisine mix") for p in prompts)


def test_redo_asks_everything_again(home, monkeypatch):
    script(monkeypatch, [], secret="sk-test")
    cli.main(["init"])
    prompts = script(monkeypatch, [], secret="")
    cli.main(["init", "--redo"])
    assert any(p.startswith("Cuisine mix") for p in prompts)
    assert not any("Change this?" in p for p in prompts)


def test_telegram_step_skipped_when_connected(home, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(cli, "connect_telegram", lambda env: pytest.fail("should not reconnect"))
    answers = [""] * 15 + ["telegram"]  # Enter through preferences, choose telegram delivery
    script(monkeypatch, answers, secret="sk-test")
    cli.main(["init"])
    assert "✓ Telegram is connected." in capsys.readouterr().out
