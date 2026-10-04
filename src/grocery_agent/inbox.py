"""Free-form feedback by replying to the bot in Telegram.

There is no server: new messages are read when the app runs (first step of
`plan`, `feedback`, or a running `listen`). The worker model turns each
message into a list of concrete changes; code applies them to the same places
`init`, `pantry` and `feedback` use, and the bot replies with what changed.

Changes to the diet, allergies or budget are held until you reply "yes".
"undo" reverses the last applied message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .config import Preferences, load_preferences, save_preferences
from .store import Store

Kind = Literal["recipe", "dislike_add", "dislike_remove", "pantry_add", "pantry_remove", "allergy_add",
               "allergy_remove", "diet_set", "budget_set", "meals_set", "servings_set", "note"]
NEEDS_OK = {"allergy_add", "allergy_remove", "diet_set", "budget_set"}
OFFSET_KEY = "telegram_offset"

YES = {"yes", "y", "yep", "ok", "okay", "sure", "confirm", "do it", "👍"}
NO = {"no", "n", "nope", "cancel", "skip", "don't", "dont"}
UNDO = {"undo", "revert", "undo that"}


class Action(BaseModel):
    kind: Kind
    recipe: str | None = Field(None, description="For kind=recipe: the recipe title from the list, verbatim.")
    cooked: bool | None = Field(None, description="For kind=recipe: did they cook it?")
    liked: bool | None = Field(None, description="For kind=recipe: did they like it? null if not said.")
    value: str | None = Field(None, description="The item, diet, number or note text, e.g. 'mushrooms', "
                                                 "'vegan' or 'none', '120', '4'.")


class Parsed(BaseModel):
    actions: list[Action]


class FeedbackOracle(Protocol):
    def parse(self, text: str, recipes: list[str], prefs: Preferences) -> list[Action]: ...


PARSE_PROMPT = """A household plans meals with an app and just sent it this message:

"{text}"

Recent recipes they were given (use these titles verbatim):
{recipes}

Current settings: diet {diet}; allergies {allergies}; dislikes {dislikes}; pantry {pantry};
budget {budget}; {meals} meals x {servings} servings.

Turn the message into changes. Kinds:
- recipe: feedback on one recipe above (cooked true/false, liked true/false/null)
- dislike_add / dislike_remove: an ingredient to avoid, or no longer avoid
- pantry_add / pantry_remove: something they now always have, or have run out of
- allergy_add / allergy_remove, diet_set (vegetarian, vegan, pescatarian, dairy-free,
  gluten-free, comma-separated, or "none"), budget_set (a number, or "none")
