"""Delivering a finished plan: always a markdown file, optionally Telegram.

Telegram credentials live in ``.env`` (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``),
never in preferences.yaml.
"""

from __future__ import annotations

import os
from datetime import date
from html import escape as esc
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

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        """New updates after `offset`. With a timeout, waits (long poll) for up to that many seconds."""
        data = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            data["offset"] = offset
        return self._call("getUpdates", data=data)

    def send_message(self, chat_id: str, text: str, html: bool = False) -> None:
        data = {"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS], "disable_web_page_preview": "true"}
        if html:
            data["parse_mode"] = "HTML"
        self._call("sendMessage", data=data)

    def send_document(self, chat_id: str, path: Path, caption: str = "") -> None:
        with path.open("rb") as fh:
            self._call("sendDocument", data={"chat_id": chat_id, "caption": caption[:1024]},
                       files={"document": (path.name, fh, "text/markdown")})


def _pack(blocks: list[str], limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Join blocks into as few messages as fit Telegram's limit; oversized blocks split by line."""
    messages: list[str] = []
    current = ""

    def add(piece: str, sep: str) -> None:
        nonlocal current
        candidate = f"{current}{sep}{piece}" if current else piece
        if len(candidate) > limit and current:
            messages.append(current)
            current = piece
        else:
            current = candidate

    for block in blocks:
        if len(block) <= limit:
            add(block, "\n\n")
        else:
            for i, line in enumerate(block.split("\n")):
                add(line, "\n\n" if i == 0 else "\n")
    if current:
        messages.append(current)
    return messages


def telegram_messages(ev: PlanEval, prefs: Preferences, cost: RunCost | None = None) -> list[str]:
    """The plan as a few short, chat-friendly HTML messages: overview, meals, shopping list."""
    from .render import CATEGORY_ORDER, _qty, check_note, pantry_summary, price_mark, price_note

    cur = prefs.currency
    overview = [f"<b>🛒 Meal plan · {date.today():%a %d %b}</b>",
                f"{len(ev.recipes)} meals × {prefs.servings} servings",
                f"Groceries {'' if ev.price_store else '≈ '}<b>{ev.total_cost:.2f} {esc(cur)}</b>"
                f" ({ev.total_cost / max(1, ev.servings):.2f}/serving)",
                f"New recipes: {ev.new_fraction:.0%}"]
    if note := check_note(ev, cur):
        overview.insert(3, f"({esc(note)})")
    if where := price_note(ev):
        overview.insert(3, f"<i>{esc(where)}</i>")
    if cost is not None:
        overview.append(f"Agent cost: {esc(cost.describe())}")
    if ev.violations:
        overview += ["", "⚠️ <b>Not met</b>"] + [f"• {esc(v)}" for v in ev.violations[:6]]

    meals = ["<b>🍽 Meals</b>"]
    for i, r in enumerate(ev.recipes, 1):
        tag = " 🆕" if r.is_new else (" ❤️" if r.liked_before else "")
        meta = " · ".join(x for x in (r.cuisine, f"{r.total_time} min" if r.total_time else "") if x)
        meals.append(f'{i}. <a href="{esc(r.url)}">{esc(r.title)}</a>{tag}' + (f" · {esc(meta)}" if meta else ""))
        meals.append(f"    {r.macros['calories']:.0f} kcal · {r.macros['protein_g']:.0f} g protein")

    shopping = [["<b>🧺 Shopping list</b>"]]
    by_cat: dict[str, list] = {}
    for g in ev.grocery:
        by_cat.setdefault(g.category, []).append(g)
    for cat in sorted(by_cat, key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99):
        lines = [f"<b>{esc(cat.title())}</b>"]
        for g in by_cat[cat]:
            buy = f"{g.packages} × {_qty(g.package_g)}" if g.packages and g.package_g else _qty(g.grams)
            price = f" · {g.cost:.2f}{price_mark(ev, g)}" if g.cost is not None else ""
            lines.append(f"▫️ {esc(g.canonical)}: {buy}{price}")
        shopping.append(lines)
    if ev.pantry_check:
        lines = ["<b>Check your pantry</b>"]
        for g in ev.pantry_check:
            buy = f"{g.packages} × {_qty(g.package_g)}" if g.packages and g.package_g else (_qty(g.grams) if g.grams else "")
            price = f" · {g.cost:.2f}" if g.cost is not None else ""
            lines.append(f"▫️ {esc(g.canonical)}" + (f": {buy}" if buy else "") + f"{price} ({esc(g.pantry_hint or '')}?)")
        shopping.append(lines)
    if ev.unpriced:
        shopping.append(["<b>Check by hand</b>"] + [f"▫️ {esc(u)}" for u in ev.unpriced])
    if ev.pantry:
        shopping.append([f"<i>Assumed in your pantry: {esc(pantry_summary(ev))}</i>"])

    return ["\n".join(overview), "\n".join(meals)] + _pack(["\n".join(b) for b in shopping])


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
        for text in telegram_messages(ev, prefs, cost):
            tg.send_message(chat_id, text, html=True)
        tg.send_document(chat_id, path, caption="Full plan with feedback checkboxes. After the week, tick what "
                                                "you cooked and liked and run grocery-agent feedback.")
        return "sent to Telegram"
    raise DeliveryError(f"unknown delivery method {prefs.delivery!r}")
