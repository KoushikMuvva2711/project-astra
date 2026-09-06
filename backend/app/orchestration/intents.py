"""Deterministic intent detection: act first, let the model narrate.

Why this exists. Expense logging is the highest-frequency interaction in the
system and the least ambiguous — "spent 250 on auto" has exactly one correct
outcome. Making that outcome depend on a language model choosing to emit a tool
call is fragile in a specific and dangerous way: when the call is skipped, the
model still produces a fluent confirmation, so the user is told the spend was
recorded when nothing was written. That was observed directly on a small local
model, and it violates the one invariant the whole design rests on — never
confirm a write that did not happen.

So for utterances that are unambiguous, the tool runs first, deterministically,
and the model's only job is to phrase the result it is handed. That is faster
(no extra round trip to decide the obvious), cheaper, reproducible across runs,
and correct regardless of how weak the model is. It is also exactly the
"cheap path for high-frequency, low-nuance turns" lever the PRD's latency
budget calls for.

The model still handles everything that needs judgement. This covers only the
cases where judgement adds nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.tools.money import AmountParseError, parse_amount

# Verbs that mark an utterance as reporting a spend rather than asking about one.
SPEND_VERBS = re.compile(
    r"\b(spent|spend|paid|pay|bought|buy|cost|costs|charged|gave|"
    r"kharch|diya)\b",
    re.IGNORECASE,
)

# Phrasings that ask about spending. Checked first — "how much did I spend"
# contains a spend verb but is a question, not a log.
QUERY_PHRASES = re.compile(
    r"\b(how much|what did i spend|what have i spent|total|summary|report|"
    r"breakdown|where did.*go|show me|list)\b",
    re.IGNORECASE,
)

CORRECTION_PHRASES = re.compile(
    r"\b(actually|make that|correct that|change that|no it was|i meant|sorry it was)\b",
    re.IGNORECASE,
)

# Speech routinely drops the verb: "180 on coffee", "250 for the auto". An amount
# followed by on/for and a noun is a spend report even with no verb present.
AMOUNT_THEN_TARGET = re.compile(
    r"\d[\d,.]*\s*(?:k|lakh|lakhs|crore|crores)?\s+(?:on|for)\s+\w+", re.IGNORECASE
)

# Amounts that are being *stated*, not spent. Without this, "my budget is 5000"
# and "salary is 90000" would both be logged as expenses.
NON_SPEND_CONTEXT = re.compile(
    r"\b(budget|target|limit|salary|income|balance|saved|savings|goal|"
    r"worth|earn|earning|rent is|costs about)\b",
    re.IGNORECASE,
)

PERIODS = {
    "today": "today",
    "this week": "week",
    "week": "week",
    "this month": "month",
    "month": "month",
    "this year": "year",
    "year": "year",
}


@dataclass(frozen=True)
class Intent:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    blocks_write_tools: bool = False
    """True when this intent already performed the turn's write, so the model
    must not be offered write tools again and cannot double-log."""


def _period_from(text: str) -> str:
    lowered = text.lower()
    for phrase, period in PERIODS.items():
        if phrase in lowered:
            return period
    return "month"


# Agents that may take the deterministic finance path. Astraea gets reads only —
# she reports across domains but never writes a specialist's data.
_FINANCE_READERS = {"vega", "astraea"}
_FINANCE_WRITERS = {"vega"}


MEAL_VERBS = re.compile(
    r"\b(ate|eaten|had|having|log this meal|for breakfast|for lunch|for dinner|"
    r"khaya|snacked)\b",
    re.IGNORECASE,
)

WORKOUT_VERBS = re.compile(
    r"\b(worked out|workout|trained|training|gym|lifted|ran|running|cycled|"
    r"swam|did legs|did push|did pull|leg day|push day|pull day)\b",
    re.IGNORECASE,
)

# Routing heuristic only — NOT the source of truth for nutrition.
#
# Speech drops the verb constantly: "two rotis and dal" is a complete meal log.
# Detecting that needs to know a food when it sees one, but intent detection runs
# before any database access, so a small static set of the most common staples
# stands in. It must remain a subset of the `foods` table; anything it misses
# still reaches the model, and anything it wrongly admits is caught by meal_log,
# which records unmatched items rather than inventing macros for them.
COMMON_FOODS = re.compile(
    r"\b(roti|rotis|chapati|chapatis|rice|dal|daal|curd|dahi|egg|eggs|paneer|"
    r"chicken|idli|idlis|dosa|dosas|poha|upma|sambar|rajma|chole|milk|banana|"
    r"paratha|parathas|omelette|omelet|oats|salad|sabzi|khichdi|biryani|"
    r"protein shake|whey|almonds|peanuts|apple|toast|bread)\b",
    re.IGNORECASE,
)

MACRO_QUERY = re.compile(
    r"\b(how much protein|how many calories|macros|calorie count|"
    r"protein today|am i on target)\b",
    re.IGNORECASE,
)


def detect(agent: str, text: str) -> Intent | None:
    """Return a deterministic action for this utterance, or None to let the model decide."""
    if not text.strip():
        return None
    if agent == "lyra":
        return _detect_health(text)
    if agent not in _FINANCE_READERS:
        return None

    # Corrections before logs: "actually make that 280" contains an amount and
    # would otherwise read as a fresh spend, double-counting the entry.
    if agent in _FINANCE_WRITERS and CORRECTION_PHRASES.search(text):
        try:
            parse_amount(text)
        except AmountParseError:
            return None
        return Intent(
            tool="expense_correct",
            arguments={"text": text},
            blocks_write_tools=True,
        )

    # Questions before logs: "how much did I spend on coffee" has a spend verb.
    if QUERY_PHRASES.search(text):
        category = _category_hint(text)
        if any(word in text.lower() for word in ("breakdown", "summary", "categor")):
            return Intent(tool="expense_summary", arguments={"period": _period_from(text)})
        arguments: dict[str, Any] = {"period": _period_from(text)}
        if category:
            arguments["category"] = category
        return Intent(tool="expense_total", arguments=arguments)

    # Only Vega writes. For Astraea everything past the query branch is
    # conversation, not a logging opportunity.
    if agent not in _FINANCE_WRITERS:
        return None

    # Amounts that are stated rather than spent stay with the model.
    if NON_SPEND_CONTEXT.search(text):
        return None

    looks_like_spend = bool(SPEND_VERBS.search(text)) or bool(AMOUNT_THEN_TARGET.search(text))
    if not looks_like_spend:
        return None

    try:
        parse_amount(text)
    except AmountParseError:
        # A spend verb with no amount is a real conversation ("I spent too much
        # this month"). Let the model handle it.
        return None

    return Intent(tool="expense_log", arguments={"text": text}, blocks_write_tools=True)


def _detect_health(text: str) -> Intent | None:
    """Lyra's deterministic paths.

    Meal logging has the same property as expense logging: "two rotis and dal"
    has one correct outcome, and leaving it to the model risks a confident
    confirmation with nothing written.
    """
    if MACRO_QUERY.search(text):
        days_back = 1 if "yesterday" in text.lower() else 0
        return Intent(tool="macro_totals", arguments={"days_back": days_back})

    if WORKOUT_VERBS.search(text):
        return Intent(
            tool="workout_log", arguments={"text": text}, blocks_write_tools=True
        )

    if MEAL_VERBS.search(text) or COMMON_FOODS.search(text):
        # Only if something food-like survives parsing; "I had a rough day" is
        # conversation, not a meal.
        from app.tools.portions import parse_meal

        if parse_meal(text):
            return Intent(
                tool="meal_log", arguments={"text": text}, blocks_write_tools=True
            )

    return None


_CATEGORY_HINTS = {
    "coffee": "dining_out",
    "chai": "dining_out",
    "grocer": "groceries",
    "auto": "auto_taxi",
    "uber": "auto_taxi",
    "ola": "auto_taxi",
    "petrol": "fuel",
    "fuel": "fuel",
    "rent": "rent",
    "swiggy": "food_delivery",
    "zomato": "food_delivery",
    "delivery": "food_delivery",
    "gym": "fitness",
}


def _category_hint(text: str) -> str | None:
    lowered = text.lower()
    for needle, slug in _CATEGORY_HINTS.items():
        if needle in lowered:
            return slug
    return None