- meals_set / servings_set: a number
- note: anything else the planner should keep in mind (in a few words)
Only include what the message actually says."""


class LLMFeedbackOracle:
    def __init__(self, model, run_config: dict | None = None):
        self.model = model.with_structured_output(Parsed)
        self.run_config = run_config or {}

    def parse(self, text: str, recipes: list[str], prefs: Preferences) -> list[Action]:
        prompt = PARSE_PROMPT.format(
            text=text, recipes="\n".join(f"- {t}" for t in recipes) or "(none yet)",
            diet=", ".join(prefs.diet) or "none", allergies=", ".join(prefs.allergies) or "none",
            dislikes=", ".join(prefs.dislikes) or "none", pantry=", ".join(prefs.staples) or "none",
            budget="none" if prefs.budget is None else f"{prefs.budget:g} {prefs.currency}",
            meals=prefs.meals, servings=prefs.servings)
        return self.model.invoke(prompt, config=self.run_config).actions


def recent_recipes(store: Store, plans: int = 3) -> dict[str, tuple[str, str]]:
    """title -> (plan_id, recipe_id), newest plan first wins."""
    out: dict[str, tuple[str, str]] = {}
    for p in store.recent_plans(limit=plans):
        for rid in json.loads(p["recipe_ids"]):
            r = store.get_recipe(rid)
            if r and r.title not in out:
                out[r.title] = (p["id"], rid)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s']", "", text.strip().lower())


@dataclass
class Outcome:
    lines: list[str] = field(default_factory=list)      # what changed, one per line
    pending: list[str] = field(default_factory=list)    # waiting for "yes"
    skipped: list[str] = field(default_factory=list)    # not understood or invalid

    def message(self) -> str:
        parts = []
        if self.lines:
            parts.append("Got it:\n" + "\n".join(self.lines))
        if self.pending:
            parts.append("Needs your OK:\n" + "\n".join(self.pending) + '\nReply "yes" to apply or "no" to skip.')
        if self.skipped:
            parts.append("Couldn't use: " + "; ".join(self.skipped))
        if self.lines:
            parts.append('Reply "undo" to reverse this.')
        return "\n\n".join(parts) or "Nothing to change in that. Tell me how a meal went, or what to avoid or stock."


class Inbox:
    def __init__(self, store: Store, prefs_path: Path, oracle: FeedbackOracle | None):
        self.store = store
        self.prefs_path = prefs_path
        self.oracle = oracle

    # ---- one message -------------------------------------------------------
    def handle(self, text: str) -> str:
        word = _norm(text)
        if word in YES:
            return self._confirm(True)
        if word in NO:
            return self._confirm(False)
        if word in UNDO:
            return self._undo()
        if self.oracle is None:
            return "I can't read free text right now (no model configured)."
        prefs = load_preferences(self.prefs_path)
        recipes = recent_recipes(self.store)
        actions = self.oracle.parse(text, list(recipes), prefs)
        now = [a for a in actions if a.kind not in NEEDS_OK]
        later = [a for a in actions if a.kind in NEEDS_OK]
        out = Outcome()
        if now:
            snapshot = self._snapshot(prefs)
            out.lines, out.skipped, touched = self._apply(now, prefs, recipes)
            snapshot["feedback"] = [f for f in snapshot["feedback"] if tuple(f["key"]) in touched]
            if out.lines:
                save_preferences(prefs, self.prefs_path)
                self.store.add_inbox(text, [a.model_dump() for a in now], snapshot, "applied")
        if later:
            prior = self.store.latest_inbox("pending")
            if prior:
                self.store.set_inbox(prior["id"], "expired")  # a newer request replaces it
            self.store.add_inbox(text, [a.model_dump() for a in later], None, "pending")
            out.pending = [describe(a) for a in later]
        return out.message()

    def _confirm(self, yes: bool) -> str:
        entry = self.store.latest_inbox("pending")
        if entry is None:
            return "Nothing is waiting for your OK."
        if not yes:
            self.store.set_inbox(entry["id"], "declined")
            return "OK, left as is."
        prefs = load_preferences(self.prefs_path)
        snapshot = self._snapshot(prefs)
        actions = [Action.model_validate(a) for a in json.loads(entry["actions"])]
        lines, skipped, _ = self._apply(actions, prefs, {})
        save_preferences(prefs, self.prefs_path)
        snapshot["feedback"] = []
        self.store.set_inbox(entry["id"], "applied", snapshot)
        return Outcome(lines=lines, skipped=skipped).message()

    def _undo(self) -> str:
        entry = self.store.latest_inbox("applied")
        if entry is None:
            return "Nothing to undo."
        snap = json.loads(entry["undo"] or "{}")
        if "prefs" in snap:
            save_preferences(Preferences.model_validate(snap["prefs"]), self.prefs_path)
        for f in snap.get("feedback", []):
            plan_id, rid = f["key"]
            if f["row"] is None:
                self.store.delete_feedback(plan_id, rid)
            else:
                self.store.record_feedback(plan_id, rid, bool(f["row"]["cooked"]), bool(f["row"]["liked"]))
        self.store.set_inbox(entry["id"], "undone")
        return f'Undone: "{entry["text"][:80]}"'

    def _snapshot(self, prefs: Preferences) -> dict:
        feedback = []
        for _title, (plan_id, rid) in recent_recipes(self.store).items():
            row = self.store.feedback_row(plan_id, rid)
            feedback.append({"key": [plan_id, rid], "row": None if row is None else
                             {"cooked": row["cooked"], "liked": row["liked"]}})
        return {"prefs": prefs.model_dump(mode="json"), "feedback": feedback}

    def _apply(self, actions: list[Action], prefs: Preferences,
               recipes: dict[str, tuple[str, str]]) -> tuple[list[str], list[str], set]:
        from .diet import normalize_diet

        lines, skipped, touched = [], [], set()
        lower = {t.lower(): t for t in recipes}

        def add(items: list[str], v: str) -> bool:
            if v.lower() in (i.lower() for i in items):
                return False
            items.append(v)
            return True

        def remove(items: list[str], v: str) -> list[str]:
            return [i for i in items if i.lower() != v.lower()]

        for a in actions:
            v = (a.value or "").strip().lower()
            try:
                if a.kind == "recipe":
                    title = lower.get((a.recipe or "").strip().lower())
                    if title is None:
                        skipped.append(f"no recent recipe called '{a.recipe}'")
                        continue
                    plan_id, rid = recipes[title]
                    old = self.store.feedback_row(plan_id, rid)
                    cooked = a.cooked if a.cooked is not None else (bool(old["cooked"]) if old else True)
                    liked = a.liked if a.liked is not None else (bool(old["liked"]) if old else False)
                    self.store.record_feedback(plan_id, rid, cooked, liked)
                    touched.add((plan_id, rid))
                    if a.liked is True:
                        icon, how = "❤️", "liked"
                    elif a.liked is False:
                        icon, how = "👎", "not liked"
                    else:
                        icon, how = "🍳", "noted"
                    lines.append(f"{icon} {title}: " + ("cooked, " if cooked else "not cooked, ") + how)
                elif not v:
                    skipped.append(f"{a.kind.replace('_', ' ')} with no value")
                elif a.kind == "dislike_add":
                    if add(prefs.dislikes, v):
                        lines.append(f"🚫 Dislikes: added {v}")
                elif a.kind == "dislike_remove":
                    prefs.dislikes = remove(prefs.dislikes, v)
                    lines.append(f"🚫 Dislikes: removed {v}")
                elif a.kind == "pantry_add":
                    if add(prefs.staples, v):
                        lines.append(f"🧺 Pantry: added {v}")
                elif a.kind == "pantry_remove":
                    prefs.staples = remove(prefs.staples, v)
                    lines.append(f"🧺 Pantry: removed {v}")
                elif a.kind == "allergy_add":
                    if add(prefs.allergies, v):
                        lines.append(f"⚠️ Allergies: added {v}")
                elif a.kind == "allergy_remove":
                    prefs.allergies = remove(prefs.allergies, v)
                    lines.append(f"⚠️ Allergies: removed {v}")
                elif a.kind == "diet_set":
                    prefs.diet = [] if v == "none" else list(dict.fromkeys(
                        normalize_diet(d) for d in v.split(",") if d.strip()))
                    lines.append(f"🥗 Diet: {', '.join(prefs.diet) or 'none'}")
                elif a.kind == "budget_set":
                    prefs.budget = None if v == "none" else float(re.sub(r"[^\d.]", "", v))
                    lines.append(f"💰 Budget: {'none' if prefs.budget is None else f'{prefs.budget:g} {prefs.currency}'}")
                elif a.kind == "meals_set":
                    prefs.meals = int(float(v))
                    lines.append(f"🍽 Meals per plan: {prefs.meals}")
                elif a.kind == "servings_set":
                    prefs.servings = int(float(v))
                    lines.append(f"🍽 Servings per meal: {prefs.servings}")
                elif a.kind == "note":
                    note = (a.value or "").strip()
                    prefs.notes = f"{prefs.notes}; {note}".strip("; ") if prefs.notes else note
                    lines.append(f"📝 Noted for the planner: {note}")
            except ValueError as exc:
                skipped.append(str(exc).split("\n")[0])
        Preferences.model_validate(prefs.model_dump())  # fail loudly rather than save something invalid
        return lines, skipped, touched


def describe(a: Action) -> str:
    v = (a.value or "").strip()
    return {
        "diet_set": f"🥗 Diet: set to {v}", "budget_set": f"💰 Budget: set to {v}",
        "allergy_add": f"⚠️ Allergies: add {v}", "allergy_remove": f"⚠️ Allergies: remove {v}",
    }.get(a.kind, f"{a.kind}: {v}")


def sync(inbox: Inbox, tg, chat_id: str, timeout: int = 0) -> int:
    """Read new messages from your chat, act on each, reply to each. Returns how many were handled.
    Messages from any other chat are ignored."""
    offset = inbox.store.get_meta(OFFSET_KEY)
    updates = tg.get_updates(int(offset) if offset else None, timeout=timeout)
    handled = 0
    for u in updates:
        inbox.store.set_meta(OFFSET_KEY, str(u["update_id"] + 1))  # never reprocess, even on error
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if str(msg.get("chat", {}).get("id")) != str(chat_id) or not text or text.startswith("/start"):
            continue
        try:
            reply = inbox.handle(text)
        except Exception as exc:  # one bad message must not stop a plan run
            reply = f"Sorry, I couldn't process that ({exc.__class__.__name__}). Nothing was changed."
        tg.send_message(chat_id, reply)
        handled += 1
    return handled
