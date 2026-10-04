"""Model construction and token accounting, provider-agnostic via LangChain."""

from __future__ import annotations

from dataclasses import dataclass

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel

from .config import Settings


class BudgetExceeded(RuntimeError):
    pass


def match_price(model: str, prices: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    """Price for a reported model name, allowing provider prefixes and dated suffixes."""
    name = model.split(":", 1)[-1].lower()
    for key in sorted(prices, key=len, reverse=True):
        k = key.lower()
        if name == k or name.startswith(k + "-") or name.startswith(k + "@"):
            return prices[key]
    return None


@dataclass
class RunCost:
    tokens: int
    usd: float | None                  # None when no model in the run has a known price
    by_model: dict[str, dict]          # model -> {"tokens", "usd"}
    unpriced: list[str]

    def describe(self) -> str:
        if self.usd is None:
            return f"{self.tokens:,} tokens (cost unknown)"
        text = f"{self.tokens:,} tokens · ${self.usd:.3f}"
        if self.unpriced:
            text += f" + unpriced {', '.join(self.unpriced)}"
        return text

    def as_dict(self) -> dict:
        return {"tokens": self.tokens, "usd": None if self.usd is None else round(self.usd, 4),
                "by_model": self.by_model, "unpriced": self.unpriced}


def run_cost(usage: dict[str, dict], prices: dict[str, tuple[float, float]]) -> RunCost:
    total_tokens, total_usd, by_model, unpriced = 0, 0.0, {}, []
    for model, u in usage.items():
        tokens = u.get("total_tokens", 0)
        total_tokens += tokens
        price = match_price(model, prices)
        if price is None:
            unpriced.append(model)
            by_model[model] = {"tokens": tokens, "usd": None}
            continue
        details = u.get("input_token_details") or {}
        cache_read = details.get("cache_read", 0) or 0
        cache_write = details.get("cache_creation", 0) or 0
        fresh_input = max(0, u.get("input_tokens", 0) - cache_read - cache_write)
        usd = (fresh_input * price[0] + cache_read * price[0] * 0.1 + cache_write * price[0] * 1.25
               + u.get("output_tokens", 0) * price[1]) / 1e6
        total_usd += usd
        by_model[model] = {"tokens": tokens, "usd": round(usd, 4)}
    priced_any = len(unpriced) < len(usage)
    return RunCost(total_tokens, total_usd if priced_any else None, by_model, unpriced)


class UsageMeter:
    """Accumulates token usage across every model call in a run."""

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        self.handler = UsageMetadataCallbackHandler()

    @property
    def by_model(self) -> dict[str, dict]:
        return dict(self.handler.usage_metadata)

    @property
    def total_tokens(self) -> int:
        return sum(u.get("total_tokens", 0) for u in self.handler.usage_metadata.values())

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.total_tokens)

    def check(self) -> None:
        if self.total_tokens >= self.max_tokens:
            raise BudgetExceeded(f"token budget of {self.max_tokens} reached ({self.total_tokens} used)")

    def config(self) -> dict:
        return {"callbacks": [self.handler]}


def make_model(settings: Settings, role: str, **kwargs) -> BaseChatModel:
    name = getattr(settings.models, role)
    return init_chat_model(name, **kwargs)
