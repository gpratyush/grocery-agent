"""Command line: init, plan, feedback, history, pool, pantry, prices, connect-telegram, connect-kroger."""

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


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    while True:
        answer = _ask(f"{prompt} ({'/'.join(choices)})", default).lower()
        if answer in choices:
            return answer
        print(f"Please answer one of: {', '.join(choices)}")


def connect_telegram(env_path: Path) -> int:
    """Interactive Telegram setup: bot token (hidden input) and chat id discovery."""
    import getpass
    import os

    from .config import load_env_file
    from .delivery import DeliveryError, Telegram, set_env_var

    load_env_file(env_path)
    print("\nTelegram setup")
    print("1. In Telegram, message @BotFather, send /newbot and follow the steps.")
    print("2. Copy the bot token it gives you and paste it below (input is hidden; it's saved only to "
          f"{env_path}).")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    entered = getpass.getpass("Bot token" + (" [keep existing]" if token else "") + ": ").strip()
    token = entered or token
    if not token:
        print("No token entered.")
        return 1
    try:
        tg = Telegram(token)
        name = tg.bot_name()
    except DeliveryError as exc:
        print(f"That token didn't work: {exc}")
        return 1
    set_env_var(env_path, "TELEGRAM_BOT_TOKEN", token)
    input(f"3. Open https://t.me/{name}, press Start (or send any message), then press Enter here.")
    try:
        chat_id = tg.latest_chat_id()
    except DeliveryError as exc:
        print(f"Couldn't read messages to the bot: {exc}")
        return 1
    if not chat_id:
        print("No message to the bot found yet. Send it a message and run `grocery-agent connect-telegram` again.")
        return 1
    set_env_var(env_path, "TELEGRAM_CHAT_ID", chat_id)
    tg.send_message(chat_id, "✅ grocery-agent is connected. Your meal plans will arrive here.")
    print("Connected. Check Telegram for a test message.")
    return 0


def cmd_connect_telegram(args: argparse.Namespace) -> int:
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    code = connect_telegram(home / ".env")
    prefs_path = home / "preferences.yaml"
    if code == 0 and prefs_path.exists():
        prefs = load_preferences(prefs_path)
        if prefs.delivery != "telegram":
            prefs.delivery = "telegram"
            save_preferences(prefs, prefs_path)
            print("Delivery set to telegram in preferences.yaml.")
    return code


def connect_kroger(env_path: Path, prefs: Preferences) -> int:
    """Interactive Kroger setup: API keys (hidden input), zip code, and which nearby store to price at."""
    import getpass
    import os

    from .config import load_env_file
    from .delivery import set_env_var
    from .kroger import Kroger, KrogerError

    load_env_file(env_path)
    print("\nKroger prices")
    print("1. Create a free account at https://developer.kroger.com, register an application "
          "(Production environment, scope product.compact) and copy its client id and secret.")
    print(f"2. Paste them below (input is hidden; they're saved only to {env_path}).")
    keys = {}
    for key, label in (("KROGER_CLIENT_ID", "Client id"), ("KROGER_CLIENT_SECRET", "Client secret")):
        have = os.environ.get(key, "")
        keys[key] = getpass.getpass(label + (" [keep existing]" if have else "") + ": ").strip() or have
        if not keys[key]:
            print(f"No {label.lower()} entered.")
            return 1
    api = Kroger(keys["KROGER_CLIENT_ID"], keys["KROGER_CLIENT_SECRET"])
    prefs.zip_code = _ask("3. Your zip code", prefs.zip_code or "")
    try:
        stores = api.locations(prefs.zip_code)
    except KrogerError as exc:
        print(f"That didn't work: {exc}")
        return 1
    if not stores:
        print(f"No Kroger-family stores found near {prefs.zip_code}.")
        return 1
    for key, value in keys.items():
        set_env_var(env_path, key, value)
    for i, st in enumerate(stores, 1):
        print(f"  {i}. {st['name']}, {st['address']}")
    choice = _ask_choice("4. Which store", [str(i) for i in range(1, len(stores) + 1)], "1")
    st = stores[int(choice) - 1]
    prefs.price_source = "kroger"
    prefs.kroger_location_id = st["id"]
    prefs.kroger_store = f"{st['name']}, {st['address']}"
    print(f"Groceries will be priced at {prefs.kroger_store}. Items it doesn't stock keep estimated prices.")
    return 0


