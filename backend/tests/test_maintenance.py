"""Self-maintenance jobs.

These are what let the user never think about sessions, context, or cleanup.
Each is idempotent — running maintenance twice must not double-apply anything.
"""

import uuid

import pytest
import sqlalchemy as sa

from app.scheduler import jobs

pytestmark = pytest.mark.asyncio


async def _session_row(db, *, agent="vega", minutes_idle=0, closed=False):
    user_id = uuid.uuid4()
    await db.execute(
        sa.text("INSERT INTO users (id, display_name) VALUES (:i, 'T')"), {"i": user_id}
    )
    session_id = uuid.uuid4()
    await db.execute(
        sa.text(
            "INSERT INTO sessions (id, user_id, active_agent, last_activity_at, closed_at) "
            "VALUES (:i, :u, :a, now() - make_interval(mins => :m), "
            "        CASE WHEN :closed THEN now() ELSE NULL END)"
        ),
        {"i": session_id, "u": user_id, "a": agent, "m": minutes_idle, "closed": closed},
    )
    return session_id


# ── Stale sessions ───────────────────────────────────────────────────────────

async def test_stale_sessions_are_closed(db):
    """An abandoned open session silently misroutes every later utterance."""
    await _session_row(db, minutes_idle=30)
    assert await jobs.close_stale_sessions(db) == 1

    reason = (await db.execute(
        sa.text("SELECT close_reason FROM sessions WHERE closed_at IS NOT NULL")
    )).scalar_one()
    assert reason == "timeout"


async def test_active_sessions_are_left_open(db):
    await _session_row(db, minutes_idle=0)
    assert await jobs.close_stale_sessions(db) == 0


async def test_closing_is_idempotent(db):
    await _session_row(db, minutes_idle=30)
    assert await jobs.close_stale_sessions(db) == 1
    assert await jobs.close_stale_sessions(db) == 0


# ── Summarisation ────────────────────────────────────────────────────────────

async def test_closed_sessions_get_summarised(db):
    session_id = await _session_row(db, closed=True)
    for role, text in (("user", "spent 250 on auto"), ("agent", "Logged. 250 rupees.")):
        await db.execute(
            sa.text(
                "INSERT INTO turns (session_id, agent, role, transcript) "
                "VALUES (:s, 'vega', :r, :t)"
            ),
            {"s": session_id, "r": role, "t": text},
        )

    assert await jobs.summarise_sessions(db) == 1
    summary = (await db.execute(
        sa.text("SELECT summary FROM session_summaries WHERE session_id = :s"),
        {"s": session_id},
    )).scalar_one()
    assert len(summary) > 10


async def test_summarisation_is_idempotent(db):
    session_id = await _session_row(db, closed=True)
    for _ in range(2):
        await db.execute(
            sa.text(
                "INSERT INTO turns (session_id, agent, role, transcript) "
                "VALUES (:s, 'vega', 'user', 'hello')"
            ),
            {"s": session_id},
        )
    assert await jobs.summarise_sessions(db) == 1
    assert await jobs.summarise_sessions(db) == 0


async def test_single_turn_sessions_are_not_summarised(db):
    """One turn is already its own summary; summarising it wastes a model call."""
    session_id = await _session_row(db, closed=True)
    await db.execute(
        sa.text(
            "INSERT INTO turns (session_id, agent, role, transcript) "
            "VALUES (:s, 'vega', 'user', 'hi')"
        ),
        {"s": session_id},
    )
    assert await jobs.summarise_sessions(db) == 0


# ── Fact hygiene ─────────────────────────────────────────────────────────────

async def _fact(db, text, *, predicate="preference", age_days=0, confidence=1.0):
    await db.execute(
        sa.text(
            "INSERT INTO memory_facts (namespace, entity, predicate, value, value_text, "
            "asserted_by, valid_from, recorded_at, confidence) VALUES "
            "('global','user', :p, '{}'::jsonb, :t, 'astraea', now(), "
            " now() - make_interval(days => :d), :c)"
        ),
        {"p": predicate, "t": text, "d": age_days, "c": confidence},
    )


async def test_old_facts_decay_but_are_never_deleted(db):
    await _fact(db, "prefers morning workouts", age_days=60)
    assert await jobs.decay_fact_confidence(db) == 1

    row = (await db.execute(
        sa.text("SELECT confidence, retracted_at FROM memory_facts")
    )).one()
    assert float(row.confidence) < 1.0
    assert row.retracted_at is None, "decay must never delete"


async def test_recent_facts_do_not_decay(db):
    await _fact(db, "just told you this", age_days=1)
    assert await jobs.decay_fact_confidence(db) == 0


async def test_decay_stops_at_the_floor(db):
    await _fact(db, "very old", age_days=900, confidence=0.30)
    assert await jobs.decay_fact_confidence(db) == 0


async def test_exact_duplicate_facts_are_retracted(db):
    for _ in range(3):
        await _fact(db, "vegetarian", predicate="preference")
    assert await jobs.consolidate_duplicate_facts(db) == 2

    live = (await db.execute(
        sa.text(
            "SELECT COUNT(*) FROM memory_facts WHERE retracted_at IS NULL "
            "AND value_text = 'vegetarian'"
        )
    )).scalar_one()
    assert live == 1


