"""Self-maintenance jobs.

The user should never have to start a fresh session, prune anything, or think
about context limits. Everything that keeps the system tidy happens here, on a
schedule, without being asked.

Every job is idempotent and independently failable: a job that throws is logged
and skipped, never retried into a loop, and never blocks the others. Maintenance
failing quietly is acceptable; maintenance taking the system down is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.provider import Message
from app.llm.registry import get_registry
from app.logging_config import get_logger

log = get_logger(__name__)

# Facts nobody has restated slowly lose confidence rather than being deleted.
# 2% a month: a preference asserted once eighteen months ago still ranks below
# one mentioned last week, without ever being erased. See memory-design.md §8.
#
# Decimal, not float. `confidence` is NUMERIC(4,3), and a float 0.30 is really
# 0.29999999999999998 — so `confidence > 0.30` compares 0.300 against something
# fractionally smaller, matches, and the floor never holds: confidence decays
# past its own minimum forever. Decimal sends an exact numeric to Postgres.
# Same class of bug as using floats for money, in a place it is easier to miss.
MONTHLY_DECAY = Decimal("0.98")
MIN_CONFIDENCE = Decimal("0.300")


async def close_stale_sessions(db: AsyncSession) -> int:
    """Close sessions that have gone quiet.

    Without this, an abandoned session stays 'open' forever and every later
    utterance is routed to whichever agent was last active — the single most
    confusing failure a user can hit, because it looks like the router is broken.
    """
    timeout = get_settings().session_timeout_seconds
    result = await db.execute(
        sa.text(
            "UPDATE sessions SET closed_at = now(), close_reason = 'timeout' "
            "WHERE closed_at IS NULL "
            "  AND last_activity_at < now() - make_interval(secs => :timeout) "
            "RETURNING id"
        ),
        {"timeout": timeout},
    )
    closed = len(result.all())
    if closed:
        log.info("maintenance.sessions_closed", count=closed)
    return closed


async def summarise_sessions(db: AsyncSession, *, limit: int = 20) -> int:
    """Write a short summary for each closed session that lacks one.

    This is what keeps recall cost roughly constant as history grows: retrieval
    searches summaries and drills into raw turns only when one looks relevant.
    Without it, the transcript grows without bound and recall quality decays —
    the slow failure the user would eventually feel as "it forgot".
    """
    rows = (
        await db.execute(
            sa.text(
                "SELECT s.id, s.active_agent, s.started_at, s.closed_at, "
                "       COUNT(t.id) AS turn_count "
                "FROM sessions s JOIN turns t ON t.session_id = s.id "
                "LEFT JOIN session_summaries ss ON ss.session_id = s.id "
                "WHERE s.closed_at IS NOT NULL AND ss.session_id IS NULL "
                "GROUP BY s.id, s.active_agent, s.started_at, s.closed_at "
                "HAVING COUNT(t.id) >= 2 "
                "ORDER BY s.closed_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()

    written = 0
    for row in rows:
        turns = (
            await db.execute(
                sa.text(
                    "SELECT role, transcript FROM turns WHERE session_id = :s "
                    "ORDER BY created_at LIMIT 40"
                ),
                {"s": row.id},
            )
        ).all()
        transcript = "\n".join(f"{t.role}: {t.transcript}" for t in turns)

        summary = await _summarise(db, transcript, agent=row.active_agent)
        await db.execute(
            sa.text(
                "INSERT INTO session_summaries "
                "(session_id, agent, summary, turn_count, period_start, period_end) "
                "VALUES (:s, :a, :summary, :n, :start, :end) "
                "ON CONFLICT (session_id) DO NOTHING"
            ),
            {
                "s": row.id,
                "a": row.active_agent,
                "summary": summary,
                "n": int(row.turn_count),
                "start": row.started_at,
                "end": row.closed_at,
            },
        )
        written += 1

    if written:
        log.info("maintenance.sessions_summarised", count=written)
    return written


async def _summarise(db: AsyncSession, transcript: str, *, agent: str) -> str:
    """Two to four sentences, on the cheap model. Falls back to a truncation.

    A mechanical fallback beats no summary: an unsummarised session is invisible
    to recall forever, whereas a crude summary is still a retrievable pointer
    into the raw turns.
    """
    try:
        response = await get_registry().complete(
            [
                Message(
                    role="system",
                    content=(
                        "Summarise this conversation in two to four sentences. "
                        "Record what was decided, logged, or asked. Keep specific "
                        "figures and names. No preamble."
                    ),
                ),
                Message(role="user", content=transcript[:6000]),
            ],
            role="cheap",
            max_tokens=160,
            temperature=0.2,
            session=db,
            agent=agent,
            purpose="summarise",
        )
        text = (response.text or "").strip()
        if len(text) >= 20 and not text.startswith(("{", "[")):
            return text
    except Exception as exc:
        log.warning("maintenance.summary_failed", error=str(exc))

    first = transcript.replace("\n", " ")[:280]
    return f"(unsummarised) {first}"


async def decay_fact_confidence(db: AsyncSession) -> int:
    """Age out facts nobody has restated, without deleting anything."""
    result = await db.execute(
        sa.text(
            "UPDATE memory_facts SET confidence = GREATEST(confidence * :decay, :floor) "
            "WHERE valid_to IS NULL AND retracted_at IS NULL "
            "  AND recorded_at < now() - interval '30 days' "
            "  AND confidence > :floor "
            "RETURNING id"
        ),
        {"decay": MONTHLY_DECAY, "floor": MIN_CONFIDENCE},
    )
    decayed = len(result.all())
    if decayed:
        log.info("maintenance.confidence_decayed", count=decayed)
    return decayed


async def consolidate_duplicate_facts(db: AsyncSession) -> int:
    """Collapse exact duplicate multi-valued facts.

    Only byte-identical `value_text` within the same predicate is merged — near
    duplicates are left alone, because deciding two differently-worded facts are
    'the same' is a judgement call, and getting it wrong silently destroys
    information the user can never recover.
    """
    result = await db.execute(
        sa.text(
            "UPDATE memory_facts SET retracted_at = now() WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY namespace, entity, predicate, value_text "
            "      ORDER BY recorded_at DESC"
            "    ) AS rn"
            "    FROM memory_facts "
            "    WHERE valid_to IS NULL AND retracted_at IS NULL"
            "  ) ranked WHERE rn > 1"
            ") RETURNING id"
        )
    )
    merged = len(result.all())
    if merged:
        log.info("maintenance.duplicate_facts_retracted", count=merged)
    return merged


async def sweep_conflicts(db: AsyncSession) -> int:
    """Queue contradictions the curator has not already recorded.

    The curator detects conflicts as events arrive; this catches contradictions
    created by direct writes or by facts that only became contradictory once an
    earlier one aged out.
    """
    rows = (
        await db.execute(
            sa.text(
                "SELECT f.namespace, f.entity, f.predicate, "
                "       array_agg(f.id ORDER BY f.recorded_at) AS ids "
                "FROM memory_facts f JOIN fact_predicates p ON p.slug = f.predicate "
                "WHERE p.cardinality = 'single' "
                "  AND f.valid_to IS NULL AND f.retracted_at IS NULL "
                "GROUP BY f.namespace, f.entity, f.predicate HAVING COUNT(*) > 1"
            )
        )
    ).all()

    queued = 0
    for row in rows:
        existing = (
            await db.execute(
                sa.text(
                    "SELECT 1 FROM memory_conflicts WHERE namespace = :ns "
                    "AND entity = :e AND predicate = :p AND state = 'open'"
                ),
                {"ns": row.namespace, "e": row.entity, "p": row.predicate},
            )
        ).scalar_one_or_none()
        if existing:
            continue

        import json

        await db.execute(
            sa.text(
                "INSERT INTO memory_conflicts "
                "(namespace, entity, predicate, fact_ids, state) "
                "VALUES (:ns, :e, :p, CAST(:ids AS jsonb), 'open')"
            ),
            {
                "ns": row.namespace,
                "e": row.entity,
                "p": row.predicate,
                "ids": json.dumps(list(row.ids)),
            },
        )
        queued += 1

    if queued:
        log.info("maintenance.conflicts_queued", count=queued)
    return queued


async def expire_hints(db: AsyncSession) -> int:
    """Delete hints that expired unread. Stale context is worse than none."""
    result = await db.execute(
        sa.text(
            "DELETE FROM memory_hints WHERE expires_at < now() "
            "  AND consumed_at IS NULL RETURNING id"
        )
    )
    expired = len(result.all())
    if expired:
        log.info("maintenance.hints_expired", count=expired)
    return expired


async def roll_recurring_bills(db: AsyncSession) -> int:
    """Advance monthly bills whose due date has passed."""
    result = await db.execute(
        sa.text(
            "UPDATE bills SET next_due = next_due + interval '1 month' "
            "WHERE active AND recurrence = 'monthly' AND next_due < current_date "
            "RETURNING id"
        )
    )
    rolled = len(result.all())
    if rolled:
        log.info("maintenance.bills_rolled", count=rolled)
    return rolled


async def prune_audio(db: AsyncSession, *, days: int = 7) -> int:
    """Drop audio references past retention. Turns are kept forever; audio is not.

    PRD §2: audio exists only to debug ASR failures. Keeping it longer is a
    privacy cost with no matching benefit.
    """
    result = await db.execute(
        sa.text(
            "UPDATE turns SET audio_ref = NULL WHERE audio_ref IS NOT NULL "
            "  AND created_at < now() - make_interval(days => :days) RETURNING id"
        ),
        {"days": days},
    )
    pruned = len(result.all())
    if pruned:
        log.info("maintenance.audio_pruned", count=pruned)
    return pruned


async def fire_due_reminders(db: AsyncSession) -> list[dict[str, Any]]:
    """Mark due reminders notified, exactly once.

    `notified_at` is what enforces Selene's remind-once rule structurally. A
    reminder that fires twice trains the user to mute notifications, and a muted
    Selene is a dead Selene.
    """
    rows = (
        await db.execute(
            sa.text(
                "UPDATE reminders SET state = 'notified', notified_at = now() "
                "WHERE state = 'pending' AND due_at IS NOT NULL AND due_at <= now() "
                "RETURNING id, text, due_at"
            )
        )
    ).all()

    fired = [{"id": r.id, "text": r.text, "due": r.due_at.isoformat()} for r in rows]
    if fired:
        log.info("maintenance.reminders_fired", count=len(fired))
    return fired


# Ordered: cheap structural work first, model-backed work last, so a failure in
# summarisation never prevents sessions from being closed.
MAINTENANCE_JOBS = (
    ("close_stale_sessions", close_stale_sessions),
    ("expire_hints", expire_hints),
    ("roll_recurring_bills", roll_recurring_bills),
    ("prune_audio", prune_audio),
    ("consolidate_duplicate_facts", consolidate_duplicate_facts),
    ("decay_fact_confidence", decay_fact_confidence),
    ("sweep_conflicts", sweep_conflicts),
    ("summarise_sessions", summarise_sessions),
)


async def run_maintenance(db: AsyncSession) -> dict[str, Any]:
    """Run every job, isolating failures.

    Each job commits on its own so one failure cannot roll back the others'
    work, and the result reports per-job outcomes rather than a single boolean —
    silent maintenance is how a system rots without anyone noticing.
    """
    results: dict[str, Any] = {}
    for name, job in MAINTENANCE_JOBS:
        try:
            results[name] = await job(db)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            results[name] = f"error: {type(exc).__name__}: {exc}"
            log.error("maintenance.job_failed", job=name, error=str(exc))

    results["ran_at"] = datetime.now(UTC).isoformat()
    return results


def next_weekly_review_at(now: datetime | None = None) -> datetime:
    """Sunday 18:00 local. Used by the scheduler and shown in the status endpoint."""
    now = now or datetime.now(UTC)
    days_ahead = (6 - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    return target + timedelta(days=7) if target <= now else target