def _kroger_connected(prefs: Preferences) -> bool:
    import os

    return bool(prefs.kroger_location_id and os.environ.get("KROGER_CLIENT_ID")
                and os.environ.get("KROGER_CLIENT_SECRET"))


def cmd_connect_kroger(args: argparse.Namespace) -> int:
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    prefs_path = home / "preferences.yaml"
    prefs = load_preferences(prefs_path) if prefs_path.exists() else Preferences()
    code = connect_kroger(home / ".env", prefs)
    if code == 0:
        save_preferences(prefs, prefs_path)
    return code


def make_prices(store: Store, prefs: Preferences):
    """The store price source for a plan run, or None (estimates only) with a note on why."""
    import os

    if prefs.price_source != "kroger":
        return None
    if not _kroger_connected(prefs):
        print("Kroger prices are on but not set up; using estimates. Run `grocery-agent connect-kroger`.",
              file=sys.stderr)
        return None
    if prefs.currency.upper() != "USD":
        print(f"Kroger prices are in USD but your currency is {prefs.currency}; using estimates.", file=sys.stderr)
        return None
    from .kroger import Kroger, KrogerPrices

    return KrogerPrices(Kroger(os.environ["KROGER_CLIENT_ID"], os.environ["KROGER_CLIENT_SECRET"]), store,
                        prefs.kroger_location_id, prefs.kroger_store or "")


def _yes(prompt: str, default: bool = False) -> bool:
    answer = _ask(prompt + (" (Y/n)" if default else " (y/N)"), "y" if default else "n").lower()
    return answer.startswith("y")


def _parse_mix(raw: str) -> dict[str, float]:
    mix = {}
    for part in raw.split(","):
        name, _, weight = part.partition(":")
        if name.strip():
            mix[name.strip().lower()] = float(weight or 1)
    return mix


def _ask_meals(p: Preferences) -> None:
    p.meals = int(_ask("Meals to plan per run", str(p.meals)))
    p.servings = int(_ask("Servings per meal", str(p.servings)))


def _ask_macros(p: Preferences) -> None:
    labels = {"calories": "Calories", "protein_g": "Protein grams", "carbs_g": "Carb grams", "fat_g": "Fat grams"}
    bands = {k: _ask_band(f"{label} per serving", p.macros_per_serving.get(k, Band())) for k, label in labels.items()}
    p.macros_per_serving = {k: b for k, b in bands.items() if b.min is not None or b.max is not None}


def _ask_cuisines(p: Preferences) -> None:
    p.cuisine_mix = _parse_mix(_ask("Cuisine mix as cuisine:weight",
                                    ", ".join(f"{c}:{w:g}" for c, w in p.cuisine_mix.items())))


def _ask_budget(p: Preferences) -> None:
    raw = _ask("Grocery budget per plan", "none" if p.budget is None else f"{p.budget:g}")
    p.budget = None if raw.lower() == "none" else float(raw)
    p.currency = _ask("Currency", p.currency)


def _ask_diet(p: Preferences) -> None:
    from .diet import DIETS, normalize_diet

    while True:
        chosen = _ask_list(f"Diet, never broken ({', '.join(DIETS)})", p.diet)
        try:
            p.diet = list(dict.fromkeys(normalize_diet(d) for d in chosen))
            return
        except ValueError as exc:
            print(exc)


