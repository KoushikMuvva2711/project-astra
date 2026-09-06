"""Report builders: deterministic data, computed in SQL.

Every figure in every report comes from here, not from a model. The model's job
is to narrate what these return — the same rule the agents follow (PRD FR-T2),
applied to reports because reports are where a wrong number does the most
damage: they are read as a summary of record, and nobody re-derives them.

A practical consequence: reports are correct even on a weak local model. The
prose degrades; the numbers do not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.money import format_inr


@dataclass
class Section:
    """One block of a report. `priority` orders what the user sees first."""

    key: str
    headline: str
    data: dict[str, Any] = field(default_factory=dict)
    priority: int = 5


@dataclass
class Report:
    kind: str
    period_start: datetime
    period_end: datetime
    sections: list[Section] = field(default_factory=list)

    def add(self, section: Section | None) -> None:
        if section is not None:
            self.sections.append(section)

    def top(self, n: int) -> list[Section]:
        """Highest-priority sections first.

        Astraea's spec caps briefings at three items. Ranking happens here so
        that limit is enforced by the data layer rather than trusted to a prompt.
        """
        return sorted(self.sections, key=lambda s: s.priority)[:n]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "sections": [
                {"key": s.key, "headline": s.headline, "data": s.data, "priority": s.priority}
                for s in self.sections
            ],
        }


def _day_start(when: datetime | None = None) -> datetime:
    now = when or datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ── Finance ──────────────────────────────────────────────────────────────────

async def spending(db: AsyncSession, *, days: int = 30) -> Section | None:
    """Spend for the window, by category, against the previous equal window.

    Comparing like windows rather than calendar months means the number is
    meaningful on any day — a month-to-date figure on the 3rd tells you nothing.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    prior = since - timedelta(days=days)

    row = (
        await db.execute(
            sa.text(
                "SELECT COALESCE(SUM(amount_minor) FILTER (WHERE occurred_at >= :since), 0) "
                "         AS current, "
                "       COALESCE(SUM(amount_minor) FILTER "
                "         (WHERE occurred_at >= :prior AND occurred_at < :since), 0) "
                "         AS previous, "
                "       COUNT(*) FILTER (WHERE occurred_at >= :since) AS entries "
                "FROM expenses WHERE superseded_by IS NULL AND occurred_at >= :prior"
            ),
            {"since": since, "prior": prior},
        )
    ).one()

    if row.entries == 0:
        return None

    categories = (
        await db.execute(
            sa.text(
                "SELECT category, SUM(amount_minor) AS total, COUNT(*) AS n "
                "FROM expenses WHERE superseded_by IS NULL AND occurred_at >= :since "
                "GROUP BY category ORDER BY total DESC LIMIT 8"
            ),
            {"since": since},
        )
    ).all()

    current, previous = int(row.current), int(row.previous)
    delta = current - previous
    # Only meaningful when there is a prior window to compare against.
    pct = round(delta * 100 / previous, 1) if previous else None

    return Section(
        key="spending",
        headline=f"{format_inr(current)} over {days} days, {row.entries} entries.",
        priority=3,
        data={
            "total_minor": current,
            "total": format_inr(current),
            "previous_minor": previous,
            "change_minor": delta,
            "change": format_inr(abs(delta)),
            "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
            "change_pct": pct,
            "entry_count": int(row.entries),
            "days": days,
            "by_category": [
                {
                    "category": c.category,
                    "total_minor": int(c.total),
                    "total": format_inr(int(c.total)),
                    "entries": int(c.n),
                    "share_pct": round(int(c.total) * 100 / current, 1) if current else 0,
                }
                for c in categories
            ],
        },
    )


async def budget_position(db: AsyncSession) -> Section | None:
    """Categories at or over their budget. Silent when nothing is breached."""
    rows = (
        await db.execute(
            sa.text(
                "SELECT b.category, b.limit_minor, "
                "       COALESCE(SUM(e.amount_minor), 0) AS spent "
                "FROM budgets b "
                "LEFT JOIN expenses e ON e.category = b.category "
                "  AND e.superseded_by IS NULL "
                "  AND e.occurred_at >= date_trunc('month', now()) "
                "WHERE b.effective_to IS NULL AND b.period = 'monthly' "
                "GROUP BY b.category, b.limit_minor"
            )
        )
    ).all()

    breached = [
        {
            "category": r.category or "overall",
            "limit": format_inr(int(r.limit_minor)),
            "spent": format_inr(int(r.spent)),
            "over_by": format_inr(int(r.spent) - int(r.limit_minor)),
            "used_pct": round(int(r.spent) * 100 / int(r.limit_minor), 1),
        }
        for r in rows
        if int(r.spent) >= int(r.limit_minor) * 0.9  # flag at 90%, not only at breach
    ]
    if not breached:
        return None

    return Section(
        key="budget",
        headline=f"{len(breached)} budget(s) at or near the limit.",
        priority=1,
        data={"categories": breached},
    )


