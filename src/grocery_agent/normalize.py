"""Ingredient normalization: raw lines -> canonical ingredients with grams.

Each distinct ingredient name goes to the worker model at most once, ever:
the answer is stored as an alias plus per-ingredient facts (macros per 100 g,
typical package size and price). Everything downstream is deterministic.
"""

from __future__ import annotations

import csv
from importlib import resources
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .pantry import PantryMatcher
from .substitute import SwapFinder
from .parse import parse_line, to_grams
from .store import IngredientFacts, Recipe, Store

Category = Literal["produce", "meat", "seafood", "dairy", "protein", "grain", "legume", "canned",
                   "condiment", "spice", "frozen", "bakery", "other"]


class IngredientEntry(BaseModel):
    name: str = Field(description="The input name, verbatim.")
    canonical: str = Field(description="Generic singular shopping-list name, lowercase, e.g. 'chicken thigh'. "
                                       "Reuse a known canonical name when it is the same product.")
    category: Category
    each_g: float | None = Field(None, description="Grams of one typical whole item, if countable.")
    density_g_per_ml: float | None = Field(None, description="Grams per ml, for liquids, powders and grains.")
    kcal_100g: float
    protein_100g: float
    carbs_100g: float
    fat_100g: float
    package_g: float = Field(description="Smallest common retail package, in grams.")
    package_price: float = Field(description="Typical supermarket price of that package.")


class IngredientBatch(BaseModel):
    items: list[IngredientEntry]


class IngredientOracle(Protocol):
    def describe(self, names: list[str], known: list[str], currency: str) -> list[IngredientEntry]: ...


ORACLE_PROMPT = """You normalize grocery ingredients for a meal planner.
For each ingredient name, give the canonical shopping-list item and estimates:
macros per 100 g (USDA-style), typical whole-item weight if countable, density if
measured by volume, and the smallest common retail package with a typical
{currency} supermarket price. Estimates are fine; be realistic, not precise.
Reuse one of these known canonical names when it is the same product:
{known}

Ingredient names:
{names}"""


class LLMOracle:
    """Worker-model oracle using structured output (any LangChain chat model)."""

    def __init__(self, model, run_config: dict | None = None):
        self.model = model.with_structured_output(IngredientBatch)
        self.run_config = run_config or {}

    def describe(self, names: list[str], known: list[str], currency: str) -> list[IngredientEntry]:
        prompt = ORACLE_PROMPT.format(currency=currency, known=", ".join(known[:400]) or "(none yet)",
                                      names="\n".join(f"- {n}" for n in names))
        batch: IngredientBatch = self.model.invoke(prompt, config=self.run_config)
        return batch.items


def seed_store(store: Store) -> int:
    """Load the bundled estimate table into an empty dictionary."""
    if store.all_facts():
        return 0
    n = 0
    with resources.files("grocery_agent.data").joinpath("seed_ingredients.csv").open() as fh:
        for row in csv.DictReader(fh):
            num = {k: (float(v) if v else None) for k, v in row.items() if k not in ("canonical", "category")}
            store.upsert_facts(IngredientFacts(canonical=row["canonical"], category=row["category"], source="seed",
                                               **num))
            store.set_alias(row["canonical"], row["canonical"])
            n += 1
    return n


def _singular(name: str) -> str:
    for suffix, repl in (("ies", "y"), ("oes", "o"), ("es", "e"), ("s", "")):
        if name.endswith(suffix) and len(name) > len(suffix) + 2:
            return name[: -len(suffix)] + repl
    return name


class Normalizer:
    def __init__(self, store: Store, oracle: IngredientOracle | None, currency: str = "USD", batch_size: int = 40,
                 pantry: PantryMatcher | None = None, swaps: SwapFinder | None = None):
        self.store = store
        self.pantry = pantry  # when set, new ingredients are also checked against the pantry list
        self.swaps = swaps    # when set, new ingredients also get their substitutes judged
        self.oracle = oracle
        self.currency = currency
        self.batch_size = batch_size

    def _resolve_locally(self, name: str) -> str | None:
        for candidate in (name, _singular(name), " ".join(_singular(w) for w in name.split())):
            hit = self.store.alias(candidate)
            if hit:
                return hit
            if self.store.facts(candidate):
                return candidate
        return None

    def resolve_names(self, names: set[str]) -> dict[str, str | None]:
        """Map names to canonicals, calling the oracle only for unknown names."""
        result: dict[str, str | None] = {}
        unknown = []
        for n in sorted(names):
            if not n:
                result[n] = None
                continue
            hit = self._resolve_locally(n)
            if hit:
                result[n] = hit
            else:
                unknown.append(n)
        if unknown and self.oracle is not None:
            known = [f.canonical for f in self.store.all_facts()]
            for i in range(0, len(unknown), self.batch_size):
                chunk = unknown[i : i + self.batch_size]
                for e in self.oracle.describe(chunk, known, self.currency):
                    canonical = e.canonical.strip().lower()
                    if not self.store.facts(canonical):
                        self.store.upsert_facts(IngredientFacts(
                            canonical=canonical, category=e.category, each_g=e.each_g,
                            density=e.density_g_per_ml, kcal=e.kcal_100g, protein=e.protein_100g,
                            carbs=e.carbs_100g, fat=e.fat_100g, package_g=e.package_g,
                            package_price=e.package_price, currency=self.currency, source="llm"))
                        known.append(canonical)
                    self.store.set_alias(e.name.strip().lower(), canonical)
                    result[e.name.strip().lower()] = canonical
        for n in unknown:
            result.setdefault(n, None)
        return result

    def normalize(self, recipes: list[Recipe]) -> None:
        parsed = {r.id: [parse_line(line) for line in r.ingredients] for r in recipes}
        names = {p.name for lines in parsed.values() for p in lines}
        mapping = self.resolve_names(names)
        keys = set()
        for r in recipes:
            items = []
            for p in parsed[r.id]:
                canonical = mapping.get(p.name)
                facts = self.store.facts(canonical) if canonical else None
                grams = to_grams(p, facts.each_g if facts else None, facts.density if facts else None)
                items.append((p.raw, p.name, canonical, grams))
                keys.add((canonical or p.name or "").lower())
            self.store.set_items(r.id, items)
        if self.pantry is not None:
            self.pantry.prepare(keys)
        if self.swaps is not None:
            self.swaps.prepare(keys)
