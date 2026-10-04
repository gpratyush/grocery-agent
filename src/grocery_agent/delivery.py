"""Delivering a finished plan: always a markdown file, optionally Telegram.

Telegram credentials live in ``.env`` (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``),
never in preferences.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from .config import Preferences
from .llm import RunCost
from .scoring import PlanEval

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096


class DeliveryError(RuntimeError):
    pass


def set_env_var(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a .env file, replacing an existing line or appending one."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        stripped = line.lstrip("# ").strip()
        if stripped.startswith(f"{key}=") and not done:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    os.environ[key] = value


class Telegram:
    def __init__(self, token: str, client: httpx.Client | None = None):
        if not token:
            raise DeliveryError("TELEGRAM_BOT_TOKEN is not set (add it to ~/.grocery-agent/.env)")
        self.base = f"{TELEGRAM_API}/bot{token}"
        self.client = client or httpx.Client(timeout=30)

    def _call(self, method: str, **kwargs) -> dict:
        try:
            resp = self.client.post(f"{self.base}/{method}", **kwargs)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DeliveryError(f"Telegram {method} failed: {exc}") from exc
        if not data.get("ok"):
            raise DeliveryError(f"Telegram {method} failed: {data.get('description', resp.status_code)}")
        return data["result"]

    def bot_name(self) -> str:
        return self._call("getMe").get("username", "")

    def latest_chat_id(self) -> str | None:
        """Chat id of the most recent message sent to the bot (used during setup)."""
        updates = self._call("getUpdates")
        for update in reversed(updates):
            msg = update.get("message") or update.get("edited_message") or {}
            chat = msg.get("chat", {})
            if "id" in chat:
                return str(chat["id"])
        return None

    def send_message(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", data={"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS],
                                        "disable_web_page_preview": "true"})

    def send_document(self, chat_id: str, path: Path, caption: str = "") -> None:
        with path.open("rb") as fh:
            self._call("sendDocument", data={"chat_id": chat_id, "caption": caption[:1024]},
                       files={"document": (path.name, fh, "text/markdown")})


def plan_summary(ev: PlanEval, prefs: Preferences, cost: RunCost | None = None) -> str:
    """Short plain-text version of the plan for a chat message."""
    lines = [f"🛒 Meal plan: {len(ev.recipes)} meals × {prefs.servings} servings",
             f"Estimated groceries: {ev.total_cost:.2f} {prefs.currency}"]
    if cost is not None:
        lines.append(f"Agent cost: {cost.describe()}")
    lines.append("")
    for i, r in enumerate(ev.recipes, 1):
        lines.append(f"{i}. {r.title} ({r.cuisine or '-'}, {r.macros['protein_g']:.0f} g protein)\n   {r.url}")
    if ev.violations:
        lines += ["", "Not met: " + "; ".join(ev.violations[:5])]
    lines += ["", "Full grocery list in the attached file."]
    return "\n".join(lines)


def deliver(path: Path, ev: PlanEval, prefs: Preferences, client: httpx.Client | None = None,
            cost: RunCost | None = None) -> str:
    """Send the plan by the configured method. Returns a one-line description."""
    if prefs.delivery == "file":
        return f"saved to {path}"
    if prefs.delivery == "telegram":
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not chat_id:
            raise DeliveryError("TELEGRAM_CHAT_ID is not set; run `grocery-agent init` to connect Telegram")
        tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""), client)
        tg.send_message(chat_id, plan_summary(ev, prefs, cost))
        tg.send_document(chat_id, path)
        return "sent to Telegram"
    raise DeliveryError(f"unknown delivery method {prefs.delivery!r}")