async def upcoming_bills(db: AsyncSession, *, days: int = 7) -> Section | None:
    rows = (
        await db.execute(
            sa.text(
                "SELECT name, amount_minor, next_due, autopay FROM bills "
                "WHERE active AND next_due IS NOT NULL "
                "  AND next_due <= (now() + make_interval(days => :days))::date "
                "ORDER BY next_due"
            ),
            {"days": days},
        )
    ).all()
    if not rows:
        return None

    return Section(
        key="bills",
        headline=f"{len(rows)} bill(s) due within {days} days.",
        priority=2,
        data={
            "bills": [
                {
                    "name": r.name,
                    "amount": format_inr(int(r.amount_minor)) if r.amount_minor else None,
                    "due": r.next_due.isoformat(),
                    "autopay": r.autopay,
                }
                for r in rows
            ]
        },
    )


# ── Health ───────────────────────────────────────────────────────────────────

async def nutrition(db: AsyncSession, *, days: int = 7) -> Section | None:
    """Daily calories and protein, and how many days hit the stored target."""
    since = _day_start() - timedelta(days=days - 1)

    rows = (
        await db.execute(
            sa.text(
                "SELECT date_trunc('day', m.occurred_at) AS day, "
                "       SUM(i.kcal) AS kcal, SUM(i.protein_dg) AS protein_dg "
                "FROM meals m JOIN meal_items i ON i.meal_id = m.id "
                "WHERE m.superseded_by IS NULL AND m.occurred_at >= :since "
                "GROUP BY 1 ORDER BY 1"
            ),
            {"since": since},
        )
    ).all()
    if not rows:
        return None

    target = (
        await db.execute(
            sa.text(
                "SELECT value FROM memory_facts WHERE namespace = 'health' "
                "AND predicate = 'daily_protein_target' AND valid_to IS NULL "
                "AND retracted_at IS NULL LIMIT 1"
            )
        )
    ).scalar_one_or_none()
    protein_target = (target or {}).get("grams")

    daily = [
        {
            "day": r.day.date().isoformat(),
            "kcal": int(r.kcal or 0),
            "protein_g": round(int(r.protein_dg or 0) / 10, 1),
        }
        for r in rows
    ]
    avg_protein = round(sum(d["protein_g"] for d in daily) / len(daily), 1)
    on_target = (
        sum(1 for d in daily if d["protein_g"] >= protein_target) if protein_target else None
    )

    return Section(
        key="nutrition",
        headline=(
            f"{avg_protein} g protein a day on average across {len(daily)} logged days."
        ),
        priority=4,
        data={
            "days_logged": len(daily),
            "days_in_window": days,
            "avg_kcal": round(sum(d["kcal"] for d in daily) / len(daily)),
            "avg_protein_g": avg_protein,
            "protein_target_g": protein_target,
            "days_on_target": on_target,
            "daily": daily,
        },
    )


async def training(db: AsyncSession, *, days: int = 14) -> Section | None:
    rows = (
        await db.execute(
            sa.text(
                "SELECT kind, COUNT(*) AS n, MAX(occurred_at) AS latest "
                "FROM workouts WHERE occurred_at >= now() - make_interval(days => :days) "
                "GROUP BY kind ORDER BY n DESC"
            ),
            {"days": days},
        )
    ).all()
    if not rows:
        return None

    total = sum(int(r.n) for r in rows)
    latest = max(r.latest for r in rows)
    days_since = (datetime.now(UTC) - latest).days

    return Section(
        key="training",
        headline=f"{total} sessions in {days} days.",
        # A long gap since the last session is worth surfacing early.
        priority=3 if days_since >= 4 else 5,
        data={
            "total_sessions": total,
            "days": days,
            "per_week": round(total * 7 / days, 1),
            "days_since_last": days_since,
            "by_kind": [{"kind": r.kind, "sessions": int(r.n)} for r in rows],
        },
    )


