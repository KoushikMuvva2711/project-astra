"""Domain tables for Lyra, Selene, Nova, and Athena.

Deliberately held back from 0001 until the agents that use them were being
built — schema written ahead of its access pattern acquires columns nobody
queries and misses the ones everybody does.

Same invariants as the finance slice: integers not floats for anything
countable, `source_turn` provenance on every row a conversation produced, and
supersession instead of mutation wherever a value can be corrected.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Lyra: health ────────────────────────────────────────────────────────
    op.create_table(
        "foods",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(96), nullable=False, unique=True),
        sa.Column("aliases", postgresql.JSONB, nullable=False, server_default="[]"),
        # Per standard serving, not per 100g: people say "two rotis", never
        # "sixty grams of roti". Grams are derivable; servings are what is spoken.
        sa.Column("serving_label", sa.String(48), nullable=False),
        sa.Column("serving_grams", sa.Integer),
        sa.Column("kcal", sa.Integer, nullable=False),
        sa.Column("protein_dg", sa.Integer, nullable=False, server_default="0"),
        sa.Column("carbs_dg", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fat_dg", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_indian", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("kcal >= 0", name="non_negative_kcal"),
        sa.CheckConstraint(
            "protein_dg >= 0 AND carbs_dg >= 0 AND fat_dg >= 0", name="non_negative_macros"
        ),
    )

    op.create_table(
        "meals",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="meal"),
        sa.Column("note", sa.Text),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("superseded_by", sa.BigInteger, sa.ForeignKey("meals.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('breakfast','lunch','dinner','snack','meal')", name="valid_kind"
        ),
    )
    op.create_index("ix_meals_live_occurred", "meals", ["occurred_at"],
                    postgresql_where=sa.text("superseded_by IS NULL"))

    op.create_table(
        "meal_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("meal_id", sa.BigInteger, sa.ForeignKey("meals.id"), nullable=False),
        sa.Column("food_id", sa.BigInteger, sa.ForeignKey("foods.id")),
        sa.Column("raw_text", sa.String(128), nullable=False),
        # Tenths of a serving, integer: "one and a half rotis" is 15, not 1.5.
        sa.Column("quantity_dc", sa.Integer, nullable=False, server_default="10"),
        sa.Column("kcal", sa.Integer, nullable=False, server_default="0"),
        sa.Column("protein_dg", sa.Integer, nullable=False, server_default="0"),
        sa.Column("carbs_dg", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fat_dg", sa.Integer, nullable=False, server_default="0"),
        sa.Column("estimated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint("quantity_dc > 0", name="positive_quantity"),
    )
    op.create_index("ix_meal_items_meal", "meal_items", ["meal_id"])

    op.create_table(
        "workouts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("duration_min", sa.Integer),
        sa.Column("perceived_effort", sa.Integer),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("external_ref", sa.String(96)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "perceived_effort IS NULL OR perceived_effort BETWEEN 1 AND 10",
            name="valid_effort",
        ),
    )
    op.create_index("ix_workouts_occurred", "workouts", ["occurred_at"])

    op.create_table(
        "exercise_sets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("workout_id", sa.BigInteger, sa.ForeignKey("workouts.id"), nullable=False),
        sa.Column("exercise", sa.String(64), nullable=False),
        sa.Column("set_index", sa.Integer, nullable=False),
        sa.Column("reps", sa.Integer),
        # Grams, integer: 62.5 kg is 62500. Progression maths must stay exact.
        sa.Column("load_g", sa.BigInteger),
        sa.CheckConstraint("set_index > 0", name="positive_set_index"),
        sa.CheckConstraint("reps IS NULL OR reps > 0", name="positive_reps"),
    )
    op.create_index("ix_sets_workout", "exercise_sets", ["workout_id"])
    op.create_index("ix_sets_exercise", "exercise_sets", ["exercise"])

    op.create_table(
        "health_samples",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("value_milli", sa.BigInteger, nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(32), nullable=False, server_default="shortcuts"),
        # Apple Health re-sends overlapping windows; this makes ingestion idempotent.
        sa.Column("external_id", sa.String(96), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "metric IN ('steps','heart_rate','hrv','sleep','active_energy',"
            "'resting_hr','weight','vo2max','respiratory_rate')",
            name="valid_metric",
        ),
    )
    op.create_index("ix_health_metric_time", "health_samples", ["metric", "started_at"])

    op.create_table(
        "body_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("weight_g", sa.BigInteger),
        sa.Column("body_fat_pct_d", sa.Integer),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.CheckConstraint("weight_g IS NULL OR weight_g > 0", name="positive_weight"),
    )
    op.create_index("ix_body_measured", "body_metrics", ["measured_at"])

    # ── Selene: home ────────────────────────────────────────────────────────
    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("recurrence", sa.String(16)),
        sa.Column("day_of_month", sa.Integer),
        sa.Column("location_trigger", sa.String(96)),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        # Selene reminds once and trusts it landed. This is what enforces that.
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('pending','notified','completed','cancelled')", name="valid_state"
        ),
        sa.CheckConstraint(
            "recurrence IS NULL OR recurrence IN "
            "('daily','weekly','monthly','quarterly','yearly')",
            name="valid_recurrence",
        ),
    )
    op.create_index("ix_reminders_due", "reminders", ["due_at"],
                    postgresql_where=sa.text("state = 'pending'"))

    op.create_table(
        "inventory_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(96), nullable=False, unique=True),
        sa.Column("category", sa.String(48)),
        sa.Column("quantity_dc", sa.Integer, nullable=False, server_default="10"),
        sa.Column("unit", sa.String(24), nullable=False, server_default="unit"),
        sa.Column("low_threshold_dc", sa.Integer, nullable=False, server_default="2"),
        sa.Column("last_purchased_at", sa.DateTime(timezone=True)),
        sa.Column("typical_days_between", sa.Integer),
        sa.Column("on_grocery_list", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("quantity_dc >= 0", name="non_negative_quantity"),
    )
    op.create_index("ix_inventory_low", "inventory_items", ["name"],
                    postgresql_where=sa.text("quantity_dc <= low_threshold_dc"))

    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("label", sa.String(96), nullable=False),
        # Deliberately no number/identifier column. Selene tracks that a passport
        # expires in March; she has no business holding the passport number, and
        # a column that does not exist cannot be read aloud by accident.
        sa.Column("expires_on", sa.Date),
        sa.Column("location_note", sa.Text),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_documents_expiry", "documents", ["expires_on"])

    op.create_table(
        "warranties",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("item", sa.String(96), nullable=False),
        sa.Column("purchased_on", sa.Date),
        sa.Column("expires_on", sa.Date),
        sa.Column("note", sa.Text),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
    )
    op.create_index("ix_warranties_expiry", "warranties", ["expires_on"])

    op.create_table(
        "trips",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("destination", sa.String(96), nullable=False),
        sa.Column("departs_on", sa.Date),
        sa.Column("returns_on", sa.Date),
        sa.Column("packing_list", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
    )

    # ── Nova: work ──────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("repo_path", sa.Text),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_worked_at", sa.DateTime(timezone=True)),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active','paused','done','abandoned')", name="valid_status"
        ),
    )
    op.create_index("ix_projects_recent", "projects", ["last_worked_at"],
                    postgresql_where=sa.text("status = 'active'"))

    # The resumption substrate. NOV-01 and NOV-08 are answered entirely from
    # this table: without next_step recorded at close, resumption is guesswork.
    op.create_table(
        "project_sessions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.BigInteger, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("completed", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("in_flight", sa.Text),
        sa.Column("blocked_on", sa.Text),
        sa.Column("next_step", sa.Text),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_project_sessions_recent", "project_sessions",
                    ["project_id", "recorded_at"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.BigInteger, sa.ForeignKey("projects.id")),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("state", sa.String(16), nullable=False, server_default="todo"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="3"),
        sa.Column("estimate_min", sa.Integer),
        sa.Column("blocked_reason", sa.Text),
        sa.Column("due_on", sa.Date),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('todo','doing','blocked','done','dropped')", name="valid_state"
        ),
        sa.CheckConstraint("priority BETWEEN 1 AND 5", name="valid_priority"),
    )
    op.create_index("ix_tasks_open", "tasks", ["project_id", "priority"],
                    postgresql_where=sa.text("state IN ('todo','doing','blocked')"))

    # ── Athena: learning ────────────────────────────────────────────────────
    op.create_table(
        "learning_tracks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("target_completion", sa.Date),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active','paused','done')", name="valid_status"
        ),
    )

    op.create_table(
        "curriculum_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("track_id", sa.BigInteger, sa.ForeignKey("learning_tracks.id"),
                  nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        # Athena checks understanding, not completion. Marking something done is
        # not the same as having learned it, so the two are separate columns.
        sa.Column("comprehension_checked", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("target_week", sa.Date),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('pending','doing','done','skipped')", name="valid_state"
        ),
    )
    op.create_index("ix_curriculum_track_seq", "curriculum_items", ["track_id", "sequence"])

    op.create_table(
        "study_sessions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("track_id", sa.BigInteger, sa.ForeignKey("learning_tracks.id")),
        sa.Column("curriculum_item_id", sa.BigInteger,
                  sa.ForeignKey("curriculum_items.id")),
        sa.Column("minutes", sa.Integer, nullable=False),
        sa.Column("note", sa.Text),
        # When a plan slips twice for the same reason the plan is wrong, not the
        # person. That judgement is only possible if the reason is recorded.
        sa.Column("blocker", sa.Text),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.CheckConstraint("minutes > 0", name="positive_minutes"),
    )
    op.create_index("ix_study_occurred", "study_sessions", ["occurred_at"])

    op.create_table(
        "papers",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("authors", sa.String(300)),
        sa.Column("url", sa.Text),
        sa.Column("scheduled_for", sa.Date),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("takeaway", sa.Text),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
    )
    op.create_index("ix_papers_scheduled", "papers", ["scheduled_for"],
                    postgresql_where=sa.text("read_at IS NULL"))

    op.create_table(
        "applications",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("university", sa.String(120), nullable=False),
        sa.Column("programme", sa.String(160)),
        sa.Column("intake", sa.String(32)),
        sa.Column("deadline_on", sa.Date),
        sa.Column("state", sa.String(20), nullable=False, server_default="researching"),
        sa.Column("scholarship_note", sa.Text),
        sa.Column("source_turn", sa.BigInteger, sa.ForeignKey("turns.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('researching','shortlisted','preparing','submitted',"
            "'accepted','rejected','withdrawn')",
            name="valid_state",
        ),
    )
    op.create_index("ix_applications_deadline", "applications", ["deadline_on"])


def downgrade() -> None:
    for table in (
        "applications", "papers", "study_sessions", "curriculum_items", "learning_tracks",
        "tasks", "project_sessions", "projects",
        "trips", "warranties", "documents", "inventory_items", "reminders",
        "body_metrics", "health_samples", "exercise_sets", "workouts",
        "meal_items", "meals", "foods",
    ):
        op.drop_table(table)