def _ask_pantry(p: Preferences) -> None:
    p.staples = _ask_list("Pantry staples you always have (broad ones like 'indian spices' work)", p.staples)
    p.dislikes = _ask_list("Ingredients you dislike", p.dislikes)
    p.allergies = _ask_list("Allergies", p.allergies)


def _ask_swaps(p: Preferences) -> None:
    print("  Swapping similar ingredients across recipes (chicken breast for thigh, shallot for the onion\n"
          "  you already have) uses up packages and lowers the bill. Dishes keep their namesake ingredient.")
    p.substitutions.level = _ask_choice("How far may swaps go", ["off", "same", "close", "liberal"],
                                        p.substitutions.level)


def _ask_style(p: Preferences) -> None:
    p.adventurousness = _ask_band("Share of brand-new recipes, 0-1", p.adventurousness)
    p.max_total_time_min = int(_ask("Max total cooking time (minutes)", str(p.max_total_time_min or 60)))
    raw = _ask("Anything else the planner should know", p.notes or "none")
    p.notes = "" if raw.lower() == "none" else raw


def _ask_prices(p: Preferences) -> None:
    p.price_source = _ask_choice("Price groceries with", ["estimate", "kroger"], p.price_source)


def _ask_delivery(p: Preferences) -> None:
    p.delivery = _ask_choice("Where should finished plans go", ["file", "telegram"], p.delivery)


def _fmt_list(items: list[str]) -> str:
    return ", ".join(items) if items else "none"


PREF_SECTIONS = [
    ("Meals", lambda p: f"{p.meals} meals × {p.servings} servings", _ask_meals),
    ("Macros", lambda p: ", ".join(f"{k} {b.describe()}" for k, b in p.macros_per_serving.items()) or "no targets",
     _ask_macros),
    ("Cuisines", lambda p: ", ".join(f"{c}:{w:g}" for c, w in p.cuisine_mix.items()), _ask_cuisines),
    ("Budget", lambda p: ("no budget" if p.budget is None else f"{p.budget:g}") + f" {p.currency}", _ask_budget),
    ("Diet", lambda p: _fmt_list(p.diet), _ask_diet),
    ("Pantry and restrictions", lambda p: f"staples {_fmt_list(p.staples)}; dislikes {_fmt_list(p.dislikes)}; "
                                          f"allergies {_fmt_list(p.allergies)}", _ask_pantry),
    ("Substitutions", lambda p: p.substitutions.level
                                + (f"; always {_fmt_list(p.substitutions.allow)}" if p.substitutions.allow else "")
                                + (f"; never {_fmt_list(p.substitutions.never)}" if p.substitutions.never else ""),
     _ask_swaps),
    ("Style", lambda p: f"new recipes {p.adventurousness.describe()}, max {p.max_total_time_min} min"
                        + (f", notes: {p.notes}" if p.notes else ""), _ask_style),
    ("Prices", lambda p: f"{p.price_source}" + (f" at {p.kroger_store}" if p.price_source == "kroger"
                                                 and p.kroger_store else ""), _ask_prices),
    ("Delivery", lambda p: p.delivery, _ask_delivery),
]

PROVIDER_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google_genai": "GOOGLE_API_KEY"}


def _needed_keys() -> list[str]:
    settings = load_settings()
    providers = {m.split(":", 1)[0] for m in (settings.models.planner, settings.models.worker) if ":" in m}
    keys = [PROVIDER_KEYS[p] for p in sorted(providers) if p in PROVIDER_KEYS]
    if settings.search_provider == "brave":
        keys.append("BRAVE_API_KEY")
    return keys


def _setup_keys(env_path: Path, redo: bool) -> None:
    import getpass
    import os

    from .delivery import set_env_var

    for key in _needed_keys():
        if os.environ.get(key):
            print(f"✓ {key} is set.")
            if not (redo or _yes(f"  Replace {key}?")):
                continue
        value = getpass.getpass(f"{key} (input hidden, saved to {env_path}; Enter to skip): ").strip()
        if value:
            set_env_var(env_path, key, value)
        else:
            print(f"  Skipped. Add {key} to {env_path} before running `grocery-agent plan`.")


