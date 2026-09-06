"""Deterministic intent detection and the output guards.

These exist because a language model cannot be trusted to emit a tool call for an
unambiguous request, and will confidently narrate a result it never received. Both
failures were observed live on a small local model. The guards make correctness
independent of model quality.
"""

import pytest

from app.orchestration.graph import _guard_false_confirmation, _guard_figures
from app.orchestration.intents import detect
from app.tools.base import ToolResult

# ── Logging intent ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "I spent 250 on auto",
        "spent 250 on auto",
        "paid 820 for groceries",
        "bought coffee for 180",
        "180 on coffee",          # no verb at all — very common in speech
        "2000 for petrol",
        "250 for the auto",
    ],
)
def test_spend_utterances_take_the_deterministic_path(text):
    intent = detect("vega", text)
    assert intent is not None, f"{text!r} should log deterministically"
    assert intent.tool == "expense_log"
    assert intent.blocks_write_tools


@pytest.mark.parametrize(
    "text",
    [
        "my budget is 5000",
        "my salary is 90000",
        "my savings goal is 200000",
        "the balance is 12000",
        "I want to limit food to 6000",
    ],
)
def test_stated_amounts_are_not_logged_as_spends(text):
    """'My budget is 5000' must not become a 5000-rupee expense."""
    assert detect("vega", text) is None


@pytest.mark.parametrize(
    "text",
    ["I spent too much this month", "money is tight", "spending feels high"],
)
def test_spend_talk_without_an_amount_goes_to_the_model(text):
    assert detect("vega", text) is None


# ── Query intent ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,tool",
    [
        ("how much did I spend this month", "expense_total"),
        ("how much have I spent", "expense_total"),
        ("what did I spend on groceries", "expense_total"),
        ("give me a breakdown by category", "expense_summary"),
        ("show me a summary", "expense_summary"),
    ],
)
def test_questions_query_rather_than_log(text, tool):
    """'How much did I spend' contains a spend verb but is a question."""
    intent = detect("vega", text)
    assert intent is not None
    assert intent.tool == tool
    assert not intent.blocks_write_tools


def test_query_extracts_the_period():
    assert detect("vega", "how much did I spend today").arguments["period"] == "today"
    assert detect("vega", "how much this week").arguments["period"] == "week"
    assert detect("vega", "how much this year").arguments["period"] == "year"


def test_query_extracts_a_category_hint():
    intent = detect("vega", "how much did I spend on groceries")
    assert intent.arguments.get("category") == "groceries"


# ── Corrections ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text", ["actually make that 280", "no it was 300", "sorry it was 190"]
)
def test_corrections_correct_rather_than_log_again(text):
    """Without this, a correction double-counts as a fresh spend."""
    intent = detect("vega", text)
    assert intent is not None
    assert intent.tool == "expense_correct"


# ── Agent scoping ────────────────────────────────────────────────────────────

def test_astraea_may_query_but_never_log():
    assert detect("astraea", "how much did I spend this month").tool == "expense_total"
    assert detect("astraea", "I spent 250 on auto") is None


@pytest.mark.parametrize("agent", ["lyra", "nova", "athena", "selene"])
def test_other_agents_have_no_finance_fast_path(agent):
    assert detect(agent, "I spent 250 on auto") is None
    assert detect(agent, "how much did I spend") is None


# ── Guard: no false confirmations ────────────────────────────────────────────

@pytest.mark.parametrize(
    "claim",
    [
        "Logged. 180 rupees, coffee.",
        "Saved that for you.",
        "Recorded 250 rupees.",
        "Done.",
        "Updated.",
    ],
)
def test_confirmation_without_a_write_is_suppressed(claim):
    """The exact failure observed live: a fluent confirmation of nothing."""
    text, suppressed = _guard_false_confirmation(claim, wrote=False)
    assert suppressed
    assert "couldn't save" in text


def test_confirmation_with_a_write_passes_through():
    text, suppressed = _guard_false_confirmation("Logged. 180 rupees, coffee.", wrote=True)
    assert not suppressed
    assert text == "Logged. 180 rupees, coffee."


def test_non_confirming_text_passes_through_without_a_write():
    """Answering a question is not claiming a write."""
    text, suppressed = _guard_false_confirmation("You've spent 3,250 rupees.", wrote=False)
    assert not suppressed


# ── Guard: figures come from the tool ────────────────────────────────────────

def test_a_figure_the_tool_did_not_produce_is_replaced():
    result = ToolResult.success("3,250 rupees across 4 categories.", total="3,250 rupees")
    text, substituted = _guard_figures("I don't have access to that information.", result)
    assert substituted
    assert text == "3,250 rupees across 4 categories."


def test_a_correct_figure_is_left_in_the_agents_own_voice():
    result = ToolResult.success("3,250 rupees across 4 categories.", total="3,250 rupees")
    text, substituted = _guard_figures("Month to date, 3250 rupees.", result)
    assert not substituted
    assert text == "Month to date, 3250 rupees."


def test_separator_formatting_does_not_trigger_a_substitution():
    """'3,250' and '3250' are the same claim."""
    result = ToolResult.success("x", total="3,250 rupees")
    _, substituted = _guard_figures("You spent 3,250 rupees this month.", result)
    assert not substituted


def test_unconfirmed_results_are_not_guarded():
    """An ambiguous amount has no figure to enforce yet."""
    result = ToolResult.confirm("ambiguous", heard="18 rupees")
    _, substituted = _guard_figures("Was that eighteen or eighty?", result)
    assert not substituted
