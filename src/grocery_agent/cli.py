"""Command line: init, plan, feedback, pool, prices."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import uuid
from datetime import date
from pathlib import Path

from .config import (ENV_TEMPLATE, SETTINGS_TEMPLATE, Band, Preferences, home_dir, load_preferences,
                     load_settings, save_preferences)
from .normalize import seed_store
from .store import IngredientFacts, Store


def open_store() -> Store:
    store = Store(home_dir() / "grocery.db")
    seed_store(store)
    return store


# ---- init -------------------------------------------------------------------
def _ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _ask_list(prompt: str, default: list[str]) -> list[str]:
    raw = _ask(prompt + " (comma-separated)", ", ".join(default) or "none")
    return [] if raw.lower() == "none" else [x.strip().lower() for x in raw.split(",") if x.strip()]


def _ask_band(prompt: str, band: Band) -> Band:
    raw = _ask(prompt + " as min-max (blank side = open)",
               f"{'' if band.min is None else f'{band.min:g}'}-{'' if band.max is None else f'{band.max:g}'}")
    lo, _, hi = raw.partition("-")
    return Band(min=float(lo) if lo.strip() else None, max=float(hi) if hi.strip() else None)


def cmd_init(args: argparse.Namespace) -> int:
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    prefs_path = home / "preferences.yaml"
    current = load_preferences(prefs_path) if prefs_path.exists() else Preferences()
    if args.defaults:
        prefs = current
    else:
        print("Let's set up your meal-planning preferences. Press Enter to keep the value in brackets.\n")
        mix_default = ", ".join(f"{c}:{w:g}" for c, w in current.cuisine_mix.items())
        mix_raw = _ask("Cuisine mix as cuisine:weight", mix_default)
        mix = {}
        for part in mix_raw.split(","):
            name, _, weight = part.partition(":")
            if name.strip():
                mix[name.strip().lower()] = float(weight or 1)
        prefs = Preferences(
            meals=int(_ask("Meals to plan per run", str(current.meals))),
            servings=int(_ask("Servings per meal", str(current.servings))),
            macros_per_serving={
                "calories": _ask_band("Calories per serving", current.macros_per_serving.get("calories", Band())),
                "protein_g": _ask_band("Protein grams per serving", current.macros_per_serving.get("protein_g", Band())),
                "carbs_g": _ask_band("Carb grams per serving", current.macros_per_serving.get("carbs_g", Band())),
                "fat_g": _ask_band("Fat grams per serving", current.macros_per_serving.get("fat_g", Band())),
            },
            cuisine_mix=mix,
            budget=(lambda v: float(v) if v.lower() != "none" else None)(
                _ask("Grocery budget per plan", "none" if current.budget is None else f"{current.budget:g}")),
            currency=_ask("Currency", current.currency),
            staples=_ask_list("Pantry staples you always have", current.staples),
            dislikes=_ask_list("Ingredients you dislike", current.dislikes),
            allergies=_ask_list("Allergies", current.allergies),
            adventurousness=_ask_band("Share of brand-new recipes, 0-1", current.adventurousness),
            max_total_time_min=int(_ask("Max total cooking time (minutes)", str(current.max_total_time_min or 60))),
            notes=(lambda v: "" if v.lower() == "none" else v)(
                _ask("Anything else the planner should know", current.notes or "none")),
        )
        prefs.macros_per_serving = {k: b for k, b in prefs.macros_per_serving.items() if b.min or b.max}
    save_preferences(prefs, prefs_path)
    for name, template in (("settings.toml", SETTINGS_TEMPLATE), (".env", ENV_TEMPLATE)):
        if not (home / name).exists():
            (home / name).write_text(template)
    open_store().close()
    print(f"\nSaved {prefs_path}\nSettings: {home / 'settings.toml'}\nAPI keys: {home / '.env'}")
    return 0


# ---- plan -------------------------------------------------------------------
def cmd_plan(args: argparse.Namespace) -> int:
    from .agent.planner import run_planner
    from .agent.tools import RunContext
    from .llm import UsageMeter, make_model
    from .normalize import LLMOracle, Normalizer
    from .render import render_plan
    from .scoring import Evaluator
    from .sourcing import HttpFetcher, make_search

    prefs = load_preferences()
    if args.meals:
        prefs.meals = args.meals
    settings = load_settings()
    store = open_store()
    meter = UsageMeter(settings.budgets.max_run_tokens)
    worker = LLMOracle(make_model(settings, "worker"), meter.config())
    ctx = RunContext(store=store, prefs=prefs, settings=settings, search=make_search(settings.search_provider),
                     fetcher=HttpFetcher(), normalizer=Normalizer(store, worker, prefs.currency), meter=meter)
    print(f"Planning {prefs.meals} meals with {settings.models.planner} …", file=sys.stderr)
    ids, notes = run_planner(ctx, make_model(settings, "planner"))
    if not ids:
        print("No plan could be made: the recipe pool is empty and sourcing found nothing.", file=sys.stderr)
        return 1
    ev = Evaluator(store, prefs).evaluate(ids)
    plan_id = uuid.uuid4().hex[:8]
    out = Path(args.output or f"meal-plan-{date.today().isoformat()}.md")
    out.write_text(render_plan(plan_id, ev, prefs, notes, usage=meter.by_model))
    store.record_plan(plan_id, ids, str(out.resolve()), ev.summary())
    for line in ctx.log:
        print("  " + line, file=sys.stderr)
    print(f"Wrote {out} · {ev.total_cost:.2f} {prefs.currency} · {meter.total_tokens:,} tokens", file=sys.stderr)
    return 0


# ---- feedback / pool / prices ------------------------------------------------
def cmd_feedback(args: argparse.Namespace) -> int:
    from .render import parse_feedback

    rows = parse_feedback(Path(args.file).read_text())
    store = open_store()
    for plan_id, rid, cooked, liked in rows:
        store.record_feedback(plan_id, rid, cooked, liked)
    print(f"Recorded feedback for {len(rows)} recipes.")
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    store = open_store()
    recipes = store.all_recipes()
    print(f"{len(recipes)} recipes in the pool")
    for r in recipes[-args.limit:]:
        print(f"  {r.id}  {r.cuisine or '-':<12} {r.title}  ({r.site})")
    return 0


PRICE_FIELDS = ["canonical", "category", "package_g", "package_price", "currency", "each_g", "density",
                "kcal", "protein", "carbs", "fat", "source"]


def cmd_prices(args: argparse.Namespace) -> int:
    store = open_store()
    if args.action == "export":
        with open(args.file, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=PRICE_FIELDS)
            w.writeheader()
            for f in store.all_facts():
                w.writerow({k: getattr(f, k) for k in PRICE_FIELDS})
        print(f"Wrote {args.file}. Edit prices, then `grocery-agent prices import {args.file}`.")
    else:
        n = 0
        with open(args.file, newline="") as fh:
            for row in csv.DictReader(fh):
                existing = store.facts(row["canonical"]) or IngredientFacts(canonical=row["canonical"])
                changed = False
                for k in PRICE_FIELDS[2:-1]:
                    if k == "currency" or row.get(k) in (None, ""):
                        continue
                    new = float(row[k])
                    if getattr(existing, k) != new:
                        setattr(existing, k, new)
                        changed = True
                if changed:
                    existing.source = "user"
                    store.upsert_facts(existing, overwrite_user=True)
                    n += 1
        print(f"Updated {n} ingredients; they won't be overwritten by estimates.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grocery-agent", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="set up preferences, settings and the key file")
    p.add_argument("--defaults", action="store_true", help="write defaults without asking")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("plan", help="plan meals and write a markdown grocery plan")
    p.add_argument("-o", "--output")
    p.add_argument("--meals", type=int)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("feedback", help="record cooked/liked ticks from a plan file")
    p.add_argument("file")
    p.set_defaults(fn=cmd_feedback)
    p = sub.add_parser("pool", help="list recipes in the local pool")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_pool)
    p = sub.add_parser("prices", help="export or import the price/ingredient table as CSV")
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("file", nargs="?", default="prices.csv")
    p.set_defaults(fn=cmd_prices)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