# ── Learning and work ────────────────────────────────────────────────────────

async def study(db: AsyncSession, *, days: int = 7) -> Section | None:
    row = (
        await db.execute(
            sa.text(
                "SELECT COUNT(*) AS sessions, COALESCE(SUM(minutes), 0) AS minutes, "
                "       COUNT(*) FILTER (WHERE blocker IS NOT NULL) AS blocked "
                "FROM study_sessions "
                "WHERE occurred_at >= now() - make_interval(days => :days)"
            ),
            {"days": days},
        )
    ).one()
    if row.sessions == 0:
        return None

    blockers = (
        await db.execute(
            sa.text(
                "SELECT blocker, COUNT(*) AS n FROM study_sessions "
                "WHERE blocker IS NOT NULL "
                "  AND occurred_at >= now() - make_interval(days => :days) "
                "GROUP BY blocker ORDER BY n DESC LIMIT 3"
            ),
            {"days": days},
        )
    ).all()

    return Section(
        key="study",
        headline=f"{row.sessions} study sessions, {int(row.minutes)} minutes.",
        priority=4,
        data={
            "sessions": int(row.sessions),
            "minutes": int(row.minutes),
            "days": days,
            "blocked_sessions": int(row.blocked),
            # A blocker seen twice means the plan is wrong, not the person.
            "recurring_blockers": [
                {"reason": b.blocker, "times": int(b.n)} for b in blockers if int(b.n) > 1
            ],
        },
    )


async def project_state(db: AsyncSession) -> Section | None:
    """What Nova would need to resume: last state and open task counts."""
    rows = (
        await db.execute(
            sa.text(
                "SELECT p.name, p.slug, p.last_worked_at, "
                "       s.next_step, s.blocked_on, "
                "       (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id "
                "        AND t.state IN ('todo','doing','blocked')) AS open_tasks "
                "FROM projects p "
                "LEFT JOIN LATERAL ("
                "  SELECT next_step, blocked_on FROM project_sessions ps "
                "  WHERE ps.project_id = p.id ORDER BY recorded_at DESC LIMIT 1"
                ") s ON TRUE "
                "WHERE p.status = 'active' ORDER BY p.last_worked_at DESC NULLS LAST LIMIT 5"
            )
        )
    ).all()
    if not rows:
        return None

    projects = []
    for r in rows:
        idle_days = (
            (datetime.now(UTC) - r.last_worked_at).days if r.last_worked_at else None
        )
        projects.append(
            {
                "name": r.name,
                "slug": r.slug,
                "idle_days": idle_days,
                "next_step": r.next_step,
                "blocked_on": r.blocked_on,
                "open_tasks": int(r.open_tasks),
            }
        )

    blocked = [p for p in projects if p["blocked_on"]]
    return Section(
        key="projects",
        headline=f"{len(projects)} active project(s).",
        priority=2 if blocked else 5,
        data={"projects": projects, "blocked_count": len(blocked)},
    )


# ── Home ─────────────────────────────────────────────────────────────────────

async def due_reminders(db: AsyncSession, *, days: int = 2) -> Section | None:
    # 'notified' as well as 'pending': the scheduler fires due reminders within a
    # minute, so filtering on 'pending' alone means a reminder is almost never in
    # this state by the time a briefing is built — being notified is not being
    # done. Only completion or cancellation removes one from the briefing.
    rows = (
        await db.execute(
            sa.text(
                "SELECT id, text, due_at, state FROM reminders "
                "WHERE state IN ('pending', 'notified') AND due_at IS NOT NULL "
                "  AND due_at <= now() + make_interval(days => :days) "
                "ORDER BY due_at LIMIT 10"
            ),
            {"days": days},
        )
    ).all()
    if not rows:
        return None

    overdue = [r for r in rows if r.due_at < datetime.now(UTC)]
    return Section(
        key="reminders",
        headline=f"{len(rows)} reminder(s) due, {len(overdue)} overdue.",
        priority=1 if overdue else 2,
        data={
            "reminders": [
                {
                    "id": r.id,
                    "text": r.text,
                    "due": r.due_at.isoformat(),
                    "overdue": r.due_at < datetime.now(UTC),
                    "already_notified": r.state == "notified",
                }
                for r in rows
            ]
        },
    )


