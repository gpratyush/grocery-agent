"""The planner: a LangGraph state graph with one LLM node over the Python tools.

    START -> planner -(tool calls)-> tools -> planner ... -> END
                     \\-(no tool calls / budget spent)-> fallback -> END

The model decides what to source and which set to pick; Python enforces every
hard constraint and every budget. If the model stops without finalizing, the
fallback node picks a plan deterministically so a run always produces output.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from ..scoring import greedy_plan
from .tools import RunContext, build_tools

SYSTEM_PROMPT = """You plan a week of home-cooked meals and the grocery shop for one household.

You work through tools. Python computes every number (macros, cost, novelty) and
enforces the hard rules; your job is judgment:
- Decide what is missing from the local recipe pool for this week, given the
  cuisine mix, macro targets, budget and the household's history.
- Write targeted, dish-level search queries to fill exactly those gaps. Use history:
  avoid what was suggested recently, lean toward what was liked, and introduce new
  dishes, cuisines or ingredients to land in the adventurousness band.
- Pick a set that is varied (not three curries), weeknight-practical, and shares
  ingredients so packages get used up.

Process: search_pool first (free). Call source_recipes only for real gaps, at most a
few times. Propose a set with evaluate_plan, fix what it reports, then call
finalize_plan once. If a constraint can't be met within budget, finalize the best
set anyway and say why in the notes. total_cost (what the budget checks) assumes the
pantry_checks are on hand; the other total is shown to the user too. Keep messages short."""


class PlannerState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    turns: int
    stop: bool


def history_brief(ctx: RunContext) -> dict:
    store, prefs = ctx.store, ctx.prefs
    plans = store.recent_plans(limit=prefs.avoid_repeats_weeks + 2)
    recent_titles, recent_cuisines, recent_ings = [], Counter(), Counter()
    for p in plans:
        for rid in json.loads(p["recipe_ids"]):
            r = store.get_recipe(rid)
            if not r:
                continue
            recent_titles.append(r.title)
            recent_cuisines[r.cuisine] += 1
            for item in store.items(rid):
                if item["canonical"]:
                    recent_ings[item["canonical"]] += 1
    liked, cooked_not_liked = [], []
    for f in store.feedback_rows():
        r = store.get_recipe(f["recipe_id"])
        if not r:
            continue
        if f["liked"]:
            liked.append(r.title)
        elif f["cooked"]:
            cooked_not_liked.append(r.title)
    pool = store.all_recipes()
    return {
        "recent_plans": len(plans),
        "recently_suggested": recent_titles[:30],
        "recent_cuisines": dict(recent_cuisines),
        "frequent_recent_ingredients": [i for i, _ in recent_ings.most_common(12)],
        "liked": liked[:20],
        "cooked_not_liked": cooked_not_liked[:20],
        "pool_size": len(pool),
        "pool_by_cuisine": dict(Counter(r.cuisine for r in pool)),
    }


def opening_message(ctx: RunContext) -> str:
    prefs = ctx.prefs
    context = {
        "preferences": prefs.model_dump(exclude_none=True),
        "cuisine_targets": prefs.cuisine_targets(),
        "sites_by_cuisine": {c: ctx.settings.sites_for(c) for c in prefs.cuisine_mix},
        "budget": ctx.budget_status(),
        "history": history_brief(ctx),
    }
    return (f"Plan {prefs.meals} meals for {prefs.servings} servings each.\n"
            f"Context (JSON):\n{json.dumps(context, ensure_ascii=False)}")


def build_graph(ctx: RunContext, model: BaseChatModel):
    tools = build_tools(ctx)
    bound = model.bind_tools(tools)
    max_turns = ctx.settings.budgets.max_planner_turns

    def planner(state: PlannerState) -> dict:
        turns = state.get("turns", 0)
        if ctx.meter.remaining <= 0:
            return {"stop": True}
        messages = [SystemMessage(SYSTEM_PROMPT), *state["messages"]]
        extra = []
        if turns >= max_turns - 2:
            extra = [HumanMessage("Turn budget nearly spent: call finalize_plan now with your best set.")]
        response = bound.invoke(messages + extra, config=ctx.meter.config())
        return {"messages": [*extra, response], "turns": turns + 1}

    def route_planner(state: PlannerState) -> str:
        last = state["messages"][-1]
        if state.get("stop") or state.get("turns", 0) > max_turns:
            return "fallback"
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "fallback"

    def route_tools(_: PlannerState) -> str:
        return END if ctx.final_ids is not None else "planner"

    def fallback(state: PlannerState) -> dict:
        if ctx.final_ids is None:
            ctx.final_ids = greedy_plan(ctx.store, ctx.prefs)
            last = state["messages"][-1]
            said = last.content if isinstance(last, AIMessage) and isinstance(last.content, str) else ""
            ctx.final_notes = ("The planner stopped before finalizing, so this set was picked by the fallback "
                               "selector from the recipe pool. " + said).strip()
        return {}

    g = StateGraph(PlannerState)
    g.add_node("planner", planner)
    g.add_node("tools", ToolNode(tools))
    g.add_node("fallback", fallback)
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", route_planner, ["tools", "fallback"])
    g.add_conditional_edges("tools", route_tools, ["planner", END])
    g.add_edge("fallback", END)
    return g.compile()


def run_planner(ctx: RunContext, model: BaseChatModel) -> tuple[list[str], str]:
    pending = [ctx.store.get_recipe(i) for i in ctx.store.unnormalized_recipe_ids()]
    if pending:
        ctx.normalizer.normalize([r for r in pending if r])
    if ctx.normalizer.pantry is not None:
        ctx.normalizer.pantry.prepare_pool()  # cached; asks only about ingredients new to this pantry list
    graph = build_graph(ctx, model)
    graph.invoke({"messages": [HumanMessage(opening_message(ctx))], "turns": 0, "stop": False},
                 config={"recursion_limit": 4 * ctx.settings.budgets.max_planner_turns + 10})
    return ctx.final_ids or [], ctx.final_notes
