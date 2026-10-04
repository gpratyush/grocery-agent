"""Deterministic plan evaluation: macros, grocery list, cost, adventurousness.

Nothing here calls a model. The planner proposes a set of recipe ids and gets
back numbers plus a list of plain-language constraint violations.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .config import MACRO_KEYS, Preferences
from .store import Recipe, Store

DEFAULT_SERVINGS = 4
FACT_KEYS = {"calories": "kcal", "protein_g": "protein", "carbs_g": "carbs", "fat_g": "fat"}


def _word_match(term: str, text: str) -> bool:
    return bool(term) and re.search(rf"\b{re.escape(term.lower())}\b", text.lower()) is not None


@dataclass
class RecipeEval:
    id: str
    title: str
    url: str
    cuisine: str
    total_time: int | None
    macros: dict[str, float]          # per serving
    macro_source: str                 # "page", "computed" or "mixed"
    is_new: bool
    recently_suggested: bool
    liked_before: bool
    violations: list[str] = field(default_factory=list)

    def brief(self) -> dict:
        return {
            "id": self.id, "title": self.title, "cuisine": self.cuisine, "site": self.url.split("/")[2],
            "time_min": self.total_time, "macros": {k: round(v) for k, v in self.macros.items()},
            "new": self.is_new, "liked_before": self.liked_before, "issues": self.violations,
        }


@dataclass
class GroceryLine:
    canonical: str
    category: str
    grams: float
    packages: int | None
    package_g: float | None
    cost: float | None
    used_by: list[str]

    @property
    def utilization(self) -> float | None:
        if not self.packages or not self.package_g:
            return None
        return min(1.0, self.grams / (self.packages * self.package_g))


@dataclass
class PlanEval:
    recipes: list[RecipeEval]
    grocery: list[GroceryLine]
    unpriced: list[str]               # raw lines we couldn't quantify or price
    pantry: list[str]                 # staples assumed on hand
    total_cost: float
    servings: int
    new_fraction: float
    cuisine_counts: dict[str, int]
    cuisine_targets: dict[str, int]
    utilization: float | None
    violations: list[str]

    @property
    def feasible(self) -> bool:
        return not self.violations

    def summary(self) -> dict:
        return {
            "feasible": self.feasible,
            "violations": self.violations,
            "total_cost": round(self.total_cost, 2),
            "cost_per_serving": round(self.total_cost / max(1, self.servings), 2),
            "new_fraction": round(self.new_fraction, 2),
            "cuisine_counts": self.cuisine_counts,
            "cuisine_targets": self.cuisine_targets,
            "package_utilization": None if self.utilization is None else round(self.utilization, 2),
            "unpriced_items": len(self.unpriced),
            "recipes": [r.brief() for r in self.recipes],
        }


class Evaluator:
    def __init__(self, store: Store, prefs: Preferences):
        self.store = store
        self.prefs = prefs
        self._all_suggested = store.suggested_ids()
        self._recent = store.suggested_ids(within_weeks=prefs.avoid_repeats_weeks)
        self._liked = {r["recipe_id"] for r in store.feedback_rows() if r["liked"]}

    # ---- per recipe ----------------------------------------------------
    def is_staple(self, *texts: str | None) -> bool:
        return any(_word_match(s, t) for s in self.prefs.staples for t in texts if t)

    def computed_macros(self, r: Recipe) -> dict[str, float]:
        servings = r.servings or DEFAULT_SERVINGS
        totals = dict.fromkeys(MACRO_KEYS, 0.0)
        for item in self.store.items(r.id):
            facts = self.store.facts(item["canonical"]) if item["canonical"] else None
            if not facts or not item["grams"]:
                continue
            for key, attr in FACT_KEYS.items():
                totals[key] += item["grams"] * (getattr(facts, attr) or 0) / 100
        return {k: v / servings for k, v in totals.items()}

    def evaluate_recipe(self, r: Recipe) -> RecipeEval:
        computed = self.computed_macros(r)
        macros = {k: r.nutrition.get(k, computed[k]) for k in MACRO_KEYS}
        page_keys = set(r.nutrition) & set(MACRO_KEYS)
        source = "page" if page_keys == set(MACRO_KEYS) else ("mixed" if page_keys else "computed")
        ev = RecipeEval(
            id=r.id, title=r.title, url=r.url, cuisine=r.cuisine, total_time=r.total_time, macros=macros,
            macro_source=source, is_new=r.id not in self._all_suggested, recently_suggested=r.id in self._recent,
            liked_before=r.id in self._liked,
        )
        for key, band in self.prefs.macros_per_serving.items():
            if not band.contains(macros[key]):
                ev.violations.append(f"{key} {macros[key]:.0f} outside {band.describe()}")
        if self.prefs.max_total_time_min and r.total_time and r.total_time > self.prefs.max_total_time_min:
            ev.violations.append(f"takes {r.total_time} min (max {self.prefs.max_total_time_min})")
        if ev.recently_suggested:
            ev.violations.append(f"suggested within the last {self.prefs.avoid_repeats_weeks} weeks")
        haystack = " ".join([r.title, *r.ingredients])
        for term in self.prefs.allergies:
            if _word_match(term, haystack):
                ev.violations.append(f"contains allergen '{term}'")
        for term in self.prefs.dislikes:
            if _word_match(term, haystack):
                ev.violations.append(f"contains disliked '{term}'")
        return ev

    # ---- whole plan ----------------------------------------------------
    def evaluate(self, recipe_ids: list[str]) -> PlanEval:
        violations: list[str] = []
        recipes: list[Recipe] = []
        for rid in dict.fromkeys(recipe_ids):
            r = self.store.get_recipe(rid)
            if r is None:
                violations.append(f"unknown recipe id {rid}")
            else:
                recipes.append(r)
        if len(recipe_ids) != len(set(recipe_ids)):
            violations.append("duplicate recipe ids")

        evals = [self.evaluate_recipe(r) for r in recipes]
        for ev in evals:
            violations.extend(f"{ev.title}: {v}" for v in ev.violations)
        if len(recipes) != self.prefs.meals:
            violations.append(f"plan has {len(recipes)} meals, need {self.prefs.meals}")

        need: dict[str, float] = defaultdict(float)
        used_by: dict[str, list[str]] = defaultdict(list)
        unpriced, pantry = [], set()
        for r in recipes:
            scale = self.prefs.servings / (r.servings or DEFAULT_SERVINGS)
            for item in self.store.items(r.id):
                canon = item["canonical"]
                if self.is_staple(canon, item["name"]):
                    pantry.add(canon or item["name"])
                    continue
                if not canon or not item["grams"]:
                    unpriced.append(f"{item['raw']} ({r.title})")
                    continue
                need[canon] += item["grams"] * scale
                if r.title not in used_by[canon]:
                    used_by[canon].append(r.title)

        grocery, total, used_g, bought_g = [], 0.0, 0.0, 0.0
        for canon, grams in sorted(need.items()):
            facts = self.store.facts(canon)
            packages = cost = None
            if facts and facts.package_g and facts.package_price is not None:
                packages = max(1, math.ceil(grams / facts.package_g - 1e-9))
                cost = packages * facts.package_price
                total += cost
                used_g += grams
                bought_g += packages * facts.package_g
            else:
                unpriced.append(canon)
            grocery.append(GroceryLine(canon, facts.category if facts else "other", grams, packages,
                                       facts.package_g if facts else None, cost, used_by[canon]))

        if self.prefs.budget is not None and total > self.prefs.budget:
            violations.append(f"estimated cost {total:.2f} over budget {self.prefs.budget:.2f}")

        counts = Counter(r.cuisine or "unknown" for r in recipes)
        targets = self.prefs.cuisine_targets()
        for cuisine, want in targets.items():
            have = counts.get(cuisine, 0)
            if have < want:
                violations.append(f"need {want - have} more {cuisine}")
        new_fraction = sum(e.is_new for e in evals) / len(evals) if evals else 0.0
        band = self.prefs.adventurousness
        if evals and not band.contains(new_fraction):
            violations.append(f"new-recipe share {new_fraction:.0%} outside target {band.describe()}")

        return PlanEval(
            recipes=evals, grocery=grocery, unpriced=unpriced, pantry=sorted(pantry), total_cost=total,
            servings=len(recipes) * self.prefs.servings, new_fraction=new_fraction, cuisine_counts=dict(counts),
            cuisine_targets=targets, utilization=(used_g / bought_g) if bought_g else None, violations=violations,
        )


def greedy_plan(store: Store, prefs: Preferences, candidate_ids: list[str] | None = None) -> list[str]:
    """Fallback selector: fill cuisine targets with clean recipes, mixing new and familiar
    to land in the adventurousness band, preferring ingredient overlap."""
    ev = Evaluator(store, prefs)
    pool = [store.get_recipe(i) for i in candidate_ids] if candidate_ids else store.all_recipes()
    clean = [(r, e) for r in pool if r and not (e := ev.evaluate_recipe(r)).violations]
    targets = prefs.cuisine_targets()
    want_new = math.ceil(prefs.meals * (prefs.adventurousness.min or 0))
    chosen: list[str] = []
    chosen_ings: set[str] = set()

    def canonicals(r: Recipe) -> set[str]:
        return {i["canonical"] for i in store.items(r.id) if i["canonical"]}

    def score(r: Recipe, e: RecipeEval) -> float:
        new_count = sum(1 for c in chosen if c not in ev._all_suggested)
        s = len(canonicals(r) & chosen_ings)
        if e.is_new and new_count < want_new:
            s += 5
        if e.liked_before:
            s += 2
        return s

    for cuisine, n in sorted(targets.items(), key=lambda kv: -kv[1]):
        for _ in range(n):
            options = [(r, e) for r, e in clean if r.cuisine == cuisine and r.id not in chosen]
            if not options:
                break
            r, _e = max(options, key=lambda re_: score(*re_))
            chosen.append(r.id)
            chosen_ings |= canonicals(r)
    while len(chosen) < prefs.meals:
        options = [(r, e) for r, e in clean if r.id not in chosen]
        if not options:
            break
        r, _e = max(options, key=lambda re_: score(*re_))
        chosen.append(r.id)
        chosen_ings |= canonicals(r)
    return chosen
