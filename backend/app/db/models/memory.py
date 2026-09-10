"""Tier 3 — facts, plus the vector index, conflict queue, and cross-agent hints.

See docs/memory-design.md §4–§7.
"""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.config import get_settings
from app.db.base import Base
from app.db.models.core import NAMESPACES

EMBED_DIM = get_settings().embed_dim


class FactPredicate(Base):
    """Predicate registry with declared cardinality.

    Cardinality is what makes conflict detection mechanical rather than a
    judgement call. `monthly_rent` is single — two open values is a contradiction
    by definition. `dietary_restriction` is multi — several open values is normal.

    Without this declaration the detector either misses real conflicts or nags
    about non-conflicts, and the second trains the user to ignore it.
    """

    __tablename__ = "fact_predicates"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(32), nullable=False)
    cardinality: Mapped[str] = mapped_column(String(8), nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("cardinality IN ('single','multi')", name="valid_cardinality"),
        CheckConstraint(
            "value_type IN ('money','number','string','date','enum','bool','json')",
            name="valid_value_type",
        ),
        CheckConstraint(f"namespace IN {NAMESPACES}", name="valid_namespace"),
    )


class MemoryFact(Base):
    """A durable assertion, bitemporal.

    Two time axes, and they come apart constantly:

      valid_from / valid_to        when the fact was true in the world
      recorded_at / retracted_at   when the system believed it

    "My rent went up to 18,000 last month", said in March: valid time starts in
    February, transaction time starts in March. One axis forces a choice between
    a false statement and losing the ability to explain why January's budget
    looked wrong at the time. See docs/memory-design.md §4.

    Nothing is deleted. Corrections close valid_to. Mistakes set retracted_at.
    """

    __tablename__ = "memory_facts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(32), nullable=False)
    entity: Mapped[str] = mapped_column(String(128), nullable=False, default="user")
    predicate: Mapped[str] = mapped_column(
        ForeignKey("fact_predicates.slug"), nullable=False
    )
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    value_text: Mapped[str] = mapped_column(Text, nullable=False)  # embedded/searchable form

    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=1.0)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    asserted_by: Mapped[str] = mapped_column(ForeignKey("agents.name"), nullable=False)

    # valid time
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # transaction time
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    superseded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("memory_facts.id"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))

    __table_args__ = (
        CheckConstraint(f"namespace IN {NAMESPACES}", name="valid_namespace"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="valid_window_ordered"
        ),
        # The hot path: current facts for one namespace. Partial index keeps it
        # small regardless of how much history accumulates.
        Index(
            "ix_facts_current",
            "namespace", "entity", "predicate",
            postgresql_where=text("valid_to IS NULL AND retracted_at IS NULL"),
        ),
        Index("ix_facts_source_turn", "source_turn"),
        Index(
            "ix_facts_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class MemoryChunk(Base):
    """Vector index over episodic and fact content.

    Kept separate from the source rows so embeddings can be regenerated wholesale
    when the embedding model changes, without touching the source of truth.
    """

    __tablename__ = "memory_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(32), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=False)
    embed_model: Mapped[str] = mapped_column(String(96), nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(f"namespace IN {NAMESPACES}", name="valid_namespace"),
        CheckConstraint(
            "source_kind IN ('turn','session_summary','fact','document','weekly_rollup')",
            name="valid_source_kind",
        ),
        Index("ix_chunks_namespace_occurred", "namespace", "occurred_at"),
        Index("ix_chunks_source", "source_kind", "source_id"),
        # HNSW over cosine distance. Declared here as well as in the migration so
        # autogenerate can see it — an index created only by raw SQL looks like
        # drift and gets proposed for deletion on the next diff.
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class MemoryConflict(Base):
    """A detected contradiction, queued for Astraea's daily briefing.

    Deliberately not surfaced mid-conversation — interrupting an expense log to
    adjudicate a rent discrepancy is the wrong moment. See memory-design.md §7.
    """

    __tablename__ = "memory_conflicts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(32), nullable=False)
    entity: Mapped[str] = mapped_column(String(128), nullable=False)
    predicate: Mapped[str] = mapped_column(ForeignKey("fact_predicates.slug"), nullable=False)
    fact_ids: Mapped[list] = mapped_column(JSONB, nullable=False)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    resolution: Mapped[str | None] = mapped_column(String(24))
    resolved_fact_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("memory_facts.id"))

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    surfaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("state IN ('open','surfaced','resolved','dismissed')", name="valid_state"),
        CheckConstraint(
            "resolution IS NULL OR resolution IN "
            "('keep_newer','keep_older','both_valid','user_supplied','dismissed')",
            name="valid_resolution",
        ),
        Index("ix_conflicts_open", "state", postgresql_where=text("state = 'open'")),
    )


class MemoryHint(Base):
    """Cross-agent context emitted by the curator's propagation rules.

    An expense logged to Vega produces a hint for Selene (inventory) and Lyra
    (food available) without either agent being invoked. Hints expire — a
    three-day-old grocery hint is noise — and are consumed once read into a
    context bundle. See memory-design.md §7.
    """

    __tablename__ = "memory_hints"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    target_agent: Mapped[str] = mapped_column(ForeignKey("agents.name"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    source_event_id: Mapped[str | None] = mapped_column(String(64))
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_hints_pending",
            "target_agent", "expires_at",
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )


class MemoryEvent(Base):
    """Durable log of the cross-agent event bus.

    Redis Streams is the live transport; this is the audit trail and the replay
    source if the curator's cursor is ever lost.
    """

    __tablename__ = "memory_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stream_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    agent: Mapped[str] = mapped_column(ForeignKey("agents.name"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    process_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_events_unprocessed", "occurred_at",
              postgresql_where=text("processed_at IS NULL")),
        Index("ix_events_type_occurred", "type", "occurred_at"),
    )


class CostLedger(Base):
    """Per-call cloud spend, for the monthly ceiling and the per-agent breakdown."""

    __tablename__ = "cost_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent: Mapped[str] = mapped_column(ForeignKey("agents.name"), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_local: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
