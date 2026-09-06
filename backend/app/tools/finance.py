"""Vega's tools.

Every figure Vega states comes from one of the query tools here, computed by a
SQL aggregate. The model narrates these results; it never adds them up itself.
That is PRD FR-T2, and it is the difference between an assistant that is trusted
and one that deserves to be.

Categorisation is keyword-first and deterministic. "auto", "swiggy", "kirana"
carry the overwhelming majority of daily logging, and resolving them without a
model call is cheaper, faster, and reproducible across runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from app.tools.base import ToolContext, ToolResult, tool
from app.tools.money import AmountParseError, format_inr, parse_amount

UNCATEGORISED = "uncategorised"


# ── Categorisation ───────────────────────────────────────────────────────────

async def categorise(context: ToolContext, text: str) -> tuple[str, str]:
    """Return (category_slug, method).

    Longest keyword wins, so "food delivery" beats "food". Falls back to
    `uncategorised` rather than guessing — a wrong category is silently wrong in
    every future report, whereas an uncategorised row is visibly incomplete.
    """
    lowered = text.lower()
    rows = await context.session.execute(
        sa.text("SELECT slug, keywords FROM expense_categories")
    )

    best_slug, best_len = None, 0
    for row in rows:
        for keyword in row.keywords or []:
            if keyword in lowered and len(keyword) > best_len:
                best_slug, best_len = row.slug, len(keyword)

    return (best_slug, "keyword") if best_slug else (UNCATEGORISED, "fallback")


# ── Writes ───────────────────────────────────────────────────────────────────

@tool(
    name="expense_log",
    description=(
        "Record a spend. Call this whenever the user reports spending money. "
        "Pass their words verbatim as `text`; do not pre-parse the amount."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The user's own words, e.g. 'spent 250 on auto'",
            },
            "category": {
                "type": "string",
                "description": "Optional category slug override.",
            },
            "merchant": {"type": "string", "description": "Optional merchant name."},
            "confirmed_minor": {
                "type": "integer",
                "description": (
                    "Only when the user has just confirmed a previously ambiguous "
                    "amount. Integer paise."
                ),
            },
        },
        "required": ["text"],
    },
    writes=True,
)
async def expense_log(
    *,
    context: ToolContext,
    text: str,
    category: str | None = None,
    merchant: str | None = None,
    confirmed_minor: int | None = None,
) -> ToolResult:
    if confirmed_minor is not None:
        minor, needs_confirmation, alternative, reason = confirmed_minor, False, None, None
    else:
        try:
            parsed = parse_amount(text, asr_confidence=context.asr_confidence)
        except AmountParseError:
            return ToolResult.failure(
                "No amount found. Ask how much was spent; do not assume."
            )
        minor = parsed.minor
        needs_confirmation = parsed.needs_confirmation
        alternative = parsed.alternative_minor
        reason = parsed.reason

    # Ambiguous amounts are understood but not written. Confirming costs a turn;
    # a silent ₹62 error costs the user's trust in every subsequent total.
    if needs_confirmation:
        return ToolResult.confirm(
            "Amount is ambiguous — confirm before writing.",
            heard_minor=minor,
            heard=format_inr(minor),
            alternative_minor=alternative,
            alternative=format_inr(alternative) if alternative else None,
            reason=reason,
        )

    slug, method = (category, "user") if category else await categorise(context, text)

    exists = await context.session.execute(
        sa.text("SELECT 1 FROM expense_categories WHERE slug = :s"), {"s": slug}
    )
    if exists.scalar_one_or_none() is None:
        slug, method = UNCATEGORISED, "fallback"

    expense_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO expenses "
                "(amount_minor, currency, category, merchant, note, occurred_at, "
                " source_turn, categorised_by) "
                "VALUES (:minor, 'INR', :category, :merchant, :note, now(), "
                "        :turn, :method) RETURNING id"
            ),
            {
                "minor": minor,
                "category": slug,
                "merchant": merchant,
                "note": text,
                "turn": context.turn_id,
                "method": method,
            },
        )
    ).scalar_one()

    return ToolResult.success(
        f"Logged. {format_inr(minor)}, {slug}.",
        expense_id=expense_id,
        amount_minor=minor,
        amount=format_inr(minor),
        category=slug,
    )


@tool(
    name="expense_correct",
    description=(
        "Correct the most recent expense, or a specific one by id. Use when the "
        "user says something like 'actually make that 280'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The user's correction, verbatim."},
            "expense_id": {
                "type": "integer",
                "description": "Optional; defaults to the most recent live expense.",
            },
            "category": {"type": "string", "description": "Optional new category slug."},
        },
        "required": ["text"],
    },
    writes=True,
)
async def expense_correct(
    *,
    context: ToolContext,
    text: str,
    expense_id: int | None = None,
    category: str | None = None,
) -> ToolResult:
    """Write a new row and point the old one at it. Never mutate.

    History survives, and `superseded_by IS NULL` keeps totals correct.
    """
    if expense_id is None:
        expense_id = (
            await context.session.execute(
                sa.text(
                    "SELECT id FROM expenses WHERE superseded_by IS NULL "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).scalar_one_or_none()

    if expense_id is None:
        return ToolResult.failure("No expense to correct.")

    original = (
        await context.session.execute(
            sa.text(
                "SELECT amount_minor, category, merchant FROM expenses WHERE id = :i"
            ),
            {"i": expense_id},
        )
    ).one_or_none()

    if original is None:
        return ToolResult.failure(f"No expense with id {expense_id}.")

    try:
        minor = parse_amount(text, asr_confidence=context.asr_confidence).minor
    except AmountParseError:
        minor = original.amount_minor

    slug = category or original.category

    new_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO expenses "
                "(amount_minor, currency, category, merchant, note, occurred_at, "
                " source_turn, categorised_by) "
                "VALUES (:minor, 'INR', :category, :merchant, :note, now(), :turn, 'user') "
                "RETURNING id"
            ),
            {
                "minor": minor,
                "category": slug,
                "merchant": original.merchant,
                "note": text,
                "turn": context.turn_id,
            },
        )
    ).scalar_one()

    await context.session.execute(
        sa.text("UPDATE expenses SET superseded_by = :new WHERE id = :old"),
        {"new": new_id, "old": expense_id},
    )

    return ToolResult.success(
        f"Updated. {format_inr(minor)}, {slug}.",
        expense_id=new_id,
        superseded=expense_id,
        amount_minor=minor,
        amount=format_inr(minor),
    )


# ── Reads. Every number Vega speaks originates here. ─────────────────────────

def _period_start(period: str) -> datetime:
    now = datetime.now(UTC)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)  # month


_PERIOD_PARAM = {
    "type": "string",
    "enum": ["today", "week", "month", "year"],
    "description": "Defaults to the current month.",
}


@tool(
    name="expense_total",
    description=(
        "Total spend over a period, optionally for one category. Returns an exact "
        "figure computed in SQL. Quote it verbatim; never re-calculate."
    ),
    parameters={
        "type": "object",
        "properties": {
            "period": _PERIOD_PARAM,
            "category": {"type": "string", "description": "Optional category slug."},
        },
    },
)
async def expense_total(
    *, context: ToolContext, period: str = "month", category: str | None = None
) -> ToolResult:
    clauses = ["superseded_by IS NULL", "occurred_at >= :since"]
    params: dict[str, Any] = {"since": _period_start(period)}
    if category:
        clauses.append("category = :category")
        params["category"] = category

    row = (
        await context.session.execute(
            sa.text(
                "SELECT COALESCE(SUM(amount_minor), 0) AS total, COUNT(*) AS n "
                f"FROM expenses WHERE {' AND '.join(clauses)}"
            ),
            params,
        )
    ).one()

    return ToolResult.success(
        f"{format_inr(row.total)} across {row.n} entries.",
        total_minor=int(row.total),
        total=format_inr(int(row.total)),
        entry_count=int(row.n),
        period=period,
        category=category,
    )


@tool(
    name="expense_summary",
    description=(
        "Spend broken down by category for a period, largest first. Exact SQL "
        "aggregates — quote them verbatim."
    ),
    parameters={
        "type": "object",
        "properties": {"period": _PERIOD_PARAM, "limit": {"type": "integer"}},
    },
)
async def expense_summary(
    *, context: ToolContext, period: str = "month", limit: int = 8
) -> ToolResult:
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT category, SUM(amount_minor) AS total, COUNT(*) AS n "
                "FROM expenses WHERE superseded_by IS NULL AND occurred_at >= :since "
                "GROUP BY category ORDER BY total DESC LIMIT :limit"
            ),
            {"since": _period_start(period), "limit": limit},
        )
    ).all()

    breakdown = [
        {
            "category": r.category,
            "total_minor": int(r.total),
            "total": format_inr(int(r.total)),
            "entry_count": int(r.n),
        }
        for r in rows
    ]
    grand_total = sum(item["total_minor"] for item in breakdown)

    return ToolResult.success(
        f"{format_inr(grand_total)} across {len(breakdown)} categories.",
        period=period,
        total_minor=grand_total,
        total=format_inr(grand_total),
        breakdown=breakdown,
    )


@tool(
    name="expense_recent",
    description="The most recent expenses, newest first.",
    parameters={
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "Default 5."}},
    },
)
async def expense_recent(*, context: ToolContext, limit: int = 5) -> ToolResult:
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT id, amount_minor, category, merchant, occurred_at "
                "FROM expenses WHERE superseded_by IS NULL "
                "ORDER BY occurred_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()

    return ToolResult.success(
        f"{len(rows)} recent entries.",
        expenses=[
            {
                "id": r.id,
                "amount_minor": int(r.amount_minor),
                "amount": format_inr(int(r.amount_minor)),
                "category": r.category,
                "merchant": r.merchant,
                "occurred_at": r.occurred_at.isoformat(),
            }
            for r in rows
        ],
    )


@tool(
    name="expense_trace",
    description=(
        "Show the utterances behind a reported figure. Use when the user asks why "
        "a number looks wrong."
    ),
    parameters={
        "type": "object",
        "properties": {"period": _PERIOD_PARAM, "category": {"type": "string"}},
    },
)
async def expense_trace(
    *, context: ToolContext, period: str = "month", category: str | None = None
) -> ToolResult:
    """Provenance. Every derived row links to the turn that produced it, so any
    total decomposes back into the sentences the user actually said."""
    clauses = ["e.superseded_by IS NULL", "e.occurred_at >= :since"]
    params: dict[str, Any] = {"since": _period_start(period)}
    if category:
        clauses.append("e.category = :category")
        params["category"] = category

    rows = (
        await context.session.execute(
            sa.text(
                "SELECT e.id, e.amount_minor, e.category, t.transcript "
                "FROM expenses e LEFT JOIN turns t ON t.id = e.source_turn "
                f"WHERE {' AND '.join(clauses)} ORDER BY e.occurred_at DESC LIMIT 20"
            ),
            params,
        )
    ).all()

    return ToolResult.success(
        f"{len(rows)} entries traced.",
        entries=[
            {
                "id": r.id,
                "amount": format_inr(int(r.amount_minor)),
                "category": r.category,
                "said": r.transcript,
            }
            for r in rows
        ],
    )


VEGA_TOOLS: list[str] = [
    "expense_log",
    "expense_correct",
    "expense_total",
    "expense_summary",
    "expense_recent",
    "expense_trace",
]

# Astraea reports across domains but never writes a specialist's data.
ASTRAEA_FINANCE_TOOLS: list[str] = [
    "expense_total",
    "expense_summary",
    "expense_recent",
]
