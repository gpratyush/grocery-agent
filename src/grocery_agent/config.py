"""User preferences (the context layer) and app settings.

Three files live in the home directory (``$GROCERY_AGENT_HOME``, default
``~/.grocery-agent``):

- ``preferences.yaml``: what to plan for. Safe to share; holds no secrets.
- ``settings.toml``: models per role, token/sourcing budgets, search provider, sites.
- ``.env``: API keys. Real environment variables win over this file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


def home_dir() -> Path:
    return Path(os.environ.get("GROCERY_AGENT_HOME", Path.home() / ".grocery-agent")).expanduser()


class Band(BaseModel):
    """Inclusive numeric range; either side may be open."""

    min: float | None = None
    max: float | None = None

    def contains(self, value: float) -> bool:
        if self.min is not None and value < self.min:
            return False
        if self.max is not None and value > self.max:
            return False
        return True

    def describe(self) -> str:
        if self.min is not None and self.max is not None:
            return f"{self.min:g}–{self.max:g}"
        if self.min is not None:
            return f"≥{self.min:g}"
        if self.max is not None:
            return f"≤{self.max:g}"
        return "any"


class Preferences(BaseModel):
    meals: int = Field(5, ge=1, le=21, description="Number of meals (recipes) to plan.")
    servings: int = Field(2, ge=1, description="Servings to cook per meal.")
    macros_per_serving: dict[str, Band] = Field(
        default_factory=lambda: {
            "calories": Band(min=450, max=800),
            "protein_g": Band(min=30),
        }
    )
    cuisine_mix: dict[str, float] = Field(
        default_factory=lambda: {"italian": 1, "mexican": 1, "indian": 1, "thai": 1}
    )
    budget: float | None = Field(None, description="Max grocery spend for the whole plan.")
    currency: str = "USD"
    staples: list[str] = Field(
        default_factory=lambda: ["salt", "black pepper", "olive oil", "vegetable oil", "sugar", "flour", "water"]
    )
    dislikes: list[str] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    adventurousness: Band = Field(
        default_factory=lambda: Band(min=0.2, max=0.6),
        description="Target share of recipes never suggested before (0–1).",
    )
    max_total_time_min: int | None = 60
    avoid_repeats_weeks: int = Field(3, description="Don't re-suggest a recipe suggested within this many weeks.")
    notes: str = Field("", description="Free-text guidance for the planner.")
    delivery: Literal["file", "telegram"] = Field(
        "file", description="Where the finished plan goes. The markdown file is always written; "
                            "'telegram' also sends it to your Telegram chat.")

    @field_validator("macros_per_serving")
    @classmethod
    def _known_macros(cls, v: dict[str, Band]) -> dict[str, Band]:
        unknown = set(v) - set(MACRO_KEYS)
        if unknown:
            raise ValueError(f"unknown macro keys {sorted(unknown)}; use {MACRO_KEYS}")
        return v

    def cuisine_targets(self) -> dict[str, int]:
        """Turn cuisine weights into a meal count per cuisine (largest remainder)."""
        total = sum(self.cuisine_mix.values()) or 1
        raw = {c: self.meals * w / total for c, w in self.cuisine_mix.items()}
        counts = {c: int(x) for c, x in raw.items()}
        leftover = self.meals - sum(counts.values())
        for c in sorted(raw, key=lambda c: raw[c] - counts[c], reverse=True)[:leftover]:
            counts[c] += 1
        return counts


MACRO_KEYS = ["calories", "protein_g", "carbs_g", "fat_g"]

DEFAULT_SITES: dict[str, list[str]] = {
    "italian": ["giallozafferano.com", "seriouseats.com", "bonappetit.com"],
    "mexican": ["mexicoinmykitchen.com", "isabeleats.com", "seriouseats.com"],
    "indian": ["indianhealthyrecipes.com", "vegrecipesofindia.com", "cookwithmanali.com"],
    "thai": ["hot-thai-kitchen.com", "rachelcooksthai.com", "seriouseats.com"],
    "korean": ["maangchi.com", "mykoreankitchen.com"],
    "japanese": ["justonecookbook.com", "norecipes.com"],
    "chinese": ["thewoksoflife.com", "omnivorescookbook.com"],
    "mediterranean": ["themediterraneandish.com", "cookieandkate.com"],
    "american": ["seriouseats.com", "budgetbytes.com", "simplyrecipes.com"],
    "any": ["budgetbytes.com", "seriouseats.com", "bbcgoodfood.com", "simplyrecipes.com"],
}


class Models(BaseModel):
    """Provider-qualified model names, passed to LangChain's ``init_chat_model``."""

    planner: str = "anthropic:claude-sonnet-5-5"
    worker: str = "anthropic:claude-haiku-4-5"


class Budgets(BaseModel):
    max_planner_turns: int = 16
    max_source_calls: int = 6
    max_new_recipes_per_run: int = 30
    max_pages_per_call: int = 12
    max_run_tokens: int = 250_000


class Settings(BaseModel):
    models: Models = Field(default_factory=Models)
    budgets: Budgets = Field(default_factory=Budgets)
    search_provider: str = "ddgs"  # "ddgs" (no key) or "brave" (BRAVE_API_KEY)
    sites: dict[str, list[str]] = Field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_SITES.items()})

    def sites_for(self, cuisine: str) -> list[str]:
        return self.sites.get(cuisine.lower()) or self.sites.get("any", [])


def load_env_file(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing env vars are not overwritten."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_preferences(path: Path | None = None) -> Preferences:
    path = path or home_dir() / "preferences.yaml"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run `grocery-agent init` first.")
    return Preferences.model_validate(yaml.safe_load(path.read_text()) or {})


def save_preferences(prefs: Preferences, path: Path | None = None) -> Path:
    path = path or home_dir() / "preferences.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(prefs.model_dump(exclude_none=True), sort_keys=False, allow_unicode=True))
    return path


def load_settings(path: Path | None = None) -> Settings:
    path = path or home_dir() / "settings.toml"
    load_env_file(path.parent / ".env")
    if not path.exists():
        return Settings()
    data = tomllib.loads(path.read_text())
    settings = Settings.model_validate(data)
    # Sites in the file extend, rather than replace, the defaults.
    merged = {k: list(v) for k, v in DEFAULT_SITES.items()}
    merged.update({k.lower(): v for k, v in data.get("sites", {}).items()})
    settings.sites = merged
    return settings


SETTINGS_TEMPLATE = """\
# grocery-agent settings. Models use LangChain's "provider:model" form, e.g.
# "openai:gpt-5-mini" or "google_genai:gemini-2.5-flash" (install the matching extra).
search_provider = "ddgs"   # "ddgs" needs no key; "brave" uses BRAVE_API_KEY

[models]
planner = "anthropic:claude-sonnet-5-5"   # decides what to source and picks the plan
worker = "anthropic:claude-haiku-4-5"     # normalizes ingredients, estimates prices

[budgets]
max_planner_turns = 16
max_source_calls = 6
max_new_recipes_per_run = 30
max_pages_per_call = 12
max_run_tokens = 250000

# Extra or replacement recipe sites per cuisine (merged over the built-in map).
[sites]
# korean = ["maangchi.com", "mykoreankitchen.com"]
"""

ENV_TEMPLATE = """\
# API keys for grocery-agent. Real environment variables take precedence.
ANTHROPIC_API_KEY=
# OPENAI_API_KEY=
# GOOGLE_API_KEY=
# BRAVE_API_KEY=
# Telegram delivery (set up with `grocery-agent connect-telegram`):
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
"""
