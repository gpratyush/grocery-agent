"""Planner graph wiring, with a scripted chat model standing in for the LLM."""

from __future__ import annotations

from pathlib import Path

from conftest import ITALIAN_1, THAI_1, THAI_2, FakeOracle
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from grocery_agent.agent.planner import run_planner
from grocery_agent.agent.tools import RunContext
from grocery_agent.llm import UsageMeter
from grocery_agent.normalize import Normalizer
from grocery_agent.store import recipe_id

HTML = (Path(__file__).parent / "fixtures" / "curry.html").read_text()
CURRY_URL = "https://hot-thai-kitchen.com/red-curry"


class ScriptedModel(BaseChatModel):
    """Returns the next scripted AIMessage on each call and records what it saw."""

    script: list = []
    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        msg = self.script.pop(0) if self.script else AIMessage("done")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def call(name, args, i):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{i}"}])


class FakeSearch:
    def search(self, query, max_results):
        return [CURRY_URL]


class FakeFetcher:
    def get(self, url):
        return HTML if url == CURRY_URL else None


def make_ctx(store, prefs, settings):
    return RunContext(store=store, prefs=prefs, settings=settings, search=FakeSearch(), fetcher=FakeFetcher(),
                      normalizer=Normalizer(store, FakeOracle()), meter=UsageMeter(100_000))


def test_planner_sources_evaluates_and_finalizes(store, prefs, settings, pool):
    curry_id = recipe_id(CURRY_URL)
    model = ScriptedModel(script=[
        call("search_pool", {"cuisine": "thai"}, 1),
        call("source_recipes", {"cuisine": "thai", "queries": ["tofu red curry"]}, 2),
        call("evaluate_plan", {"recipe_ids": [THAI_1.id, curry_id, ITALIAN_1.id]}, 3),
        call("finalize_plan", {"recipe_ids": [THAI_1.id, curry_id, ITALIAN_1.id], "notes": "Added a tofu curry."}, 4),
    ], seen=[])
    ctx = make_ctx(store, prefs, settings)
    ids, notes = run_planner(ctx, model)
    assert ids == [THAI_1.id, curry_id, ITALIAN_1.id]
    assert notes == "Added a tofu curry."
    assert ctx.source_calls == 1 and ctx.new_recipes == 1
    assert store.get_recipe(curry_id).cuisine == "thai"
    # The opening message carries context and history, so no turn is spent fetching them.
    assert '"cuisine_targets": {"thai": 2, "italian": 1}' in model.seen[0][1].content
    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert '"feasible":true' in tool_results[2].content


def test_sourcing_budget_is_enforced_in_the_tool(store, prefs, settings, pool):
    settings.budgets.max_source_calls = 1
    model = ScriptedModel(script=[
        call("source_recipes", {"cuisine": "thai", "queries": ["a"]}, 1),
        call("source_recipes", {"cuisine": "thai", "queries": ["b"]}, 2),
        call("finalize_plan", {"recipe_ids": [THAI_1.id, THAI_2.id, ITALIAN_1.id], "notes": "ok"}, 3),
    ], seen=[])
    ctx = make_ctx(store, prefs, settings)
    run_planner(ctx, model)
    assert ctx.source_calls == 1
    second = [m for m in model.seen[2] if isinstance(m, ToolMessage)][-1]
    assert "sourcing budget spent" in second.content


def test_fallback_when_model_stops_without_finalizing(store, prefs, settings, pool):
    model = ScriptedModel(script=[AIMessage("I think we're good.")], seen=[])
    ids, notes = run_planner(make_ctx(store, prefs, settings), model)
    assert sorted(ids) == sorted([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    assert "fallback" in notes


def test_turn_limit_forces_wrap_up(store, prefs, settings, pool):
    settings.budgets.max_planner_turns = 3
    model = ScriptedModel(script=[call("search_pool", {}, i) for i in range(10)], seen=[])
    ids, notes = run_planner(make_ctx(store, prefs, settings), model)
    assert len(model.seen) <= 4
    assert ids and "fallback" in notes
    assert any("finalize_plan now" in getattr(m, "content", "") for m in model.seen[-1])
