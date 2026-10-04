"""Markdown output for a plan, and parsing feedback back out of it."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .config import Preferences
from .scoring import PlanEval

CATEGORY_ORDER = ["produce", "meat", "seafood", "protein", "dairy", "bakery", "grain", "legume", "canned",
                  "frozen", "condiment", "spice", "other"]


def _qty(grams: float) -> str:
    return f"{grams / 1000:.2f} kg" if grams >= 1000 else f"{grams:.0f} g"


def render_plan(plan_id: str, ev: PlanEval, prefs: Preferences, notes: str = "", usage: dict | None = None) -> str:
    cur = prefs.currency
    out = [f"# Meal plan · {date.today().isoformat()}", ""]
    out.append(f"{len(ev.recipes)} meals × {prefs.servings} servings · estimated groceries **{ev.total_cost:.2f} {cur}**"
               f" ({ev.total_cost / max(1, ev.servings):.2f} {cur}/serving) · new recipes {ev.new_fraction:.0%}"
               f" (target {prefs.adventurousness.describe()})")
    if prefs.budget is not None:
        out.append(f"Budget: {prefs.budget:.2f} {cur}")
    out.append("")
    if notes:
        out += ["## Planner notes", "", notes.strip(), ""]
    if ev.violations:
        out += ["## Unmet constraints", ""] + [f"- {v}" for v in ev.violations] + [""]

    out += ["## Meals", "", "| # | Recipe | Cuisine | Time | kcal | Protein | Carbs | Fat |",
            "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(ev.recipes, 1):
        m = r.macros
        tag = " 🆕" if r.is_new else (" ❤️" if r.liked_before else "")
        out.append(f"| {i} | [{r.title}]({r.url}){tag} | {r.cuisine or '-'} | {r.total_time or '-'} min | "
                   f"{m['calories']:.0f} | {m['protein_g']:.0f} g | {m['carbs_g']:.0f} g | {m['fat_g']:.0f} g |")
    bands = ", ".join(f"{k} {b.describe()}" for k, b in prefs.macros_per_serving.items())
    out += ["", f"Macros are per serving (targets: {bands}). Values come from the recipe page when it lists them,"
                " otherwise they are computed from ingredients.", ""]

    out += ["## Grocery list", ""]
    by_cat = defaultdict(list)
    for line in ev.grocery:
        by_cat[line.category].append(line)
    for cat in sorted(by_cat, key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99):
        out.append(f"**{cat.title()}**")
        out.append("")
        for g in by_cat[cat]:
            buy = f"{g.packages} × {_qty(g.package_g)}" if g.packages and g.package_g else "?"
            cost = f"{g.cost:.2f}" if g.cost is not None else "?"
            out.append(f"- [ ] {g.canonical}: need {_qty(g.grams)}, buy {buy} · {cost} {cur}"
                       f" _(for {', '.join(g.used_by)})_")
        out.append("")
    if ev.unpriced:
        out += ["**Check these by hand** (couldn't quantify or price)", ""]
        out += [f"- [ ] {u}" for u in ev.unpriced] + [""]
    if ev.pantry:
        out += [f"**Assumed in your pantry:** {', '.join(ev.pantry)}", ""]
    out += [f"Prices are estimates. Edit them with `grocery-agent prices export` / `import`.", ""]

    out += ["## Feedback", "",
            f"Tick what you cooked and liked, then run `grocery-agent feedback <this file>`.", ""]
    for r in ev.recipes:
        out.append(f"- [ ] cooked [ ] liked · {r.title} <!-- plan:{plan_id} recipe:{r.id} -->")
    out.append("")
    if usage:
        total = sum(u.get("total_tokens", 0) for u in usage.values())
        out += [f"<sub>Tokens used: {total:,} ({', '.join(usage)})</sub>", ""]
    return "\n".join(out)


FEEDBACK_LINE = re.compile(
    r"^- \[(?P<cooked>[ xX])\] cooked \[(?P<liked>[ xX])\] liked .*<!-- plan:(?P<plan>\S+) recipe:(?P<rid>\S+) -->"
)


def parse_feedback(markdown: str) -> list[tuple[str, str, bool, bool]]:
    """Return (plan_id, recipe_id, cooked, liked) for each feedback line."""
    rows = []
    for line in markdown.splitlines():
        m = FEEDBACK_LINE.match(line.strip())
        if m:
            rows.append((m["plan"], m["rid"], m["cooked"] != " ", m["liked"] != " "))
    return rows