async def test_near_duplicates_are_left_alone(db):
    """Deciding two differently-worded facts are 'the same' is a judgement call
    that silently destroys information when it goes wrong."""
    await _fact(db, "vegetarian")
    await _fact(db, "mostly vegetarian")
    assert await jobs.consolidate_duplicate_facts(db) == 0


async def test_conflict_sweep_queues_once(db):
    for minor in (1_650_000, 1_800_000):
        await db.execute(
            sa.text(
                "INSERT INTO memory_facts (namespace, entity, predicate, value, "
                "value_text, asserted_by, valid_from) VALUES "
                "('finance','user','monthly_rent', CAST(:v AS jsonb), :t, 'vega', now())"
            ),
            {"v": f'{{"minor": {minor}}}', "t": f"rent {minor // 100}"},
        )
    assert await jobs.sweep_conflicts(db) == 1
    assert await jobs.sweep_conflicts(db) == 0


# ── Hints, bills, audio ──────────────────────────────────────────────────────

async def test_expired_unread_hints_are_deleted(db):
    await db.execute(
        sa.text(
            "INSERT INTO memory_hints (target_agent, kind, content, expires_at) "
            "VALUES ('selene','stale','old', now() - interval '1 day')"
        )
    )
    assert await jobs.expire_hints(db) == 1


async def test_live_hints_survive(db):
    await db.execute(
        sa.text(
            "INSERT INTO memory_hints (target_agent, kind, content, expires_at) "
            "VALUES ('selene','fresh','new', now() + interval '2 days')"
        )
    )
    assert await jobs.expire_hints(db) == 0


async def test_overdue_monthly_bills_roll_forward(db):
    await db.execute(
        sa.text(
            "INSERT INTO bills (name, amount_minor, recurrence, next_due, active) "
            "VALUES ('rent', 1800000, 'monthly', current_date - 2, TRUE)"
        )
    )
    assert await jobs.roll_recurring_bills(db) == 1

    next_due = (await db.execute(sa.text("SELECT next_due FROM bills"))).scalar_one()
    assert next_due > __import__("datetime").date.today()


async def test_audio_is_pruned_but_turns_are_kept(db):
    session_id = await _session_row(db)
    await db.execute(
        sa.text(
            "INSERT INTO turns (session_id, agent, role, transcript, audio_ref, created_at) "
            "VALUES (:s,'vega','user','x','/audio/1.opus', now() - interval '10 days')"
        ),
        {"s": session_id},
    )
    assert await jobs.prune_audio(db, days=7) == 1

    row = (await db.execute(sa.text("SELECT transcript, audio_ref FROM turns"))).one()
    assert row.transcript == "x", "turns are kept forever"
    assert row.audio_ref is None


# ── Reminders ────────────────────────────────────────────────────────────────

async def test_due_reminders_fire_exactly_once(db):
    """Remind once, well. A reminder that repeats gets notifications muted."""
    await db.execute(
        sa.text(
            "INSERT INTO reminders (text, due_at, state) "
            "VALUES ('pay rent', now() - interval '1 minute', 'pending')"
        )
    )
    first = await jobs.fire_due_reminders(db)
    assert len(first) == 1
    assert await jobs.fire_due_reminders(db) == []


async def test_a_fired_reminder_still_appears_in_the_briefing(db):
    """Being notified is not being done.

    The scheduler fires due reminders within a minute, so a briefing that only
    looked at 'pending' would essentially never show one — the reminder would be
    flipped to 'notified' before any briefing was built.
    """
    from app.reports import builders

    await db.execute(
        sa.text(
            "INSERT INTO reminders (text, due_at, state) "
            "VALUES ('pay rent', now() - interval '1 minute', 'pending')"
        )
    )
    await jobs.fire_due_reminders(db)

    section = await builders.due_reminders(db, days=2)
    assert section is not None, "a fired-but-incomplete reminder must still surface"
    assert section.data["reminders"][0]["already_notified"] is True


async def test_completed_reminders_leave_the_briefing(db):
    await db.execute(
        sa.text(
            "INSERT INTO reminders (text, due_at, state, completed_at) "
            "VALUES ('done', now() - interval '1 hour', 'completed', now())"
        )
    )
    from app.reports import builders

    assert await builders.due_reminders(db, days=2) is None


async def test_future_reminders_do_not_fire(db):
    await db.execute(
        sa.text(
            "INSERT INTO reminders (text, due_at, state) "
            "VALUES ('later', now() + interval '2 hours', 'pending')"
        )
    )
    assert await jobs.fire_due_reminders(db) == []


# ── The whole sweep ──────────────────────────────────────────────────────────

async def test_run_maintenance_reports_every_job(db):
    result = await jobs.run_maintenance(db)
    for name, _ in jobs.MAINTENANCE_JOBS:
        assert name in result
    assert "ran_at" in result


async def test_one_failing_job_does_not_stop_the_others(db, monkeypatch):
    """Maintenance failing quietly is acceptable; taking the system down is not."""
    async def boom(_db, **_kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(jobs, "expire_hints", boom)
    monkeypatch.setattr(
        jobs, "MAINTENANCE_JOBS",
        (("expire_hints", boom), ("close_stale_sessions", jobs.close_stale_sessions)),
    )

    result = await jobs.run_maintenance(db)
    assert "error" in str(result["expire_hints"])
    assert result["close_stale_sessions"] == 0
