"""Tier 2 — the finance domain slice. Vega's tables.

Every numeric answer Vega gives comes from a SQL aggregate over these tables,
never from a model summing retrieved text. See docs/memory-design.md §3 and
PRD FR-T2.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    slug: Mapped[str] = mapped_column(String(48), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    parent: Mapped[str | None] = mapped_column(ForeignKey("expense_categories.slug"))
    # Phrases the categoriser matches before falling back to a model call.
    # Cheap, deterministic, and covers the overwhelming majority of daily logging.
    keywords: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class Expense(Base):
    """A single spend.

    amount_minor is an integer count of paise. No float touches this path — see
    PRD FR-T1. The CHECK is > 0 because a refund is its own row with its own
    semantics, not a sign flip on an expense.

    Corrections never mutate: "make that 280" writes a new row and sets
    superseded_by on the old one. Totals filter superseded_by IS NULL.
    """

    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    category: Mapped[str] = mapped_column(
        ForeignKey("expense_categories.slug"), nullable=False
    )
    merchant: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=1.0)
    categorised_by: Mapped[str] = mapped_column(String(16), nullable=False, default="keyword")

    superseded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("expenses.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="positive_amount"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(
            "categorised_by IN ('keyword','model','user','rule')", name="valid_categorised_by"
        ),
        # Every reporting query filters on live rows in a date range; this index
        # is what keeps spend_summary fast enough for the recall budget.
        Index(
            "ix_expenses_live_occurred",
            "occurred_at",
            postgresql_where=text("superseded_by IS NULL"),
        ),
        Index(
            "ix_expenses_live_category_occurred",
            "category", "occurred_at",
            postgresql_where=text("superseded_by IS NULL"),
        ),
        Index("ix_expenses_source_turn", "source_turn"),
    )


class Budget(Base):
    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category: Mapped[str | None] = mapped_column(ForeignKey("expense_categories.slug"))
    period: Mapped[str] = mapped_column(String(12), nullable=False, default="monthly")
    limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("limit_minor > 0", name="positive_limit"),
        CheckConstraint("period IN ('weekly','monthly','yearly')", name="valid_period"),
        # NULL category == the overall budget. Only one live per category.
        Index(
            "uq_budget_live",
            "category", "period",
            unique=True,
            postgresql_where=text("effective_to IS NULL"),
        ),
    )


class Bill(Base):
    """A recurring obligation. Selene tracks that it is due; Vega tracks the money.

    Both agents legitimately hold the same bill for different reasons —
    ownership splits by question asked, not by object. See docs/agents/selene.md.
    """

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    category: Mapped[str | None] = mapped_column(ForeignKey("expense_categories.slug"))

    recurrence: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    next_due: Mapped[date | None] = mapped_column(Date, index=True)

    autopay: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "recurrence IN ('weekly','monthly','quarterly','yearly','once')",
            name="valid_recurrence",
        ),
        CheckConstraint(
            "day_of_month IS NULL OR (day_of_month BETWEEN 1 AND 31)", name="valid_day_of_month"
        ),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recurrence: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")
    next_charge: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detected_from: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    last_seen_expense: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("expenses.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="positive_amount"),
        CheckConstraint("detected_from IN ('user','pattern')", name="valid_detected_from"),
    )


class SavingsGoal(Base):
    __tablename__ = "savings_goals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    target_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    saved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    target_date: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("target_minor > 0", name="positive_target"),
        CheckConstraint("saved_minor >= 0", name="non_negative_saved"),
    )
