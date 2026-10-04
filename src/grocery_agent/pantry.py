"""Matching ingredients against the pantry list.

Pantry entries can be specific ("olive oil") or broad ("indian spices"). An exact
whole-word match is free; everything else goes to the worker model once per
ingredient and pantry list, and the answer is cached in the store:

    covered  clearly an instance of a pantry entry (cumin -> indian spices)
    maybe    plausibly covered, worth checking (fresh ginger -> indian spices?)
    no       buy it

Evaluation only reads the cache, so scoring stays deterministic and free.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .store import Store

Verdict = Literal["covered", "maybe", "no"]


def word_match(term: str, text: str) -> bool:
    return bool(term) and re.search(rf"\b{re.escape(term.lower())}\b", text.lower()) is not None


def staples_key(staples: list[str]) -> str:
    """Cache key for a pantry list: changing the list re-asks the model."""
    norm = sorted({s.strip().lower() for s in staples if s.strip()})
    return hashlib.sha1("\n".join(norm).encode()).hexdigest()[:12]


def exact_staple(staples: list[str], *texts: str | None) -> str | None:
    for s in staples:
        if any(t and word_match(s, t) for t in texts):
            return s
    return None


class PantryMatch(BaseModel):
    name: str = Field(description="The ingredient name, verbatim.")
    verdict: Verdict = Field(description="covered, maybe or no.")
    pantry_entry: str | None = Field(None, description="The pantry entry it matches, verbatim; null for no.")


class PantryBatch(BaseModel):
    items: list[PantryMatch]


class PantryOracle(Protocol):
    def classify(self, names: list[str], staples: list[str]) -> list[PantryMatch]: ...


PANTRY_PROMPT = """A household keeps these items in its pantry at all times:
{staples}

For each ingredient below, say whether the pantry already covers it:
- covered: it is clearly one of the pantry entries or an instance of one
  (e.g. "cumin" is covered by "indian spices", "basmati rice" by "rice").
- maybe: a pantry entry plausibly covers it but a reasonable person would check
  (e.g. "fresh ginger" or "curry leaves" against "indian spices").
- no: it must be bought. Fresh produce, meat, dairy and other perishables are
  almost never covered by a shelf-stable category.
Give the matching pantry entry verbatim for covered and maybe.

Ingredients:
{names}"""


class LLMPantryOracle:
    def __init__(self, model, run_config: dict | None = None):
        self.model = model.with_structured_output(PantryBatch)
        self.run_config = run_config or {}

    def classify(self, names: list[str], staples: list[str]) -> list[PantryMatch]:
        prompt = PANTRY_PROMPT.format(staples="\n".join(f"- {s}" for s in staples),
                                      names="\n".join(f"- {n}" for n in names))
        return self.model.invoke(prompt, config=self.run_config).items


class PantryMatcher:
    """Fills the pantry cache for ingredient names, asking the oracle only about new ones."""

    def __init__(self, store: Store, oracle: PantryOracle | None, staples: list[str], batch_size: int = 60):
        self.store = store
        self.oracle = oracle
        self.staples = staples
        self.key = staples_key(staples)
        self.batch_size = batch_size

    def prepare(self, names: set[str]) -> None:
        if not self.staples:
            return
        known = self.store.pantry_verdicts(self.key)
        lookup = {s.lower(): s for s in self.staples}
        todo = []
        for n in sorted(x for x in names if x and x not in known):
            hit = exact_staple(self.staples, n)
            if hit:
                self.store.set_pantry_verdict(self.key, n, "covered", hit)
            else:
                todo.append(n)
        if not todo or self.oracle is None:
            return
        for i in range(0, len(todo), self.batch_size):
            chunk = todo[i : i + self.batch_size]
            answered = set()
            for m in self.oracle.classify(chunk, self.staples):
                name = m.name.strip().lower()
                if name not in chunk:
                    continue
                entry = lookup.get((m.pantry_entry or "").strip().lower())
                verdict = m.verdict if entry else "no"  # a match must name a real pantry entry
                self.store.set_pantry_verdict(self.key, name, verdict, entry)
                answered.add(name)
            for name in set(chunk) - answered:
                self.store.set_pantry_verdict(self.key, name, "no", None)

    def prepare_pool(self) -> None:
        self.prepare(self.store.item_keys())