def _telegram_connected() -> bool:
    import os

    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def cmd_init(args: argparse.Namespace) -> int:
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    prefs_path = home / "preferences.yaml"
    for name, template in (("settings.toml", SETTINGS_TEMPLATE), (".env", ENV_TEMPLATE)):
        if not (home / name).exists():
            (home / name).write_text(template)
    load_settings()  # also loads .env, so already-set keys are detected
    existing = prefs_path.exists()
    prefs = load_preferences(prefs_path) if existing else Preferences()
    if not args.defaults:
        if existing and not args.redo:
            print("You've run setup before. For each part, press Enter to keep it or answer y to change it.\n")
        else:
            print("Let's set up your meal planning. Press Enter to keep the value in brackets.\n")
        for title, summary, ask in PREF_SECTIONS:
            if existing and not args.redo:
                print(f"✓ {title}: {summary(prefs)}")
                if not _yes("  Change this?"):
                    continue
            else:
                print(f"— {title}")
            ask(prefs)
        prefs = Preferences.model_validate(prefs.model_dump())
    save_preferences(prefs, prefs_path)
    open_store().close()
    if not args.defaults:
        print("\n— API keys")
        _setup_keys(home / ".env", args.redo)
        if prefs.price_source == "kroger":
            print("\n— Kroger")
            if _kroger_connected(prefs) and not args.redo:
                print(f"✓ Kroger is connected: {prefs.kroger_store}.")
                redo_kr = _yes("  Change store or keys?")
            else:
                redo_kr = True
            if redo_kr:
                if connect_kroger(home / ".env", prefs) == 0:
                    save_preferences(prefs, prefs_path)
                else:
                    print("Kroger isn't connected yet; plans will use estimated prices. "
                          "Run `grocery-agent connect-kroger` to try again.")
        if prefs.delivery == "telegram":
            print("\n— Telegram")
            if _telegram_connected() and not args.redo:
                print("✓ Telegram is connected.")
                redo_tg = _yes("  Reconnect it?")
            else:
                redo_tg = True
            if redo_tg and connect_telegram(home / ".env") != 0:
                print("Telegram isn't connected yet; plans will still be saved as files. "
                      "Run `grocery-agent connect-telegram` to try again.")
    print(f"\nSaved {prefs_path}\nSettings: {home / 'settings.toml'}\nAPI keys: {home / '.env'}")
    return 0


