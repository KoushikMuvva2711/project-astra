"""Declarative cross-agent propagation rules.

This file is where "telling Vega about groceries reaches Selene and Lyra" is
literally written down. Rules live here rather than inside the curator so that
adding cross-domain behaviour is a data change, not a control-flow change.

Hints expire. A three-day-old grocery hint is noise, and noise in the context
bundle is worse than nothing — it displaces relevant material under the token
budget and teaches the agent to distrust its own context.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str
    agent: str
    payload: dict[str, Any]
    turn_id: int | None = None


@dataclass(frozen=True)
class Hint:
    target_agent: str
    kind: str
    content: str
    ttl_days: int = 3


@dataclass(frozen=True)
class Rule:
    on: str
    emit: Callable[[Event], list[Hint]]
    when: Callable[[Event], bool] = field(default=lambda _event: True)
    description: str = ""


def _groceries(event: Event) -> list[Hint]:
    return [
        Hint(
            "selene",
            "inventory_likely_replenished",
            "Groceries were bought recently — the pantry is probably restocked.",
            ttl_days=3,
        ),
        Hint(
            "lyra",
            "groceries_purchased",
            "Groceries were bought recently, so fresh food is available at home.",
            ttl_days=3,
        ),
    ]


def _food_delivery(event: Event) -> list[Hint]:
    return [
        Hint(
            "lyra",
            "meal_ordered_in",
            "A food delivery was ordered — a meal may be unlogged.",
            ttl_days=1,
        )
    ]


def _fitness_spend(event: Event) -> list[Hint]:
    return [
        Hint(
            "lyra",
            "fitness_purchase",
            "Something fitness-related was purchased recently.",
            ttl_days=7,
        )
    ]


def _large_spend(event: Event) -> list[Hint]:
    amount = event.payload.get("amount_minor", 0)
    return [
        Hint(
            "astraea",
            "large_expense",
            f"A single expense of {amount // 100} rupees was logged.",
            ttl_days=7,
        )
    ]


def _education_spend(event: Event) -> list[Hint]:
    return [
        Hint(
            "athena",
            "education_spend",
            "An education-related expense was logged — possibly an exam or course fee.",
            ttl_days=14,
        )
    ]


# Ordered only for readability; all matching rules fire.
RULES: list[Rule] = [
    Rule(
        on="expense.logged",
        when=lambda e: e.payload.get("category") == "groceries",
        emit=_groceries,
        description="Grocery spend implies a restocked pantry and available food.",
    ),
    Rule(
        on="expense.logged",
        when=lambda e: e.payload.get("category") == "food_delivery",
        emit=_food_delivery,
        description="Delivery spend implies a meal Lyra may not have been told about.",
    ),
    Rule(
        on="expense.logged",
        when=lambda e: e.payload.get("category") == "fitness",
        emit=_fitness_spend,
        description="Fitness spend is context for Lyra's programming.",
    ),
    Rule(
        on="expense.logged",
        when=lambda e: e.payload.get("category") == "education",
        emit=_education_spend,
        description="Education spend is a signal on Athena's application timeline.",
    ),
    Rule(
        on="expense.logged",
        # 5,000 rupees. Astraea prioritises; she does not need every coffee.
        when=lambda e: (e.payload.get("amount_minor") or 0) >= 500_000,
        emit=_large_spend,
        description="Unusually large single expenses are worth the coordinator's attention.",
    ),
]


def hints_for(event: Event) -> list[Hint]:
    """Every hint implied by an event. All matching rules fire."""
    hints: list[Hint] = []
    for rule in RULES:
        if rule.on != event.type:
            continue
        try:
            if rule.when(event):
                hints.extend(rule.emit(event))
        except Exception:
            # A malformed payload must not stop the curator. Skip the rule.
            continue
    return hints
