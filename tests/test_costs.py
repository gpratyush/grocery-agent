import pytest
from conftest import ITALIAN_1, THAI_1, THAI_2

from grocery_agent.config import DEFAULT_MODEL_PRICES, Budgets
from grocery_agent.delivery import telegram_messages
from grocery_agent.llm import match_price, run_cost
from grocery_agent.render import render_plan
from grocery_agent.scoring import Evaluator


def test_sourcing_budget_decays_with_history():
    b = Budgets(max_source_calls=6, max_new_recipes_per_run=30, max_run_tokens=250_000, max_planner_turns=16)
    cold, two, many = b.for_history(0), b.for_history(2), b.for_history(20)
    assert (cold.max_source_calls, cold.max_new_recipes_per_run, cold.max_run_tokens) == (18, 90, 750_000)
    assert cold.max_planner_turns == 16 + 12
    assert two.max_source_calls == 12  # extra halves after cold_start_half_life (2) plans
    assert many.max_source_calls == 6 and many.max_planner_turns == 16
    assert b.sourcing_factor(0) > b.sourcing_factor(1) > b.sourcing_factor(5) > 1


def test_no_boost_when_multiplier_is_one():
    b = Budgets(cold_start_multiplier=1.0)
    assert b.for_history(0) == b


def test_match_price_handles_prefixes_and_dated_ids():
    assert match_price("claude-sonnet-5-5", DEFAULT_MODEL_PRICES) == (2.0, 10.0)
    assert match_price("anthropic:claude-haiku-4-5", DEFAULT_MODEL_PRICES) == (1.0, 5.0)
    assert match_price("claude-haiku-4-5-20251001", DEFAULT_MODEL_PRICES) == (1.0, 5.0)
    assert match_price("claude-sonnet-5", DEFAULT_MODEL_PRICES) == (2.0, 10.0)
    assert match_price("gpt-5-mini", DEFAULT_MODEL_PRICES) is None


def test_run_cost_counts_cache_and_unpriced_models():
    usage = {
        "claude-sonnet-5-5": {"input_tokens": 50_000, "output_tokens": 5_000, "total_tokens": 55_000,
                              "input_token_details": {"cache_read": 40_000, "cache_creation": 0}},
        "claude-haiku-4-5": {"input_tokens": 10_000, "output_tokens": 2_000, "total_tokens": 12_000},
        "mystery-model": {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200},
    }
    c = run_cost(usage, DEFAULT_MODEL_PRICES)
    sonnet = (10_000 * 2 + 40_000 * 0.2 + 5_000 * 10) / 1e6
    haiku = (10_000 * 1 + 2_000 * 5) / 1e6
    assert c.usd == pytest.approx(sonnet + haiku)
    assert c.tokens == 67_200 and c.unpriced == ["mystery-model"]
    assert "$0.098" in c.describe() and "unpriced mystery-model" in c.describe()
    assert run_cost({"x": {"total_tokens": 5}}, {}).describe() == "5 tokens (cost unknown)"


def test_cost_appears_in_plan_and_telegram_summary(store, prefs, pool):
    ev = Evaluator(store, prefs).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    cost = run_cost({"claude-haiku-4-5": {"input_tokens": 1000, "output_tokens": 0, "total_tokens": 1000}},
                    DEFAULT_MODEL_PRICES)
    md = render_plan("p", ev, prefs, cost=cost)
    assert "Agent cost this run: 1,000 tokens · $0.001" in md
    assert "Agent cost: 1,000 tokens · $0.001" in telegram_messages(ev, prefs, cost)[0]
