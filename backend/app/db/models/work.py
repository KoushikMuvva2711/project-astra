"""Tier 2 — Nova's domain: projects, session state, tasks."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
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


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    repo_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_worked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','paused','done','abandoned')", name="valid_status"
        ),
        Index(
            "ix_projects_recent",
            "last_worked_at",
            postgresql_where=text("status = 'active'"),
        ),
    )


class ProjectSession(Base):
    """The resumption substrate.

    Nova's defining capability is restoring full working state after a gap, and
    that quality is set at *close* time, not at open time. A session that ends
    without `next_step` recorded turns the next resumption into guesswork, which
    is exactly the failure the agent exists to prevent.
    """

    __tablename__ = "project_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    completed: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    in_flight: Mapped[str | None] = mapped_column(Text)
    blocked_on: Mapped[str | None] = mapped_column(Text)
    next_step: Mapped[str | None] = mapped_column(Text)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_project_sessions_recent", "project_id", "recorded_at"),)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="todo")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    estimate_min: Mapped[int | None] = mapped_column(Integer)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    due_on: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('todo','doing','blocked','done','dropped')", name="valid_state"
        ),
        CheckConstraint("priority BETWEEN 1 AND 5", name="valid_priority"),
        Index(
            "ix_tasks_open",
            "project_id", "priority",
            postgresql_where=text("state IN ('todo','doing','blocked')"),
        ),
    )
