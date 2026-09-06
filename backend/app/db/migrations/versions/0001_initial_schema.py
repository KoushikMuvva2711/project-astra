"""Initial schema: episodic memory, facts, cross-agent bus, finance domain.

Scoped deliberately to what Phase 1 exercises. The remaining domain tables
(health, work, learning, home) land with their agents rather than ahead of them —
migrating schema nobody has used yet is how you end up with columns that don't
fit the real access pattern.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 768

AGENTS = "('astraea', 'lyra', 'vega', 'nova', 'athena', 'selene')"
NS = "('global', 'finance', 'health', 'work', 'learning', 'home')"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── Core ────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("locale", sa.String(16), nullable=False, server_default="en-IN"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        "agents",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("namespace", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint(f"name IN {AGENTS}", name="valid_agent_name"),
        sa.CheckConstraint(f"namespace IN {NS}", name="valid_namespace"),
    )

    op.create_table(
        "expense_categories",
        sa.Column("slug", sa.String(48), primary_key=True),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("parent", sa.String(48), sa.ForeignKey("expense_categories.slug")),
        sa.Column("keywords", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("is_essential", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="100"),
    )

    op.create_table(
        "fact_predicates",
        sa.Column("slug", sa.String(64), primary_key=True),
        sa.Column("namespace", sa.String(32), nullable=False),
        sa.Column("cardinality", sa.String(8), nullable=False),
        sa.Column("value_type", sa.String(16), nullable=False),
        sa.Column("description", sa.Text),
        sa.CheckConstraint("cardinality IN ('single','multi')",
                           name="valid_cardinality"),
        sa.CheckConstraint(
            "value_type IN ('money','number','string','date','enum','bool','json')",
            name="valid_value_type",
        ),
        sa.CheckConstraint(f"namespace IN {NS}", name="valid_namespace"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("active_agent", sa.String(32), sa.ForeignKey("agents.name"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("close_reason", sa.String(32)),
        sa.Column("channel", sa.String(16), nullable=False, server_default="voice"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "close_reason IS NULL OR close_reason IN "
            "('timeout','explicit','agent_switch','error')",
            name="valid_close_reason",
        ),
        sa.CheckConstraint("channel IN ('voice','text','shortcut','system')",
                           name="valid_channel"),
    )
    op.create_index("ix_sessions_open", "sessions", ["user_id", "last_activity_at"],
                    postgresql_where=sa.text("closed_at IS NULL"))

    op.create_table(
        "turns",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("agent", sa.String(32), sa.ForeignKey("agents.name"), nullable=False),
        sa.Column("role", sa.String(8), nullable=False),
        sa.Column("transcript", sa.Text, nullable=False),
        sa.Column("asr_confidence", sa.Numeric(4, 3)),
        sa.Column("route_confidence", sa.Numeric(4, 3)),
        sa.Column("route_method", sa.String(16)),
        sa.Column("audio_ref", sa.Text),
        sa.Column("interrupted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("tokens_in", sa.Integer),
        sa.Column("tokens_out", sa.Integer),
        sa.Column("cost_minor", sa.Integer),
        sa.Column("model_used", sa.String(96)),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('user','agent','system')", name="valid_role"),
        sa.CheckConstraint(
            "route_method IS NULL OR route_method IN "
            "('exact','phonetic','fuzzy','two_token','session','fallthrough','explicit')",
            name="valid_route_method",
        ),
    )
    op.create_index("ix_turns_session_created", "turns", ["session_id", "created_at"])
    op.create_index("ix_turns_agent_created", "turns", ["agent", "created_at"])
    op.create_index("ix_turns_created_at", "turns", ["created_at"])
    # Supports mining mis-routes for the fixture corpus (conversation-architecture.md §4.4).
    op.create_index("ix_turns_low_confidence_routes", "turns", ["route_confidence"],
                    postgresql_where=sa.text("route_confidence < 0.85"))

    op.create_table(
        "session_summaries",
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("agent", sa.String(32), sa.ForeignKey("agents.name"), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBED_DIM)),
        sa.Column("turn_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    # ── Facts and vectors ───────────────────────────────────────────────────
    op.create_table(
        "memory_facts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("namespace", sa.String(32), nullable=False),
        sa.Column("entity", sa.String(128), nullable=False, server_default="user"),
        sa.Column("predicate", sa.String(64), sa.ForeignKey("fact_predicates.slug"),
                  nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("value_text", sa.Text, nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="1.0"),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("asserted_by", sa.String(32), sa.ForeignKey("agents.name"), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("retracted_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_by", sa.BigInteger, sa.ForeignKey("memory_facts.id")),
        sa.Column("embedding", Vector(EMBED_DIM)),
        sa.CheckConstraint(f"namespace IN {NS}", name="valid_namespace"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1",
                           name="confidence_range"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from",
                           name="valid_window_ordered"),
    )
    op.create_index("ix_facts_current", "memory_facts",
                    ["namespace", "entity", "predicate"],
                    postgresql_where=sa.text("valid_to IS NULL AND retracted_at IS NULL"))
    op.create_index("ix_facts_source_turn", "memory_facts", ["source_turn"])

    op.create_table(
        "memory_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("namespace", sa.String(32), nullable=False),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("embed_model", sa.String(96), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(f"namespace IN {NS}", name="valid_namespace"),
        sa.CheckConstraint(
            "source_kind IN ('turn','session_summary','fact','document','weekly_rollup')",
            name="valid_source_kind",
        ),
    )
    op.create_index("ix_chunks_namespace_occurred", "memory_chunks",
                    ["namespace", "occurred_at"])
    op.create_index("ix_chunks_source", "memory_chunks", ["source_kind", "source_id"])
    # HNSW over cosine distance. m/ef_construction are the pgvector defaults;
    # at this corpus size recall is effectively exact and build time is trivial.
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON memory_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX ix_facts_embedding_hnsw ON memory_facts "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "memory_conflicts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("namespace", sa.String(32), nullable=False),
        sa.Column("entity", sa.String(128), nullable=False),
        sa.Column("predicate", sa.String(64), sa.ForeignKey("fact_predicates.slug"),
                  nullable=False),
        sa.Column("fact_ids", postgresql.JSONB, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("resolution", sa.String(24)),
        sa.Column("resolved_fact_id", sa.BigInteger, sa.ForeignKey("memory_facts.id")),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("surfaced_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('open','surfaced','resolved','dismissed')",
                           name="valid_state"),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN "
            "('keep_newer','keep_older','both_valid','user_supplied','dismissed')",
            name="valid_resolution",
        ),
    )
    op.create_index("ix_conflicts_open", "memory_conflicts", ["state"],
                    postgresql_where=sa.text("state = 'open'"))

    op.create_table(
        "memory_hints",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("target_agent", sa.String(32), sa.ForeignKey("agents.name"), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("source_event_id", sa.String(64)),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_hints_pending", "memory_hints", ["target_agent", "expires_at"],
                    postgresql_where=sa.text("consumed_at IS NULL"))

    op.create_table(
        "memory_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("stream_id", sa.String(64), unique=True),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("agent", sa.String(32), sa.ForeignKey("agents.name"), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("process_error", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_events_unprocessed", "memory_events", ["occurred_at"],
                    postgresql_where=sa.text("processed_at IS NULL"))
    op.create_index("ix_events_type_occurred", "memory_events", ["type", "occurred_at"])

    op.create_table(
        "cost_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("agent", sa.String(32), sa.ForeignKey("agents.name"), nullable=False),
        sa.Column("model", sa.String(96), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_minor", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_local", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_cost_ledger_occurred_at", "cost_ledger", ["occurred_at"])

    # ── Finance domain ──────────────────────────────────────────────────────
    op.create_table(
        "expenses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("category", sa.String(48), sa.ForeignKey("expense_categories.slug"),
                  nullable=False),
        sa.Column("merchant", sa.String(128)),
        sa.Column("note", sa.Text),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="1.0"),
        sa.Column("categorised_by", sa.String(16), nullable=False, server_default="keyword"),
        sa.Column("superseded_by", sa.BigInteger, sa.ForeignKey("expenses.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("amount_minor > 0", name="positive_amount"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1",
                           name="confidence_range"),
        sa.CheckConstraint("categorised_by IN ('keyword','model','user','rule')",
                           name="valid_categorised_by"),
    )
    op.create_index("ix_expenses_live_occurred", "expenses", ["occurred_at"],
                    postgresql_where=sa.text("superseded_by IS NULL"))
    op.create_index("ix_expenses_live_category_occurred", "expenses",
                    ["category", "occurred_at"],
                    postgresql_where=sa.text("superseded_by IS NULL"))
    op.create_index("ix_expenses_source_turn", "expenses", ["source_turn"])

    op.create_table(
        "budgets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("category", sa.String(48), sa.ForeignKey("expense_categories.slug")),
        sa.Column("period", sa.String(12), nullable=False, server_default="monthly"),
        sa.Column("limit_minor", sa.BigInteger, nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("limit_minor > 0", name="positive_limit"),
        sa.CheckConstraint("period IN ('weekly','monthly','yearly')",
                           name="valid_period"),
    )
    op.create_index("uq_budget_live", "budgets", ["category", "period"], unique=True,
                    postgresql_where=sa.text("effective_to IS NULL"))

    op.create_table(
        "bills",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("amount_minor", sa.BigInteger),
        sa.Column("category", sa.String(48), sa.ForeignKey("expense_categories.slug")),
        sa.Column("recurrence", sa.String(16), nullable=False, server_default="monthly"),
        sa.Column("day_of_month", sa.Integer),
        sa.Column("next_due", sa.Date),
        sa.Column("autopay", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("recurrence IN ('weekly','monthly','quarterly','yearly','once')",
                           name="valid_recurrence"),
        sa.CheckConstraint("day_of_month IS NULL OR (day_of_month BETWEEN 1 AND 31)",
                           name="valid_day_of_month"),
    )
    op.create_index("ix_bills_next_due", "bills", ["next_due"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("recurrence", sa.String(16), nullable=False, server_default="monthly"),
        sa.Column("next_charge", sa.Date),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("detected_from", sa.String(16), nullable=False, server_default="user"),
        sa.Column("last_seen_expense", sa.BigInteger, sa.ForeignKey("expenses.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("amount_minor > 0", name="positive_amount"),
        sa.CheckConstraint("detected_from IN ('user','pattern')",
                           name="valid_detected_from"),
    )

    op.create_table(
        "savings_goals",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("target_minor", sa.BigInteger, nullable=False),
        sa.Column("saved_minor", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("target_date", sa.Date),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("target_minor > 0", name="positive_target"),
        sa.CheckConstraint("saved_minor >= 0", name="non_negative_saved"),
    )


def downgrade() -> None:
    for table in (
        "savings_goals", "subscriptions", "bills", "budgets", "expenses",
        "cost_ledger", "memory_events", "memory_hints", "memory_conflicts",
        "memory_chunks", "memory_facts", "session_summaries", "turns", "sessions",
        "fact_predicates", "expense_categories", "agents", "users",
    ):
        op.drop_table(table)
    op.execute("DROP EXTENSION IF EXISTS vector")
