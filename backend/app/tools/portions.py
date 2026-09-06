"""Parsing spoken meals into food items and quantities.

"Two rotis, dal, and a bowl of curd" has to become three rows without asking the
user to weigh anything. Nobody says "sixty grams of roti", so grams are never the
input unit — servings are, and the quantity is stored as tenths of a serving so
"one and a half rotis" is the integer 15 rather than a float.

Whatever cannot be matched is still recorded, marked estimated, with zero macros
rather than invented ones. An unrecognised food should make the day's totals
visibly incomplete, not quietly wrong — the same reason an unmatched expense is
`uncategorised` instead of a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Tenths of a serving. Speech is full of fractional portions.
WORD_QUANTITIES: dict[str, int] = {
    "a": 10, "an": 10, "one": 10, "single": 10,
    "two": 20, "double": 20, "couple": 20, "few": 30,
    "three": 30, "four": 40, "five": 50, "six": 60,
    "seven": 70, "eight": 80, "nine": 90, "ten": 100,
    "half": 5, "quarter": 3,
}

# Container words that mean "one standard serving" and carry no food meaning.
PORTION_WORDS = frozenset(
    {
        "katori", "katoris", "bowl", "bowls", "plate", "plates", "glass",
        "glasses", "cup", "cups", "scoop", "scoops", "piece", "pieces",
        "slice", "slices", "serving", "servings", "helping", "helpings",
        "tsp", "tbsp", "spoon", "spoons",
    }
)

FILLER = frozenset(
    {
        "i", "had", "ate", "have", "eating", "just", "some", "with", "and",
        "of", "for", "my", "the", "a", "an", "then", "also", "plus", "today",
        "breakfast", "lunch", "dinner", "snack", "meal", "at", "in", "on",
        # Vague placeholders. Without these, "I had something" parses
        # "something" as an unknown food and writes a zero-macro meal instead of
        # asking what was actually eaten.
        "something", "stuff", "things", "thing", "food", "anything", "it",
    }
)

# Articles that follow a fraction are grammar, not a second quantity:
# "half a paratha" is 0.5 servings, not 1.5.
ARTICLES = frozenset({"a", "an"})

MEAL_KINDS = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snack",
    "supper": "dinner",
}

_SPLIT = re.compile(r"[,;]|\band\b|\bplus\b|\bwith\b", re.IGNORECASE)
_WORD = re.compile(r"[a-z]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ParsedItem:
    raw_text: str
    food_name: str
    quantity_dc: int


def detect_meal_kind(text: str) -> str:
    lowered = text.lower()
    for word, kind in MEAL_KINDS.items():
        if word in lowered:
            return kind
    return "meal"


def _quantity_from(tokens: list[str]) -> tuple[int, list[str]]:
    """Pull a leading quantity off a fragment. Returns (tenths, remaining tokens)."""
    if not tokens:
        return 10, tokens

    total = 0
    index = 0
    saw_fraction = False

    while index < len(tokens):
        token = tokens[index]
        if match := _NUMBER.fullmatch(token):
            value = float(match.group(0))
            # Integer tenths without float arithmetic leaking into storage.
            total += int(round(value * 10))
            index += 1
        elif token in ARTICLES and saw_fraction:
            # "half a paratha": the article is grammar, not another serving.
            index += 1
        elif token in WORD_QUANTITIES:
            amount = WORD_QUANTITIES[token]
            total += amount
            saw_fraction = amount < 10
            index += 1
        else:
            break

    return (total or 10), tokens[index:]


def parse_meal(text: str) -> list[ParsedItem]:
    """Split an utterance into (food, quantity) pairs.

    Returns an empty list when nothing food-like is present, so the caller can
    ask rather than write an empty meal.
    """
    items: list[ParsedItem] = []

    for fragment in _SPLIT.split(text.lower()):
        fragment = fragment.strip()
        if not fragment:
            continue

        tokens = re.findall(r"[a-z]+|\d+(?:\.\d+)?", fragment)
        if not tokens:
            continue

        # Strip filler *before* reading the quantity: in "I had two eggs" the
        # leading "I" would otherwise stop quantity parsing at the first token
        # and leave "two" stuck to the food name. Quantity words survive even
        # when they are also filler ("a").
        candidates = [
            token
            for token in tokens
            if token not in FILLER or token in WORD_QUANTITIES
        ]

        quantity, rest = _quantity_from(candidates)

        # Drop container words and any remaining filler; what is left is food.
        food_tokens = [
            token
            for token in rest
            if token not in PORTION_WORDS and token not in FILLER
        ]
        if not food_tokens:
            continue

        items.append(
            ParsedItem(
                raw_text=fragment,
                food_name=" ".join(food_tokens),
                quantity_dc=quantity,
            )
        )

    return items
