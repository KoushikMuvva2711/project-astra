"""Tier 1 — episodic memory. Conversations, sessions, turns.

The append-only source of truth. Tiers 2 and 3 are derived from this and are
rebuildable by reprocessing it, which is why turns are never mutated and why
every derived row elsewhere carries a source_turn_id.

See docs/memory-design.md §2.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db.base import Base, TimestampMixin

EMBED_DIM = get_settings().embed_dim

AGENT_NAMES = ("astraea", "lyra", "vega", "nova", "athena", "selene")
NAMESPACES = ("global", "finance", "health", "work", "learning", "home")


class User(Base, TimestampMixin):
    """Single-tenant, but modelled explicitly so nothing hardcodes an implicit user."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en-IN")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")


class Agent(Base):
    """Agent registry. Configuration lives in code (docs/agents/*.md → AgentSpec);
    this table exists for referential integrity and per-agent runtime state."""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint(f"name IN {AGENT_NAMES}", name="valid_agent_name"),
        CheckConstraint(f"namespace IN {NAMESPACES}", name="valid_namespace"),
    )


class Session(Base, TimestampMixin):
    """A contiguous stretch of interaction with one focused agent.

    Opens on a resolved wake-name, closes on timeout, explicit end, or a
    different wake-name. See conversation-architecture.md §2.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    active_agent: Mapped[str] = mapped_column(ForeignKey("agents.name"), nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(32))

    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="voice")

    turns: Mapped[list["Turn"]] = relationship(back_populates="session")

    __table_args__ = (
        CheckConstraint(
            "close_reason IS NULL OR close_reason IN "
            "('timeout','explicit','agent_switch','error')",
            name="valid_close_reason",
        ),
        CheckConstraint("channel IN ('voice','text','shortcut','system')", name="valid_channel"),
        # Partial index: "is there an open session" is the hottest query on this
        # table and runs on every turn.
        Index("ix_sessions_open", "user_id", "last_activity_at",
              postgresql_where=("closed_at IS NULL")),
    )


class Turn(Base):
    """One utterance or one response. Immutable once written.

    route_confidence and route_method are stored so routing failures can be
    mined from production and fed back into the fixture corpus
    (conversation-architecture.md §4.4). This is the feedback loop that keeps
    wake-name accuracy from silently degrading.
    """

    __tablename__ = "turns"

    # BigInteger explicitly: Mapped[int] would infer Integer and cap the turn log
    # at ~2.1 billion rows, and every source_turn FK must match this width.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    agent: Mapped[str] = mapped_column(ForeignKey("agents.name"), nullable=False)
    role: Mapped[str] = mapped_column(String(8), nullable=False)

    transcript: Mapped[str] = mapped_column(Text, nullable=False)
    asr_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    route_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    route_method: Mapped[str | None] = mapped_column(String(16))

    audio_ref: Mapped[str | None] = mapped_column(Text)
    interrupted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_minor: Mapped[int | None] = mapped_column(Integer)
    model_used: Mapped[str | None] = mapped_column(String(96))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    session: Mapped[Session] = relationship(back_populates="turns")

    __table_args__ = (
        CheckConstraint("role IN ('user','agent','system')", name="valid_role"),
        CheckConstraint(
            "route_method IS NULL OR route_method IN "
            "('exact','phonetic','fuzzy','two_token','session','fallthrough','explicit')",
            name="valid_route_method",
        ),
        Index("ix_turns_session_created", "session_id", "created_at"),
        Index("ix_turns_agent_created", "agent", "created_at"),
        # Supports mining low-confidence routes for the fixture corpus.
        Index("ix_turns_low_confidence_routes", "route_confidence",
              postgresql_where=("route_confidence < 0.85")),
    )


class SessionSummary(Base):
    """2–4 sentence summary written on session close, embedded for recall.

    Retrieval hits summaries first and drills into raw turns only when a summary
    looks relevant — this is what keeps recall cost roughly constant as history
    grows. See docs/memory-design.md §2.
    """

    __tablename__ = "session_summaries"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id"), primary_key=True
    )
    agent: Mapped[str] = mapped_column(ForeignKey("agents.name"), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
