"""Model construction and token accounting, provider-agnostic via LangChain."""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel

from .config import Settings


class BudgetExceeded(RuntimeError):
    pass


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
