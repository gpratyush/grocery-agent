import httpx
import pytest
from conftest import ITALIAN_1, THAI_1, THAI_2

from grocery_agent.delivery import DeliveryError, Telegram, _pack, deliver, set_env_var, telegram_messages
from grocery_agent.scoring import Evaluator


class FakeTelegram:
    """httpx transport that records Bot API calls."""

    def __init__(self, updates=None, fail=None):
        self.calls, self.updates, self.fail = [], updates or [], fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, request))
        if method == self.fail:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        result = {"getMe": {"username": "my_grocery_bot"}, "getUpdates": self.updates}.get(method, {})
        return httpx.Response(200, json={"ok": True, "result": result})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def _plan(store, prefs):
    return Evaluator(store, prefs).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])


def test_file_delivery_sends_nothing(store, prefs, pool, tmp_path):
    fake = FakeTelegram()
    assert deliver(tmp_path / "p.md", _plan(store, prefs), prefs, fake.client()).startswith("saved to")
    assert fake.calls == []


def test_telegram_sends_short_messages_then_file(store, prefs, pool, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    prefs.delivery = "telegram"
    path = tmp_path / "plan.md"
    path.write_text("# plan")
    fake = FakeTelegram()
    assert deliver(path, _plan(store, prefs), prefs, fake.client()) == "sent to Telegram"
    assert [m for m, _ in fake.calls] == ["sendMessage", "sendMessage", "sendMessage", "sendDocument"]
    assert "/bot123:abc/" in str(fake.calls[0][1].url)
    meals = fake.calls[1][1].content.decode()
    assert "chat_id=42" in meals and "parse_mode=HTML" in meals and "Thai+Basil+Chicken" in meals
    assert b'filename="plan.md"' in fake.calls[3][1].content


def test_telegram_errors_are_reported(store, prefs, pool, tmp_path, monkeypatch):
    prefs.delivery = "telegram"
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(DeliveryError, match="TELEGRAM_CHAT_ID"):
        deliver(tmp_path / "p.md", _plan(store, prefs), prefs)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    (tmp_path / "p.md").write_text("x")
    with pytest.raises(DeliveryError, match="chat not found"):
        deliver(tmp_path / "p.md", _plan(store, prefs), prefs, FakeTelegram(fail="sendMessage").client())


def test_latest_chat_id_from_updates():
    updates = [{"message": {"chat": {"id": 1}}}, {"message": {"chat": {"id": 777}}}]
    assert Telegram("t", FakeTelegram(updates).client()).latest_chat_id() == "777"
    assert Telegram("t", FakeTelegram([]).client()).latest_chat_id() is None


def test_telegram_messages_are_readable_html(store, prefs, pool):
    overview, meals, shopping = telegram_messages(_plan(store, prefs), prefs)
    assert overview.startswith("<b>🛒 Meal plan") and "3 meals × 2 servings" in overview
    assert '<a href="https://a.com/green-curry">Green Curry</a>' in meals
    assert "<b>Produce</b>" in shopping and "▫️ garlic:" in shopping and "Assumed in your pantry" in shopping
    assert "need" not in shopping  # buy quantities only, no recipe cross-references


def test_titles_are_html_escaped(store, prefs, pool):
    ev = _plan(store, prefs)
    ev.recipes[0].title = "Mac & <Cheese>"
    assert "Mac &amp; &lt;Cheese&gt;" in telegram_messages(ev, prefs)[1]


def test_pack_respects_limit_and_splits_long_blocks():
    assert _pack(["a" * 10, "b" * 10], limit=25) == ["a" * 10 + "\n\n" + "b" * 10]
    assert _pack(["a" * 10, "b" * 10], limit=15) == ["a" * 10, "b" * 10]
    long_block = "\n".join(["x" * 8] * 5)
    assert all(len(m) <= 20 for m in _pack([long_block], limit=20))
    assert "".join(_pack([long_block], limit=20)).count("x") == 40


def test_set_env_var_replaces_commented_and_appends(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=x\n# TELEGRAM_BOT_TOKEN=\n")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    set_env_var(env, "TELEGRAM_BOT_TOKEN", "tok")
    set_env_var(env, "TELEGRAM_CHAT_ID", "9")
    assert env.read_text() == "ANTHROPIC_API_KEY=x\nTELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=9\n"
    assert oct(env.stat().st_mode)[-3:] == "600"


def test_connect_telegram_saves_token_and_chat_id(tmp_path, monkeypatch):
    import grocery_agent.delivery as delivery
    from grocery_agent.cli import connect_telegram

    fake = FakeTelegram(updates=[{"message": {"chat": {"id": 555}}}])
    real_client = httpx.Client
    monkeypatch.setattr(delivery.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "123:secret")
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    assert connect_telegram(env) == 0
    assert "TELEGRAM_BOT_TOKEN=123:secret" in env.read_text()
    assert "TELEGRAM_CHAT_ID=555" in env.read_text()
    assert [m for m, _ in fake.calls] == ["getMe", "getUpdates", "sendMessage"]
