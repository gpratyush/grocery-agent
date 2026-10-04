# grocery-agent

Plans a set of meals from recipes on the web, filtered by your macros, budget, cuisine mix and how adventurous you want to be, and writes a markdown plan with a costed grocery list.

It runs on your own API keys and works with any LangChain chat model provider (Anthropic by default; OpenAI and Google through optional extras).

## Install

```bash
pipx install git+https://github.com/gpratyush/grocery-agent
# or, for OpenAI / Gemini models:
pipx install "grocery-agent[openai,google] @ git+https://github.com/gpratyush/grocery-agent"
```

Requires Python 3.11+.

## Set up

```bash
grocery-agent init
```

`init` walks through your preferences in short sections (meals, macros, cuisines, budget, pantry and restrictions, style, delivery), then your API key, then Telegram if you chose it. Keys are typed with hidden input. When you run `init` again, it shows what's already set up and lets you press Enter to skip each part, so you can change just one thing. `init --redo` asks every question again.

Everything lives in `~/.grocery-agent/` (override with `GROCERY_AGENT_HOME`):

| File | What it holds |
|---|---|
| `preferences.yaml` | Meals per run, servings, macro bands per serving, cuisine mix, budget, pantry staples, dislikes, allergies, adventurousness band, max cooking time, free-text notes. No secrets, so it's safe to share. |
| `settings.toml` | Which model does which job, token and sourcing budgets, search provider, extra recipe sites per cuisine. |
| `plans/` | Every plan the tool has written, one markdown file per run. This is your plan history. |
| `.env` | API keys (`ANTHROPIC_API_KEY`, optionally `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `BRAVE_API_KEY`, and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for Telegram delivery). Real environment variables take precedence. |

### Getting plans on Telegram

`init` asks where finished plans should go: `file` (the default) or `telegram`. The markdown file is always written. With `telegram`, each plan also arrives in your Telegram chat as a short summary message (meals, links, estimated cost) followed by the full plan as an attached `.md` file.

To connect, choose `telegram` in `init`, or run `grocery-agent connect-telegram` at any time:

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token it gives you.
2. Paste the token when prompted. Input is hidden, and the token is stored only in `~/.grocery-agent/.env` with owner-only permissions.
3. Open your new bot, press **Start**, then press Enter in the terminal. The tool finds your chat id and sends a test message.

`grocery-agent plan --no-send` skips delivery for a single run. If sending fails, the plan is still saved and the error is printed.

## Use

```bash
grocery-agent plan                  # saves ~/.grocery-agent/plans/YYYY-MM-DD-<id>.md
grocery-agent plan --meals 4 -o week.md   # ...and also writes a copy to week.md
grocery-agent history               # past plans and where each is saved
grocery-agent feedback              # after the week: read back cooked/liked ticks from the latest plan
grocery-agent connect-telegram      # send future plans to your Telegram chat
grocery-agent pool                  # list recipes collected so far
grocery-agent prices export prices.csv   # edit estimated prices / package sizes...
grocery-agent prices import prices.csv   # ...and they stick
```

The plan file has the meals with links and per-serving macros, the grocery list grouped by aisle with package-rounded quantities and estimated cost, the pantry items it assumed, any constraint it couldn't meet, and a feedback checklist.

## How it works

```
             ┌────────────── planner agent (LangGraph, mid-tier model) ──────────────┐
             │  decides what's missing, writes targeted queries, picks the final set │
             └───────┬──────────────┬──────────────────┬───────────────────┬─────────┘
               search_pool    source_recipes      evaluate_plan       finalize_plan
                  (free)     search → fetch →    macros, cost, novelty,
                             recipe-scrapers →   cuisine mix, violations
                             normalize (cheap model, once per ingredient)
             └──────────────────── SQLite: recipe pool · ingredient dictionary · prices · history ┘
```

- **The model makes judgment calls; Python does the arithmetic.** Macros, package-rounded cost, cuisine counts, novelty against history, allergens and time limits are all computed in code. The planner gets back numbers plus plain-language violations ("Green Curry: protein_g 18 outside ≥30") and revises.
- **Sourcing is driven by the agent but bounded by code.** The planner writes dish-level queries from the cuisine mix, macro gaps and history (what was suggested recently, what you liked) and chooses sites, starting from a per-cuisine site map. `source_recipes` enforces the budgets itself: calls per run, new recipes per run, pages per call, and a run-wide token cap.
- **No page text reaches a model.** Pages are parsed with [recipe-scrapers](https://github.com/hhursev/recipe-scrapers) (schema.org and site-specific scrapers). Pages without structured recipe data are skipped.
- **Each ingredient name hits the cheap model once, ever.** Lines are parsed by rules. Only unknown ingredient names go to the worker model, which returns a canonical name, macros per 100 g and a typical package size and price. Those answers are stored, and about 80 common ingredients ship pre-seeded.
- **The pool grows.** Recipes and ingredient facts persist between runs, so later runs search less.
- **There is always an output.** If the planner stops early or runs out of budget, a deterministic selector picks the plan from the pool.

## Cost

Every plan reports what the run cost in model tokens and dollars. The figure appears at the top of the plan file, in the Telegram summary, in the terminal and in `grocery-agent history`. Prices for current Claude models are built in. For any other model, add `"model-name" = [input, output]` (USD per million tokens) under `[model_prices]` in `settings.toml`. Otherwise its cost shows as unknown.

**First runs search more.** With no history, the recipe pool is empty, so the sourcing budget starts at `cold_start_multiplier` (default 3×) the normal amount. That covers web searches, new recipes and run tokens, plus the planner turns needed to use them. The extra then decays exponentially, halving every `cold_start_half_life` plans (default 2):

| Past plans | 0 | 1 | 2 | 4 | 8 |
|---|---|---|---|---|---|
| Sourcing budget | 3× | 2.4× | 2× | 1.5× | ~1.1× |

My rough estimate for a steady-state 5-meal run (Sonnet planning, Haiku normalizing) is well under $0.50, and a first run costs more. Lower the values under `[budgets]` in `settings.toml` to cap cost further.

## Limitations

- Prices are estimates (bundled table plus model estimates), labeled as such. Correct them with `prices import`.
- The default search uses DuckDuckGo through `ddgs`, with no key, and can be rate-limited. Set `search_provider = "brave"` and `BRAVE_API_KEY` for a more reliable search API.
- Macros come from the recipe page when it lists them; otherwise they're computed from ingredient estimates.

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The design notes behind this layout are in the project thread that started it.
