"""Tier 2 — Selene's domain: reminders, inventory, documents, warranties, trips."""

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
    String,
    Text,
    func,
)

# Aliased: Reminder has a column named `text`, which shadows sqlalchemy.text
# inside the class body and turns any use of it into a TypeError.
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Reminder(Base):
    """`notified_at` is what enforces "remind once, well".

    Selene's spec forbids nagging, and a muted Selene is a dead Selene. Firing is
    gated on this column rather than on the agent remembering not to repeat.
    """

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recurrence: Mapped[str | None] = mapped_column(String(16))
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    location_trigger: Mapped[str | None] = mapped_column(String(96))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','notified','completed','cancelled')", name="valid_state"
        ),
        CheckConstraint(
            "recurrence IS NULL OR recurrence IN "
            "('daily','weekly','monthly','quarterly','yearly')",
            name="valid_recurrence",
        ),
        Index("ix_reminders_due", "due_at", postgresql_where=sql("state = 'pending'")),
    )


class InventoryItem(Base):
    """`last_purchased_at` and `typical_days_between` are what let Selene say
    "you bought rice eleven days ago — running out already?". Storing a list is
    easy; the purchase history is what makes it worth asking."""

    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    category: Mapped[str | None] = mapped_column(String(48))
    quantity_dc: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    unit: Mapped[str] = mapped_column(String(24), nullable=False, default="unit")
    low_threshold_dc: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    last_purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    typical_days_between: Mapped[int | None] = mapped_column(Integer)
    on_grocery_list: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("quantity_dc >= 0", name="non_negative_quantity"),
        Index(
            "ix_inventory_low",
            "name",
            postgresql_where=sql("quantity_dc <= low_threshold_dc"),
        ),
    )


class Document(Base):
    """Tracks that a document exists and when it expires — never its number.

    Selene's spec says she is discreet with documents. The strongest form of that
    is having nowhere to put an identifier: a column that does not exist cannot be
    read aloud within earshot of someone else.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    label: Mapped[str] = mapped_column(String(96), nullable=False)
    expires_on: Mapped[date | None] = mapped_column(Date)
    location_note: Mapped[str | None] = mapped_column(Text)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_documents_expiry", "expires_on"),)


class Warranty(Base):
    __tablename__ = "warranties"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item: Mapped[str] = mapped_column(String(96), nullable=False)
    purchased_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))

    __table_args__ = (Index("ix_warranties_expiry", "expires_on"),)


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    destination: Mapped[str] = mapped_column(String(96), nullable=False)
    departs_on: Mapped[date | None] = mapped_column(Date)
    returns_on: Mapped[date | None] = mapped_column(Date)
    packing_list: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
