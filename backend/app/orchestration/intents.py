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
    r"breakdown|where did.*go|show me|"
    # "list" needs a financial object. Bare, it catches "add rice to the list"
    # and answers a grocery request with a spend total.
    r"list (?:my |the |all )?(?:expenses|spending|spends|transactions|payments))\b",
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

# Speech recognisers expand contractions as often as they keep them: the same
# question arrives as "what's left" one turn and "what is left" the next.
# Matching only the contracted form drops the utterance to the model, which then
# picks a plausible-looking tool — seen live, where "what is left" reached
# project_resume instead of task_list.
WHATS = r"what(?:'?s|\s+is|\s+are)"

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
    if agent == "selene":
        return _detect_home(text)
    if agent == "nova":
        return _detect_work(text)
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


# --------------------------------------------------------------------------- #
# Selene
# --------------------------------------------------------------------------- #

REMINDER_VERBS = re.compile(
    r"\b(remind me|set a reminder|set an alarm for|don'?t let me forget|"
    r"put a reminder)\b",
    re.IGNORECASE,
)

REMINDER_QUERY = re.compile(
    # "on" is deliberately absent: "what's on the grocery list" is not a reminder
    # query, and a bare "on" swallows it.
    rf"\b({WHATS}\s+(due|coming up|pending)|what do i have (coming|due)|"
    rf"any reminders|my reminders|{WHATS}\s+left to do|anything due)\b",
    re.IGNORECASE,
)

GROCERY_QUERY = re.compile(
    rf"\b({WHATS}\s+on (the|my) (grocery |shopping )?list|"
    r"(grocery|shopping) list|what do i need to buy|what am i out of)\b",
    re.IGNORECASE,
)

GROCERY_ADD = re.compile(
    r"\b(?:add|put)\s+(?P<item>.+?)\s+(?:to|on)\s+(?:the\s+|my\s+)?"
    r"(?:grocery|shopping)?\s*list\b",
    re.IGNORECASE,
)

NEED_TO_BUY = re.compile(
    r"\b(?:i\s+)?need (?:to (?:buy|get|pick up)\s+)?(?:some\s+)?(?P<item>.+)$",
    re.IGNORECASE,
)

RAN_OUT = re.compile(
    r"\b(?:we(?:'re| are)?|i(?:'m| am)?)?\s*(?:completely |almost )?"
    r"(?:out of|run out of|ran out of|finished (?:the|our)?|khatam)\s+(?P<item>.+)$"
    r"|\b(?P<item2>[\w\s]{2,40}?)\s+(?:is|are)\s+(?:finished|over|khatam|done|empty)\b",
    re.IGNORECASE,
)

LOW_STOCK_QUERY = re.compile(
    rf"\b({WHATS}\s+running low|running low|{WHATS}\s+low|stock check|"
    r"how much .* (do i have|is left)|do i have any)\b",
    re.IGNORECASE,
)

DOCUMENT_QUERY = re.compile(
    rf"\b({WHATS}\s+expiring|expiring soon|document.*expir|passport.*expir|"
    r"visa.*expir|when does my .* expire)\b",
    re.IGNORECASE,
)

# Words that survive item extraction but are not the item.
_ITEM_NOISE = re.compile(
    r"\b(selene|please|some|a|an|the|more|of|for|home|today|tomorrow|"
    r"grocery|shopping|list)\b",
    re.IGNORECASE,
)


def _clean_item(raw: str) -> str | None:
    item = _ITEM_NOISE.sub(" ", raw)
    item = re.sub(r"[^\w\s-]", " ", item)
    item = re.sub(r"\s+", " ", item).strip().lower()
    # Multi-word leftovers are usually a sentence, not an item. Selene's tools
    # take a noun; anything longer goes to the model rather than being guessed at.
    return item if item and len(item.split()) <= 3 else None


def _detect_home(text: str) -> Intent | None:
    """Selene's deterministic paths.

    Reminder creation is her highest-frequency turn and has one correct outcome,
    so it must not depend on the model choosing to emit a call — a fluent
    "Set, the 1st" with nothing written is the exact failure this design forbids.
    """
    # Specific verbs before broad queries. "Put milk on the shopping list"
    # contains "shopping list" and would otherwise be read as a request to hear
    # the list back rather than to add to it.
    if REMINDER_VERBS.search(text):
        return Intent(
            tool="reminder_create", arguments={"text": text}, blocks_write_tools=True
        )

    if match := GROCERY_ADD.search(text):
        if item := _clean_item(match.group("item")):
            return Intent(
                tool="grocery_add", arguments={"item": item}, blocks_write_tools=True
            )
        return None

    if GROCERY_QUERY.search(text):
        return Intent(tool="grocery_list", arguments={})

    if REMINDER_QUERY.search(text):
        return Intent(tool="reminder_list", arguments={})

    if DOCUMENT_QUERY.search(text):
        return Intent(tool="document_expiry", arguments={})

    if LOW_STOCK_QUERY.search(text):
        return Intent(tool="inventory_status", arguments={})

    if match := RAN_OUT.search(text):
        raw = match.group("item") or match.group("item2") or ""
        if item := _clean_item(raw):
            return Intent(
                tool="inventory_consume",
                arguments={"item": item, "text": text},
                blocks_write_tools=True,
            )
        return None

    if (match := NEED_TO_BUY.search(text)) and (item := _clean_item(match.group("item"))):
        return Intent(
            tool="grocery_add", arguments={"item": item}, blocks_write_tools=True
        )

    return None


# --------------------------------------------------------------------------- #
# Nova
# --------------------------------------------------------------------------- #

# NOV-01. "Where was I" is Nova's defining turn and the one she has already been
# caught answering from imagination — with zero projects on file she described an
# authentication module and a commit history to match. Running project_resume
# deterministically means the answer starts from a row or from an explicit "no
# projects on record", never from a blank context the model fills in.
RESUME_QUERY = re.compile(
    r"\b(where (was|were) (i|we)|where did (i|we) leave|continue where|"
    r"pick up where|resume|what was i (working|doing)|what were we (working|doing)|"
    rf"catch me up|where are we (on|with)|status on|{WHATS}\s+the state of)\b",
    re.IGNORECASE,
)

PROJECT_QUERY = re.compile(
    r"\b(what projects|my projects|list (my )?projects|which projects|"
    rf"what am i building|{WHATS}\s+active)\b",
    re.IGNORECASE,
)

TASK_QUERY = re.compile(
    rf"\b({WHATS}\s+(left|open|next|on my plate)|my tasks|list (my )?tasks|"
    rf"what do i need to do|{WHATS}\s+outstanding|{WHATS}\s+blocked|open tasks|"
    r"todo list|to-do list)\b",
    re.IGNORECASE,
)

def _detect_work(text: str) -> Intent | None:
    """Nova's deterministic paths.

    Only the read side is deterministic. Task creation and completion carry more
    ambiguity than an expense or a meal — "I should refactor the parser" is as
    often thinking aloud as it is a task — so those stay with the model, which
    can ask. The reads are where fabrication happens, and reads are safe to force.
    """
    if RESUME_QUERY.search(text):
        return Intent(tool="project_resume", arguments={})

    if PROJECT_QUERY.search(text):
        return Intent(tool="project_list", arguments={})

    if TASK_QUERY.search(text):
        arguments: dict[str, Any] = {}
        if re.search(r"\bblocked\b", text, re.IGNORECASE):
            arguments["state"] = "blocked"
        return Intent(tool="task_list", arguments=arguments)

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
