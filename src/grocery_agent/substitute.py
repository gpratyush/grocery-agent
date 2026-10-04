"""Ingredient substitutions: buy fewer distinct things and use up packages.

Which swaps work is a judgment call, so the worker model answers it once per
ingredient (against same-category ingredients in the pool and the pantry list)
and the answer is cached. When to swap is arithmetic, done in code at
evaluation time: a swap is applied only when it lowers the bill, and never
when it breaks the diet, an allergy, or the dish's namesake ingredient.

    same        a cook wouldn't notice (chicken breast <-> chicken thigh)
    close       a small change in flavour or texture (shallot <-> red onion)
    noticeable  works, but changes the dish (cod <-> shrimp)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from .pantry import staples_key
from .store import Store

Quality = Literal["same", "close", "noticeable"]
Level = Literal["off", "same", "close", "liberal"]
ALLOWED: dict[str, set[str]] = {
    "off": set(), "same": {"same"}, "close": {"same", "close"}, "liberal": {"same", "close", "noticeable"},
}
MAX_CANDIDATES = 120


def parse_pair(text: str) -> tuple[str, str]:
    a, sep, b = text.partition("=")
    if not sep or not a.strip() or not b.strip():
        raise ValueError(f"'{text}' should look like 'cilantro = parsley'")
    return a.strip().lower(), b.strip().lower()


class Substitutions(BaseModel):
    level: Level = Field("close", description="How far swaps may go: off, same, close or liberal.")
    allow: list[str] = Field(default_factory=list, description="Pairs always allowed, e.g. 'cilantro = parsley'.")
    never: list[str] = Field(default_factory=list, description="Pairs never swapped, e.g. 'butter = olive oil'.")

    @field_validator("allow", "never")
    @classmethod
    def _pairs(cls, v: list[str]) -> list[str]:
        return [" = ".join(parse_pair(x)) for x in v]

    def pairs(self, which: str) -> set[frozenset[str]]:
        return {frozenset(parse_pair(x)) for x in getattr(self, which)}


@dataclass
class Swap:
    recipe: str
    original: str
    substitute: str
    quality: str
    pantry: bool = False    # the substitute is already on hand

    def describe(self) -> str:
        return f"{self.recipe}: {self.original} → {self.substitute}" + (" (from your pantry)" if self.pantry else "")


# ---- judgments (model, cached) ---------------------------------------------
class SubOption(BaseModel):
    name: str = Field(description="A candidate, verbatim.")
    quality: Quality


class SubEntry(BaseModel):
    name: str = Field(description="The ingredient, verbatim.")
    substitutes: list[SubOption] = Field(default_factory=list)


class SubBatch(BaseModel):
    items: list[SubEntry]


class SubOracle(Protocol):
    def suggest(self, names: list[str], candidates: dict[str, list[str]]) -> list[SubEntry]: ...


SUB_PROMPT = """You help a home cook buy fewer distinct groceries by swapping similar ingredients.
For each ingredient below, pick from ITS candidate list the ones a cook could use
instead in a typical recipe, and rate each:
- same: nobody would notice (chicken breast for chicken thigh, cilantro for parsley as garnish)
- close: a small change in flavour or texture (shallot for red onion, kale for spinach)
- noticeable: it works but changes the dish (shrimp for cod)
Leave out candidates that don't work as a swap. Only use names from the candidate lists, verbatim.

{items}"""


class LLMSubOracle:
    def __init__(self, model, run_config: dict | None = None):
        self.model = model.with_structured_output(SubBatch)
        self.run_config = run_config or {}

    def suggest(self, names: list[str], candidates: dict[str, list[str]]) -> list[SubEntry]:
        items = "\n".join(f"- {n}: candidates {', '.join(candidates[n])}" for n in names)
        return self.model.invoke(SUB_PROMPT.format(items=items), config=self.run_config).items


class SwapFinder:
    """Fills the substitution cache, asking the oracle only about ingredients it hasn't judged
    against the current pantry list. Answers are stored in both directions."""

    def __init__(self, store: Store, oracle: SubOracle | None, staples: list[str], batch_size: int = 25):
        self.store = store
        self.oracle = oracle
        self.staples = [s.lower() for s in staples]
        self.scope = staples_key(staples)
        self.batch_size = batch_size

    def candidates(self, canonical: str, pool: set[str]) -> list[str]:
        facts = self.store.facts(canonical)
        cat = facts.category if facts else None
        same_cat = sorted(c for c in pool if c != canonical and cat and (f := self.store.facts(c)) and f.category == cat)
        return (same_cat + [s for s in self.staples if s != canonical and s not in same_cat])[:MAX_CANDIDATES]

    def prepare(self, names: set[str]) -> None:
        if self.oracle is None:
            return
        pool = {c for c in self.store.item_canonicals()}
        todo = sorted(n for n in names if n and n in pool and not self.store.swaps_asked(self.scope, n))
        for i in range(0, len(todo), self.batch_size):
            chunk = todo[i : i + self.batch_size]
            cands = {n: self.candidates(n, pool) for n in chunk}
            chunk = [n for n in chunk if cands[n]]
            if chunk:
                for e in self.oracle.suggest(chunk, cands):
                    name = e.name.strip().lower()
                    if name not in cands:
                        continue
                    for opt in e.substitutes:
                        sub = opt.name.strip().lower()
                        if sub in cands[name]:
                            self.store.set_swap(self.scope, name, sub, opt.quality)
            for n in todo[i : i + self.batch_size]:
                self.store.mark_swaps_asked(self.scope, n)

    def prepare_pool(self) -> None:
        self.prepare(self.store.item_canonicals())


# ---- rules (code) ------------------------------------------------------------
def locked(title: str, original: str, substitute: str) -> bool:
    """A word of the ingredient that names the dish must survive the swap:
    "Thai Basil Chicken" keeps its basil, but chicken thigh -> chicken breast is fine."""
    for w in re.findall(r"[a-z]{4,}", original.lower()):
        if _word(w, title) and not _word(w, substitute):
            return True
    return False


def _word(w: str, text: str) -> bool:
    w = w[:-1] if len(w) > 4 and w.endswith("s") else w
    return re.search(rf"\b{re.escape(w)}(s|es)?\b", text.lower()) is not None
