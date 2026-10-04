"""Markdown output for a plan, and parsing feedback back out of it."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .config import Preferences
from .llm import RunCost
from .scoring import PlanEval

CATEGORY_ORDER = ["produce", "meat", "seafood", "protein", "dairy", "bakery", "grain", "legume", "canned",
                  "frozen", "condiment", "spice", "other"]


def _qty(grams: float) -> str:
    return f"{grams / 1000:.2f} kg" if grams >= 1000 else f"{grams:.0f} g"


def pantry_summary(ev: PlanEval) -> str:
    """'salt, olive oil; cumin, turmeric (indian spices)': exact matches first, then grouped by pantry entry."""
    plain, grouped = [], defaultdict(list)
    for name in ev.pantry:
        staple = ev.pantry_sources.get(name)
        if not staple or staple.lower() in name.lower():
            plain.append(name)
        else:
            grouped[staple].append(name)
    parts = [", ".join(plain)] if plain else []
    parts += [f"{', '.join(names)} ({staple})" for staple, names in grouped.items()]
    return "; ".join(parts)


def check_note(ev: PlanEval, cur: str) -> str | None:
    n = len(ev.pantry_check)
    if not n:
        return None
    return f"{ev.total_if_buying_checks:.2f} {cur} if you need the {n} \"check your pantry\" item{'s' if n > 1 else ''}"


def price_note(ev: PlanEval) -> str | None:
    """'Kroger Downtown, 100 E Court St; 6 items estimated' when store prices are on."""
    if not ev.price_store:
        return None
    n = ev.estimated_count
    return ev.price_store + (f"; {n} item{'s' if n != 1 else ''} estimated" if n else "")


def price_mark(ev: PlanEval, line) -> str:
    """' ~' after an estimated price, when the rest come from a store."""
    return " ~" if ev.price_store and line.cost is not None and line.price_source == "estimate" else ""


def render_plan(plan_id: str, ev: PlanEval, prefs: Preferences, notes: str = "", cost: RunCost | None = None) -> str:
    cur = prefs.currency
    out = [f"# Meal plan · {date.today().isoformat()}", ""]
    where = price_note(ev)
    per_serving = f"{ev.total_cost / max(1, ev.servings):.2f} {cur}/serving"
    groceries = (f"groceries **{ev.total_cost:.2f} {cur}** ({where}) · {per_serving}" if where
                 else f"estimated groceries **{ev.total_cost:.2f} {cur}** ({per_serving})")
    out.append(f"{len(ev.recipes)} meals × {prefs.servings} servings · {groceries} · new recipes {ev.new_fraction:.0%}"
               f" (target {prefs.adventurousness.describe()})")
    if note := check_note(ev, cur):
        out.append(f"({note})")
    if prefs.budget is not None:
        out.append(f"Budget: {prefs.budget:.2f} {cur}")
    if cost is not None:
        out.append(f"Agent cost this run: {cost.describe()}")
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
            line_cost = f"{g.cost:.2f}" if g.cost is not None else "?"
            out.append(f"- [ ] {g.canonical}: need {_qty(g.grams)}, buy {buy} · {line_cost} {cur}{price_mark(ev, g)}"
                       f" _(for {', '.join(g.used_by)})_")
        out.append("")
    if ev.pantry_check:
        out += ["**Check your pantry** (not in the total; buy only if you're out)", ""]
        for g in ev.pantry_check:
            buy = f"buy {g.packages} × {_qty(g.package_g)} · {g.cost:.2f} {cur}" if g.packages and g.package_g else "buy ?"
            need = f"need {_qty(g.grams)}, " if g.grams else ""
            out.append(f"- [ ] {g.canonical}: {need}{buy} _({g.pantry_hint}? · for {', '.join(g.used_by)})_")
        out.append("")
    if ev.unpriced:
        out += ["**Check these by hand** (couldn't quantify or price)", ""]
        out += [f"- [ ] {u}" for u in ev.unpriced] + [""]
    if ev.pantry:
        out += [f"**Assumed in your pantry:** {pantry_summary(ev)}", ""]
    if ev.price_store:
        out += [f"Prices are from {ev.price_store} (sale prices where lower); ~ marks an estimate for items it "
                "couldn't match.", ""]
    else:
        out += ["Prices are estimates. Edit them with `grocery-agent prices export` / `import`.", ""]

    out += ["## Feedback", "",
            f"Tick what you cooked and liked, then run `grocery-agent feedback <this file>`.", ""]
    for r in ev.recipes:
        out.append(f"- [ ] cooked [ ] liked · {r.title} <!-- plan:{plan_id} recipe:{r.id} -->")
    out.append("")
    if cost is not None and cost.by_model:
        parts = [f"{m}: {d['tokens']:,} tokens" + ("" if d["usd"] is None else f" (${d['usd']:.3f})")
                 for m, d in cost.by_model.items()]
        out += [f"<sub>Agent cost by model: {'; '.join(parts)}</sub>", ""]
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
