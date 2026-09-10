"""Tier 2 — Lyra's domain: food, training, recovery, body composition.

Everything countable is an integer in a fixed unit, for the same reason money is
paise: macro totals and load progressions are arithmetic the user will check.

  quantity_dc   tenths of a serving   "one and a half rotis" -> 15
  protein_dg    decigrams             34.5 g -> 345
  load_g        grams                 62.5 kg -> 62500
  value_milli   thousandths           HRV 45.5 ms -> 45500

Portions are servings rather than grams because that is how people speak. Nobody
says "sixty grams of roti"; they say "two rotis". Grams are derivable from
`foods.serving_grams` when needed.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Food(Base):
    __tablename__ = "foods"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    serving_label: Mapped[str] = mapped_column(String(48), nullable=False)
    serving_grams: Mapped[int | None] = mapped_column(Integer)
    kcal: Mapped[int] = mapped_column(Integer, nullable=False)
    protein_dg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    carbs_dg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fat_dg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_indian: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("kcal >= 0", name="non_negative_kcal"),
        CheckConstraint(
            "protein_dg >= 0 AND carbs_dg >= 0 AND fat_dg >= 0", name="non_negative_macros"
        ),
    )


class Meal(Base):
    __tablename__ = "meals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="meal")
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    superseded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("meals.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('breakfast','lunch','dinner','snack','meal')", name="valid_kind"
        ),
        Index(
            "ix_meals_live_occurred",
            "occurred_at",
            postgresql_where=text("superseded_by IS NULL"),
        ),
    )


class MealItem(Base):
    __tablename__ = "meal_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    meal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("meals.id"), nullable=False)
    food_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("foods.id"))
    raw_text: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity_dc: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    kcal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    protein_dg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    carbs_dg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fat_dg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("quantity_dc > 0", name="positive_quantity"),
        Index("ix_meal_items_meal", "meal_id"),
    )


class Workout(Base):
    __tablename__ = "workouts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    perceived_effort: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    external_ref: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "perceived_effort IS NULL OR perceived_effort BETWEEN 1 AND 10",
            name="valid_effort",
        ),
        Index("ix_workouts_occurred", "occurred_at"),
    )


class ExerciseSet(Base):
    __tablename__ = "exercise_sets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workout_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workouts.id"), nullable=False
    )
    exercise: Mapped[str] = mapped_column(String(64), nullable=False)
    set_index: Mapped[int] = mapped_column(Integer, nullable=False)
    reps: Mapped[int | None] = mapped_column(Integer)
    load_g: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint("set_index > 0", name="positive_set_index"),
        CheckConstraint("reps IS NULL OR reps > 0", name="positive_reps"),
        Index("ix_sets_workout", "workout_id"),
        Index("ix_sets_exercise", "exercise"),
    )


class HealthSample(Base):
    """Apple Watch data, arriving in batches via the Shortcuts automation.

    `external_id` is unique so a re-sent window is idempotent — Apple Health
    exports overlap by design, and Lyra's averages must not double-count.
    """

    __tablename__ = "health_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="shortcuts")
    external_id: Mapped[str | None] = mapped_column(String(96), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "metric IN ('steps','heart_rate','hrv','sleep','active_energy',"
            "'resting_hr','weight','vo2max','respiratory_rate')",
            name="valid_metric",
        ),
        Index("ix_health_metric_time", "metric", "started_at"),
    )


class BodyMetric(Base):
    __tablename__ = "body_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    weight_g: Mapped[int | None] = mapped_column(BigInteger)
    body_fat_pct_d: Mapped[int | None] = mapped_column(Integer)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))

    __table_args__ = (
        CheckConstraint("weight_g IS NULL OR weight_g > 0", name="positive_weight"),
        Index("ix_body_measured", "measured_at"),
    )