# ---- plan -------------------------------------------------------------------
def cmd_plan(args: argparse.Namespace) -> int:
    from .agent.planner import run_planner
    from .agent.tools import RunContext
    from .llm import UsageMeter, make_model, run_cost
    from .normalize import LLMOracle, Normalizer
    from .pantry import LLMPantryOracle, PantryMatcher
    from .substitute import LLMSubOracle, SwapFinder
    from .render import render_plan
    from .scoring import Evaluator
    from .sourcing import HttpFetcher, make_search

    prefs = load_preferences()
    if args.meals:
        prefs.meals = args.meals
    settings = load_settings()
    store = open_store()
    past_plans = store.plan_count()
    base_calls = settings.budgets.max_source_calls
    settings.budgets = settings.budgets.for_history(past_plans)
    if settings.budgets.max_source_calls != base_calls:
        print(f"{past_plans} past plans: sourcing budget ×{settings.budgets.max_source_calls / base_calls:.1f} "
              f"({settings.budgets.max_source_calls} searches, {settings.budgets.max_new_recipes_per_run} new recipes, "
              f"{settings.budgets.max_run_tokens:,} tokens)", file=sys.stderr)
    meter = UsageMeter(settings.budgets.max_run_tokens)
    worker_model = make_model(settings, "worker")
    worker = LLMOracle(worker_model, meter.config())
    pantry = PantryMatcher(store, LLMPantryOracle(worker_model, meter.config()), prefs.staples)
    prices = make_prices(store, prefs)
    swaps = (None if prefs.substitutions.level == "off"
             else SwapFinder(store, LLMSubOracle(worker_model, meter.config()), prefs.staples))
    ctx = RunContext(store=store, prefs=prefs, settings=settings, search=make_search(settings.search_provider),
                     fetcher=HttpFetcher(),
                     normalizer=Normalizer(store, worker, prefs.currency, pantry=pantry, swaps=swaps),
                     meter=meter, prices=prices)
    print(f"Planning {prefs.meals} meals with {settings.models.planner} …", file=sys.stderr)
    ids, notes = run_planner(ctx, make_model(settings, "planner"))
    if not ids:
        print("No plan could be made: the recipe pool is empty and sourcing found nothing.", file=sys.stderr)
        return 1
    ev = Evaluator(store, prefs, prices).evaluate(ids)
    if prices is not None:
        n = sum(g.price_source != "estimate" for g in ev.grocery)
        print(f"Prices: {n} of {len(ev.grocery)} items from {prices.store_name or 'Kroger'}, the rest estimated"
              + (f" (Kroger API error: {prices.failed})" if prices.failed else "") + ".", file=sys.stderr)
    plan_id = uuid.uuid4().hex[:8]
    out = plan_path(plan_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    cost = run_cost(meter.by_model, settings.model_prices)
    out.write_text(render_plan(plan_id, ev, prefs, notes, cost=cost))
    if args.output:
        Path(args.output).write_text(out.read_text())
        print(f"Copied to {args.output}", file=sys.stderr)
    store.record_plan(plan_id, ids, str(out.resolve()), {**ev.summary(), "agent_cost": cost.as_dict()})
    if not args.no_send:
        from .delivery import DeliveryError, deliver

        try:
            print(f"Plan {deliver(out, ev, prefs, cost=cost)}.", file=sys.stderr)
        except DeliveryError as exc:
            print(f"Couldn't deliver the plan ({exc}); it's saved at {out}.", file=sys.stderr)
    for line in ctx.log:
        print("  " + line, file=sys.stderr)
    print(f"Wrote {out} · groceries {ev.total_cost:.2f} {prefs.currency} · agent {cost.describe()}", file=sys.stderr)
    return 0


# ---- feedback / pool / prices ------------------------------------------------
def plan_path(plan_id: str) -> Path:
    """Default home for plans: one file per run under ~/.grocery-agent/plans/."""
    return home_dir() / "plans" / f"{date.today().isoformat()}-{plan_id}.md"


def cmd_feedback(args: argparse.Namespace) -> int:
    from .render import parse_feedback

    store = open_store()
    if args.file:
        path = Path(args.file)
    else:
        latest = store.recent_plans(limit=1)
        if not latest:
            print("No plans yet. Run `grocery-agent plan` first.")
            return 1
        path = Path(latest[0]["path"])
    if not path.exists():
        print(f"{path} not found.")
        return 1
    rows = parse_feedback(path.read_text())
    for plan_id, rid, cooked, liked in rows:
        store.record_feedback(plan_id, rid, cooked, liked)
    print(f"Recorded feedback for {len(rows)} recipes.")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    import json

    store = open_store()
    plans = store.recent_plans(limit=args.limit)
    if not plans:
        print("No plans yet.")
        return 0
    for p in plans:
        summary = json.loads(p["summary"] or "{}")
        titles = ", ".join(r["title"] for r in summary.get("recipes", []))
        cost = summary.get("total_cost")
        agent_usd = (summary.get("agent_cost") or {}).get("usd")
        print(f"{p['created_at'][:10]}  {p['id']}  {'' if cost is None else f'{cost:.2f}  '}"
              f"{'' if agent_usd is None else f'(agent ${agent_usd:.3f})  '}{titles}")
        print(f"    {p['path']}")
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    store = open_store()
    recipes = store.all_recipes()
    print(f"{len(recipes)} recipes in the pool")
    for r in recipes[-args.limit:]:
        print(f"  {r.id}  {r.cuisine or '-':<12} {r.title}  ({r.site})")
    return 0


def _split_items(words: list[str]) -> list[str]:
    return [s.strip().lower() for s in " ".join(words).split(",") if s.strip()]


def cmd_pantry(args: argparse.Namespace) -> int:
    from .pantry import staples_key

    prefs = load_preferences()
    if args.action in ("add", "remove"):
        items = _split_items(args.items)
        if not items:
            print(f"Usage: grocery-agent pantry {args.action} <item>[, <item> ...]", file=sys.stderr)
            return 2
        have = [s.lower() for s in prefs.staples]
        if args.action == "add":
            new = [i for i in items if i not in have]
            prefs.staples += new
            msg = f"Added {', '.join(new)}." if new else "Already in your pantry."
        else:
            gone = [i for i in items if i in have]
            prefs.staples = [s for s in prefs.staples if s.lower() not in items]
            missing = [i for i in items if i not in have]
            msg = (f"Removed {', '.join(gone)}." if gone else "") + (f" Not in your pantry: {', '.join(missing)}."
                                                                     if missing else "")
        save_preferences(prefs)
        print(msg.strip())
    if not prefs.staples:
        print("Your pantry list is empty. Add to it with `grocery-agent pantry add <item>[, <item> ...]`.")
        return 0
    store = open_store()
    matches: dict[str, dict[str, list[str]]] = {}
    for name, (verdict, staple) in store.pantry_verdicts(staples_key(prefs.staples)).items():
        if staple and verdict in ("covered", "maybe") and staple.lower() != name:
            matches.setdefault(staple, {}).setdefault(verdict, []).append(name)
    print(f"Pantry ({len(prefs.staples)} items, in {home_dir() / 'preferences.yaml'}):")
    for s in prefs.staples:
        m = matches.get(s, {})
        extra = "; ".join(f"{label}: {', '.join(sorted(m[v]))}" for v, label in (("covered", "covers"),
                                                                             ("maybe", "maybe")) if m.get(v))
        print(f"  - {s}" + (f"  ({extra})" if extra else ""))
    print("Add or remove with `grocery-agent pantry add|remove <item>[, <item> ...]`.")
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
    p.add_argument("--redo", action="store_true", help="ask every question again instead of offering to skip")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("plan", help="plan meals and write a markdown grocery plan")
    p.add_argument("-o", "--output", help="also write a copy here (the plan is always kept in ~/.grocery-agent/plans/)")
    p.add_argument("--meals", type=int)
    p.add_argument("--no-send", action="store_true", help="only write the file; skip Telegram delivery")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("connect-telegram", help="connect a Telegram bot so plans are sent to you")
    p.set_defaults(fn=cmd_connect_telegram)
    p = sub.add_parser("connect-kroger", help="price groceries at your nearest Kroger store")
    p.set_defaults(fn=cmd_connect_kroger)
    p = sub.add_parser("feedback", help="record cooked/liked ticks from a plan file (default: latest plan)")
    p.add_argument("file", nargs="?")
    p.set_defaults(fn=cmd_feedback)
    p = sub.add_parser("history", help="list past plans and where they are saved")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("pool", help="list recipes in the local pool")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_pool)
    p = sub.add_parser("pantry", help="show the pantry list, or add/remove items (comma-separated)")
    p.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    p.add_argument("items", nargs="*")
    p.set_defaults(fn=cmd_pantry)
    p = sub.add_parser("prices", help="export or import the price/ingredient table as CSV")
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("file", nargs="?", default="prices.csv")
    p.set_defaults(fn=cmd_prices)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