async def expiring_documents(db: AsyncSession, *, days: int = 60) -> Section | None:
    rows = (
        await db.execute(
            sa.text(
                "SELECT kind, label, expires_on FROM documents "
                "WHERE expires_on IS NOT NULL "
                "  AND expires_on <= (now() + make_interval(days => :days))::date "
                "ORDER BY expires_on LIMIT 10"
            ),
            {"days": days},
        )
    ).all()
    if not rows:
        return None

    return Section(
        key="documents",
        headline=f"{len(rows)} document(s) expiring within {days} days.",
        priority=2,
        data={
            "documents": [
                # Label and kind only — never an identifier. See docs/agents/selene.md.
                {"kind": r.kind, "label": r.label, "expires": r.expires_on.isoformat()}
                for r in rows
            ]
        },
    )


# ── Memory health ────────────────────────────────────────────────────────────

async def open_conflicts(db: AsyncSession) -> Section | None:
    """Contradictions the curator found. Surfaced in briefings, never mid-turn."""
    rows = (
        await db.execute(
            sa.text(
                "SELECT c.id, c.namespace, c.predicate, c.fact_ids "
                "FROM memory_conflicts c WHERE c.state = 'open' LIMIT 5"
            )
        )
    ).all()
    if not rows:
        return None

    conflicts = []
    for row in rows:
        values = (
            await db.execute(
                sa.text(
                    "SELECT value_text, recorded_at FROM memory_facts "
                    "WHERE id = ANY(:ids) ORDER BY recorded_at"
                ),
                {"ids": list(row.fact_ids)},
            )
        ).all()
        conflicts.append(
            {
                "id": row.id,
                "namespace": row.namespace,
                "predicate": row.predicate,
                "values": [
                    {"text": v.value_text, "recorded": v.recorded_at.isoformat()}
                    for v in values
                ],
            }
        )

    return Section(
        key="conflicts",
        headline=f"{len(conflicts)} contradiction(s) need a decision.",
        priority=1,
        data={"conflicts": conflicts},
    )


# ── Cross-domain: the reason a coordinator exists ────────────────────────────

async def correlations(db: AsyncSession, *, days: int = 14) -> Section | None:
    """Patterns visible only across namespaces.

    This is the one thing no specialist agent can produce, and the PRD's third
    success criterion. Each correlation is computed, not inferred — the model
    narrates them but does not discover them, because a model asked to find
    patterns in a data dump will find some whether or not they exist.
    """
    found: list[dict[str, Any]] = []
    since = datetime.now(UTC) - timedelta(days=days)
    prior = since - timedelta(days=days)

    # Grocery spend down while eating-out spend up: a diet-adherence signal that
    # neither Vega nor Lyra can see alone.
    row = (
        await db.execute(
            sa.text(
                "SELECT "
                " COALESCE(SUM(amount_minor) FILTER (WHERE category='groceries' "
                "   AND occurred_at >= :since),0) AS groceries_now, "
                " COALESCE(SUM(amount_minor) FILTER (WHERE category='groceries' "
                "   AND occurred_at >= :prior AND occurred_at < :since),0) AS groceries_before, "
                " COALESCE(SUM(amount_minor) FILTER (WHERE category IN "
                "   ('food_delivery','dining_out') AND occurred_at >= :since),0) AS out_now, "
                " COALESCE(SUM(amount_minor) FILTER (WHERE category IN "
                "   ('food_delivery','dining_out') AND occurred_at >= :prior "
                "   AND occurred_at < :since),0) AS out_before "
                "FROM expenses WHERE superseded_by IS NULL AND occurred_at >= :prior"
            ),
            {"since": since, "prior": prior},
        )
    ).one()

    if row.groceries_before > 0 and row.out_before > 0:
        groceries_down = int(row.groceries_now) < int(row.groceries_before) * 0.7
        eating_out_up = int(row.out_now) > int(row.out_before) * 1.3
        if groceries_down and eating_out_up:
            found.append(
                {
                    "kind": "groceries_down_eating_out_up",
                    "detail": (
                        f"Grocery spend fell from {format_inr(int(row.groceries_before))} "
                        f"to {format_inr(int(row.groceries_now))} while eating out rose "
                        f"from {format_inr(int(row.out_before))} to "
                        f"{format_inr(int(row.out_now))}."
                    ),
                    "namespaces": ["finance", "health"],
                }
            )

    # Training volume down in a window where sleep was short.
    sleep_row = (
        await db.execute(
            sa.text(
                "SELECT AVG(value_milli) / 3600000.0 AS avg_hours, COUNT(*) AS n "
                "FROM health_samples WHERE metric = 'sleep' AND started_at >= :since"
            ),
            {"since": since},
        )
    ).one()
    workout_count = (
        await db.execute(
            sa.text("SELECT COUNT(*) FROM workouts WHERE occurred_at >= :since"),
            {"since": since},
        )
    ).scalar_one()

    if sleep_row.n and sleep_row.avg_hours and float(sleep_row.avg_hours) < 6.5:
        found.append(
            {
                "kind": "short_sleep",
                "detail": (
                    f"Sleep averaged {float(sleep_row.avg_hours):.1f} hours over "
                    f"{days} days, with {workout_count} training sessions."
                ),
                "namespaces": ["health"],
            }
        )

    # Study sessions dropped while project work rose — the Nova/Athena tension.
    counts = (
        await db.execute(
            sa.text(
                "SELECT "
                " (SELECT COUNT(*) FROM study_sessions WHERE occurred_at >= :since) AS study_now, "
                " (SELECT COUNT(*) FROM study_sessions WHERE occurred_at >= :prior "
                "  AND occurred_at < :since) AS study_before, "
                " (SELECT COUNT(*) FROM tasks WHERE completed_at >= :since) AS tasks_now"
            ),
            {"since": since, "prior": prior},
        )
    ).one()

    if int(counts.study_before) >= 3 and int(counts.study_now) < int(counts.study_before) * 0.5:
        found.append(
            {
                "kind": "study_displaced_by_project_work",
                "detail": (
                    f"Study sessions fell from {counts.study_before} to "
                    f"{counts.study_now} while {counts.tasks_now} project tasks "
                    "were completed."
                ),
                "namespaces": ["learning", "work"],
            }
        )

    if not found:
        return None

    return Section(
        key="correlations",
        headline=f"{len(found)} cross-domain pattern(s).",
        priority=2,
        data={"patterns": found, "days": days},
    )


