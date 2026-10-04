"""Tools the planner agent calls. Each wraps deterministic Python and returns
compact JSON (about 100 tokens per recipe), never page text."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool, tool

from ..config import Preferences, Settings
from ..llm import UsageMeter
from ..normalize import Normalizer
from ..scoring import Evaluator, PlanEval, PriceSource
from ..sourcing import Fetcher, SearchProvider, source
from ..store import Store

MAX_QUERIES_PER_CALL = 3


@dataclass
class RunContext:
    store: Store
    prefs: Preferences
    settings: Settings
    search: SearchProvider
    fetcher: Fetcher
    normalizer: Normalizer
    meter: UsageMeter
    prices: PriceSource | None = None   # store prices; None means estimates only
    source_calls: int = 0
    new_recipes: int = 0
    final_ids: list[str] | None = None
    final_notes: str = ""
    log: list[str] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)  # tools may run on parallel threads

    def evaluator(self) -> Evaluator:
        return Evaluator(self.store, self.prefs, self.prices)

    def budget_status(self) -> dict:
        b = self.settings.budgets
        return {
            "source_calls_left": max(0, b.max_source_calls - self.source_calls),
            "new_recipes_left": max(0, b.max_new_recipes_per_run - self.new_recipes),
            "tokens_left": self.meter.remaining,
        }


def diet_query(query: str, diets: list[str]) -> str:
    """Prefix the diet so search returns compliant dishes: "vegetarian paneer tikka"."""
    missing = [d for d in diets if d.lower() not in query.lower()]
    return " ".join([*missing, query]) if missing else query


def _dump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def build_tools(ctx: RunContext) -> list[BaseTool]:
    @tool
    def search_pool(cuisine: str | None = None, keyword: str | None = None, only_new: bool = False,
                    include_flagged: bool = False, limit: int = 15) -> str:
        """Search recipes already in the local pool (free, no fetching).

        Args:
            cuisine: Filter by cuisine, e.g. "thai". Omit for all.
            keyword: Match against title or ingredients, e.g. "chickpea".
            only_new: Only recipes never suggested before.
            include_flagged: Also return recipes that miss a target (macros, time, dislikes, repeats).
                Recipes that break the diet or an allergy are never returned.
            limit: Max results.
        """
        with ctx.lock:
            return _search_pool(cuisine, keyword, only_new, include_flagged, limit)

    def _search_pool(cuisine, keyword, only_new, include_flagged, limit) -> str:
        ev = ctx.evaluator()
        out = []
        for r in ctx.store.all_recipes():
            if cuisine and r.cuisine != cuisine.lower():
                continue
            if keyword and keyword.lower() not in (r.title + " " + " ".join(r.ingredients)).lower():
                continue
            e = ev.evaluate_recipe(r)
            if e.excluded:
                continue
            if only_new and not e.is_new:
                continue
            if e.violations and not include_flagged:
                continue
            out.append(e.brief())
        out.sort(key=lambda b: (bool(b["issues"]), not b["new"]))
        return _dump({"count": len(out), "recipes": out[:limit]})

    @tool
    def source_recipes(cuisine: str, queries: list[str], sites: list[str] | None = None) -> str:
        """Find NEW recipes on the web and add them to the pool. Costs budget; use targeted queries.

        Args:
            cuisine: The cuisine these recipes are for, e.g. "korean".
            queries: 1-3 specific dish-level searches, e.g. ["gochujang chicken thighs", "kimchi tofu stew"].
                Shape them to fill a gap: macros, cuisine, novelty vs history, ingredient overlap.
            sites: Sites to search. Omit to use the configured sites for this cuisine.
        """
        with ctx.lock:
            return _source_recipes(cuisine, queries, sites)

    def _source_recipes(cuisine, queries, sites) -> str:
        status = ctx.budget_status()
        if status["source_calls_left"] <= 0 or status["new_recipes_left"] <= 0:
            return _dump({"error": "sourcing budget spent; plan from the pool", "budget": status})
        ctx.meter.check()
        ctx.source_calls += 1
        use_sites = sites or ctx.settings.sites_for(cuisine)
        max_pages = min(ctx.settings.budgets.max_pages_per_call, status["new_recipes_left"])
        queries = [diet_query(q, ctx.prefs.diet) for q in queries[:MAX_QUERIES_PER_CALL]]
        found = source(queries, use_sites, cuisine.lower(), search=ctx.search,
                       fetcher=ctx.fetcher, max_pages=max_pages, known_url=ctx.store.has_url)
        added = [r for r in found if ctx.store.add_recipe(r)]
        ctx.new_recipes += len(added)
        if added:
            ctx.normalizer.normalize(added)
        ctx.log.append(f"sourced {len(added)} {cuisine} via {queries} on {use_sites}")
        ev = ctx.evaluator()
        evals = [ev.evaluate_recipe(r) for r in added]
        ok = [e for e in evals if not e.excluded]
        out = {"added": len(ok), "recipes": [e.brief() for e in ok], "budget": ctx.budget_status()}
        if len(ok) < len(evals):
            out["dropped_for_diet_or_allergy"] = [f"{e.title}: {e.excluded[0]}" for e in evals if e.excluded]
        return _dump(out)

    @tool
    def evaluate_plan(recipe_ids: list[str]) -> str:
        """Score a candidate plan: cost with package rounding, macros, cuisine mix, novelty and violations.

        Args:
            recipe_ids: Recipe ids from search_pool / source_recipes, one per meal.
        """
        with ctx.lock:
            pe: PlanEval = ctx.evaluator().evaluate(recipe_ids)
        summary = pe.summary()
        summary["recipes"] = [{"id": r["id"], "title": r["title"], "issues": r["issues"]} for r in summary["recipes"]]
        return _dump(summary)

    @tool
    def finalize_plan(recipe_ids: list[str], notes: str) -> str:
        """Commit the final plan. Call once, when the plan is feasible or the budget is nearly spent.

        Args:
            recipe_ids: The chosen recipe ids, one per meal.
            notes: 2-5 sentences for the user: why this set, trade-offs, any unmet constraint and why.
        """
        ids = list(dict.fromkeys(recipe_ids))
        with ctx.lock:
            ev = ctx.evaluator()
            evals = [ev.evaluate_recipe(r) for i in ids if (r := ctx.store.get_recipe(i))]
            bad = [f"{e.title}: {e.excluded[0]}" for e in evals if e.excluded]
        if bad:
            return _dump({"ok": False, "error": "these recipes break the diet or an allergy and can never be in a "
                                                "plan; replace them and call finalize_plan again", "recipes": bad})
        ctx.final_ids = ids
        ctx.final_notes = notes
        return _dump({"ok": True})

    return [search_pool, source_recipes, evaluate_plan, finalize_plan]
