"""Free-form feedback from Telegram: parsed by a (fake) model, applied by code, confirmed back."""

from __future__ import annotations

import pytest
from conftest import ITALIAN_1, THAI_1, THAI_2
from test_delivery import FakeTelegram

from grocery_agent.config import load_preferences, save_preferences
from grocery_agent.delivery import Telegram
from grocery_agent.inbox import Action, Inbox, sync


class ScriptedOracle:
    def __init__(self, *batches):
        self.batches, self.seen = list(batches), []

    def parse(self, text, recipes, prefs):
        self.seen.append((text, recipes))
        return self.batches.pop(0)


@pytest.fixture
def setup(store, prefs, pool, tmp_path):
    path = tmp_path / "preferences.yaml"
    save_preferences(prefs, path)
    store.record_plan("p1", [THAI_1.id, THAI_2.id, ITALIAN_1.id], "x.md", {})
    return store, path


def test_feedback_message_is_applied_and_confirmed(setup):
    store, path = setup
    oracle = ScriptedOracle([
        Action(kind="recipe", recipe="Green Curry", cooked=True, liked=True),
        Action(kind="recipe", recipe="chicken pasta", cooked=True, liked=False),
        Action(kind="pantry_remove", value="olive oil"),
        Action(kind="dislike_add", value="Mushrooms"),
    ])
    reply = Inbox(store, path, oracle).handle("the curry was great, pasta was bland. out of olive oil, no mushrooms")
    assert reply.splitlines()[:5] == ["Got it:", "❤️ Green Curry: cooked, liked", "👎 Chicken Pasta: cooked, not liked",
                                      "🧺 Pantry: removed olive oil", "🚫 Dislikes: added mushrooms"]
    assert reply.endswith('Reply "undo" to reverse this.')
    assert "Thai Basil Chicken" in oracle.seen[0][1]
    prefs = load_preferences(path)
    assert "olive oil" not in prefs.staples and prefs.dislikes == ["mushrooms"]
    assert store.feedback_row("p1", THAI_2.id)["liked"] == 1 and store.feedback_row("p1", ITALIAN_1.id)["liked"] == 0


def test_undo_restores_preferences_and_feedback(setup):
    store, path = setup
    inbox = Inbox(store, path, ScriptedOracle([Action(kind="recipe", recipe="Green Curry", cooked=True, liked=True),
                                               Action(kind="meals_set", value="4")]))
    inbox.handle("loved the curry, 4 meals next time")
    assert load_preferences(path).meals == 4
    assert inbox.handle("Undo").startswith("Undone")
    assert load_preferences(path).meals == 3 and store.feedback_row("p1", THAI_2.id) is None
    assert inbox.handle("undo") == "Nothing to undo."


def test_diet_allergy_and_budget_wait_for_yes(setup):
    store, path = setup
    inbox = Inbox(store, path, ScriptedOracle([Action(kind="diet_set", value="vegetarian"),
                                               Action(kind="dislike_add", value="olives")]))
    reply = inbox.handle("we're going vegetarian, and no olives")
    assert "🚫 Dislikes: added olives" in reply
    assert 'Needs your OK:\n🥗 Diet: set to vegetarian\nReply "yes" to apply or "no" to skip.' in reply
    assert load_preferences(path).diet == [] and load_preferences(path).dislikes == ["olives"]
    assert "🥗 Diet: vegetarian" in inbox.handle("Yes!")
    assert load_preferences(path).diet == ["vegetarian"]
    assert inbox.handle("yes") == "Nothing is waiting for your OK."
    assert inbox.handle("undo").startswith("Undone") and load_preferences(path).diet == []


def test_no_declines_a_pending_change(setup):
    store, path = setup
    inbox = Inbox(store, path, ScriptedOracle([Action(kind="budget_set", value="$80")]))
    inbox.handle("budget 80")
    assert inbox.handle("no") == "OK, left as is."
    assert load_preferences(path).budget is None


def test_invalid_values_are_reported_not_saved(setup):
    store, path = setup
    inbox = Inbox(store, path, ScriptedOracle([Action(kind="recipe", recipe="Lasagna", liked=True),
                                               Action(kind="note", value="more soups in winter")]))
    reply = inbox.handle("loved the lasagna; more soups please")
    assert "Couldn't use: no recent recipe called 'Lasagna'" in reply
    assert load_preferences(path).notes == "more soups in winter"


def test_sync_reads_only_your_chat_and_never_rereads(setup):
    store, path = setup
    updates = [
        {"update_id": 10, "message": {"chat": {"id": 42}, "text": "/start"}},
        {"update_id": 11, "message": {"chat": {"id": 999}, "text": "set budget to 1"}},
        {"update_id": 12, "message": {"chat": {"id": 42}, "text": "loved the curry"}},
    ]
    fake = FakeTelegram(updates=updates)
    tg = Telegram("123:abc", fake.client())
    oracle = ScriptedOracle([Action(kind="recipe", recipe="Green Curry", cooked=True, liked=True)])
    assert sync(Inbox(store, path, oracle), tg, "42") == 1
    sent = [r for m, r in fake.calls if m == "sendMessage"]
    assert len(sent) == 1 and b"Green+Curry" in sent[0].content
    assert store.get_meta("telegram_offset") == "13"
    fake.updates = []
    sync(Inbox(store, path, oracle), tg, "42")
    assert b"offset=13" in [r for m, r in fake.calls if m == "getUpdates"][-1].content


def test_a_failing_message_does_not_stop_the_rest(setup):
    store, path = setup

    class Broken:
        def parse(self, *a):
            raise RuntimeError("model down")

    fake = FakeTelegram(updates=[{"update_id": 1, "message": {"chat": {"id": 42}, "text": "hello"}}])
    assert sync(Inbox(store, path, Broken()), Telegram("123:abc", fake.client()), "42") == 1
    assert b"Nothing+was+changed" in [r for m, r in fake.calls if m == "sendMessage"][0].content


def test_plan_reads_telegram_first(monkeypatch, tmp_path, capsys):
    from grocery_agent import cli

    monkeypatch.setenv("GROCERY_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    order = []
    monkeypatch.setattr(cli, "read_telegram", lambda *a, **k: order.append("telegram"))
    monkeypatch.setattr(cli, "load_preferences", lambda *a: order.append("prefs") or (_ for _ in ()).throw(SystemExit))
    monkeypatch.setattr("grocery_agent.llm.make_model", lambda *a: object())
    with pytest.raises(SystemExit):
        cli.main(["plan"])
    assert order == ["telegram", "prefs"]
