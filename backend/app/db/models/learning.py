"""Tier 2 — Athena's domain: curriculum, study, papers, applications."""

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LearningTrack(Base):
    __tablename__ = "learning_tracks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    target_completion: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('active','paused','done')", name="valid_status"),
    )


class CurriculumItem(Base):
    """`state` and `comprehension_checked` are separate on purpose.

    Athena's spec says she checks understanding, not completion — marking a
    chapter done is not evidence of having learned it. Collapsing these into one
    column would make "you marked the RAG chapter done, explain why naive
    chunking hurts retrieval" unaskable.
    """

    __tablename__ = "curriculum_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    track_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("learning_tracks.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    comprehension_checked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    target_week: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','doing','done','skipped')", name="valid_state"
        ),
        Index("ix_curriculum_track_seq", "track_id", "sequence"),
    )


class StudySession(Base):
    """`blocker` is the column that makes adaptive planning possible.

    When the same obstacle appears twice the plan is wrong, not the person — but
    that is only detectable if the reason a session was missed was recorded at
    the time rather than inferred later.
    """

    __tablename__ = "study_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    track_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("learning_tracks.id")
    )
    curriculum_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("curriculum_items.id")
    )
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    blocker: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))

    __table_args__ = (
        CheckConstraint("minutes > 0", name="positive_minutes"),
        Index("ix_study_occurred", "occurred_at"),
    )


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    authors: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[date | None] = mapped_column(Date)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    takeaway: Mapped[str | None] = mapped_column(Text)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))

    __table_args__ = (
        Index("ix_papers_scheduled", "scheduled_for",
              postgresql_where=text("read_at IS NULL")),
    )


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    university: Mapped[str] = mapped_column(String(120), nullable=False)
    programme: Mapped[str | None] = mapped_column(String(160))
    intake: Mapped[str | None] = mapped_column(String(32))
    deadline_on: Mapped[date | None] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="researching")
    scholarship_note: Mapped[str | None] = mapped_column(Text)
    source_turn: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("turns.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('researching','shortlisted','preparing','submitted',"
            "'accepted','rejected','withdrawn')",
            name="valid_state",
        ),
        Index("ix_applications_deadline", "deadline_on"),
    )