# ── Composed reports ─────────────────────────────────────────────────────────

async def daily_briefing(db: AsyncSession) -> Report:
    """What needs attention today.

    Deliberately weighted toward things with a deadline or a decision. If nothing
    scores highly, the briefing is genuinely empty — Astraea's spec requires her
    to say nothing matters rather than manufacture content, and a briefing that
    invents items teaches the user to skip it.
    """
    now = datetime.now(UTC)
    report = Report(kind="daily_briefing", period_start=_day_start(now), period_end=now)

    report.add(await open_conflicts(db))
    report.add(await due_reminders(db, days=1))
    report.add(await upcoming_bills(db, days=3))
    report.add(await budget_position(db))
    report.add(await project_state(db))
    report.add(await expiring_documents(db, days=30))
    return report


async def weekly_review(db: AsyncSession) -> Report:
    """The wider picture: trends and cross-domain patterns."""
    now = datetime.now(UTC)
    report = Report(kind="weekly_review", period_start=now - timedelta(days=7), period_end=now)

    report.add(await correlations(db, days=14))
    report.add(await spending(db, days=7))
    report.add(await nutrition(db, days=7))
    report.add(await training(db, days=14))
    report.add(await study(db, days=7))
    report.add(await project_state(db))
    report.add(await open_conflicts(db))
    report.add(await budget_position(db))
    return report


async def domain_report(db: AsyncSession, domain: str, *, days: int = 30) -> Report:
    """A single agent's domain, in depth."""
    now = datetime.now(UTC)
    report = Report(
        kind=f"{domain}_report", period_start=now - timedelta(days=days), period_end=now
    )

    if domain == "finance":
        report.add(await spending(db, days=days))
        report.add(await budget_position(db))
        report.add(await upcoming_bills(db, days=30))
    elif domain == "health":
        report.add(await nutrition(db, days=min(days, 30)))
        report.add(await training(db, days=days))
    elif domain == "learning":
        report.add(await study(db, days=days))
    elif domain == "work":
        report.add(await project_state(db))
    elif domain == "home":
        report.add(await due_reminders(db, days=7))
        report.add(await expiring_documents(db, days=90))
    else:
        raise ValueError(f"unknown domain {domain!r}")

    return report


DOMAINS = ("finance", "health", "learning", "work", "home")


def report_period(kind: str) -> tuple[date, date]:
    """Convenience for callers that need the window without building the report."""
    today = datetime.now(UTC).date()
    if kind == "weekly_review":
        return today - timedelta(days=7), today
    return today, today
