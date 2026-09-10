"""Nova's tools against the real schema.

NOV-08 — resumption after a long gap — is the PRD success criterion, and NOV-06
(a session close always records a next step) is what makes it possible. Both are
enforced in code here rather than asked for in a prompt.

Nova is also the agent actually caught fabricating, so the empty cases get as
much attention as the populated ones: zero projects must produce an explicit,
quotable "none on record" rather than an empty list to narrate around.
"""

import pytest
import sqlalchemy as sa

from app.tools import work  # noqa: F401  — registers Nova's tools
from app.tools.base import ToolContext, ToolNotAllowed, dispatch
from app.tools.work import NOVA_TOOLS

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(db, turn_id):
    return ToolContext(session=db, agent="nova", turn_id=turn_id)


async def call(name, ctx, **args):
    return await dispatch(name, args, allowed=NOVA_TOOLS, context=ctx)


# ── The empty state, which is where fabrication happens ──────────────────────

async def test_no_projects_is_stated_as_a_fact_not_an_empty_list(ctx):
    result = await call("project_list", ctx)
    assert result.ok
    assert result.data["count"] == 0
    assert "no active projects on record" in result.message.lower()


async def test_resuming_with_nothing_on_record_says_so(ctx):
    """The exact turn Nova answered by inventing an auth module."""
    result = await call("project_resume", ctx)
    assert result.data["found"] is False
    assert result.data["projects_on_record"] == 0
    assert "no projects on record" in result.message.lower()


async def test_resuming_a_project_that_does_not_exist_fails_loudly(ctx):
    await call("project_create", ctx, name="Astra")
    result = await call("project_resume", ctx, project="nonexistent thing")
    assert not result.ok
    assert result.data["projects_on_record"] == 1


# ── Projects ─────────────────────────────────────────────────────────────────

async def test_project_is_created_with_a_slug(ctx, db):
    result = await call("project_create", ctx, name="Project Astra")
    assert result.ok

    row = (await db.execute(sa.text("SELECT slug, name, status FROM projects"))).one()
    assert (row.slug, row.status) == ("project-astra", "active")


async def test_duplicate_project_is_refused_rather_than_duplicated(ctx, db):
    await call("project_create", ctx, name="Astra")
    result = await call("project_create", ctx, name="astra")
    assert not result.ok

    count = (await db.execute(sa.text("SELECT COUNT(*) FROM projects"))).scalar_one()
    assert count == 1


async def test_project_list_reports_open_task_counts(ctx):
    await call("project_create", ctx, name="Astra")
    await call("task_create", ctx, title="wire up the router", project="Astra")
    await call("task_create", ctx, title="write the tests", project="Astra")

    result = await call("project_list", ctx)
    assert result.data["count"] == 1
    assert result.data["projects"][0]["open_tasks"] == 2


# ── NOV-06: a session close always records a next step ───────────────────────

async def test_recording_state_without_a_next_step_asks_instead_of_writing(ctx, db):
    """A row without next_step looks like captured state and restores nothing."""
    await call("project_create", ctx, name="Astra")

    result = await call(
        "project_record_state", ctx, project="Astra",
        summary="worked on the memory layer", completed=["curator loop"],
    )
    assert result.needs_confirmation
    assert result.data["missing"] == "next_step"

    count = (await db.execute(sa.text("SELECT COUNT(*) FROM project_sessions"))).scalar_one()
    assert count == 0, "a session without a next step must not be written"


async def test_a_blank_next_step_counts_as_missing(ctx, db):
    await call("project_create", ctx, name="Astra")
    result = await call(
        "project_record_state", ctx, project="Astra", summary="s", next_step="   "
    )
    assert result.needs_confirmation

    count = (await db.execute(sa.text("SELECT COUNT(*) FROM project_sessions"))).scalar_one()
    assert count == 0


async def test_recording_state_with_a_next_step_writes_and_touches_the_project(ctx, db):
    await call("project_create", ctx, name="Astra")
    result = await call(
        "project_record_state", ctx, project="Astra",
        summary="memory layer", completed=["curator loop"],
        in_flight="conflict detection", blocked_on="pgvector index choice",
        next_step="finish the conflict queue",
    )
    assert result.ok

    row = (
        await db.execute(
            sa.text("SELECT summary, completed, in_flight, blocked_on, next_step "
                    "FROM project_sessions")
        )
    ).one()
    assert row.next_step == "finish the conflict queue"
    assert row.completed == ["curator loop"]
    assert row.blocked_on == "pgvector index choice"

    touched = (
        await db.execute(sa.text("SELECT last_worked_at FROM projects"))
    ).scalar_one()
    assert touched is not None


# ── NOV-08: resumption after a long gap ──────────────────────────────────────

async def test_resumption_after_a_thirty_day_gap_restores_full_state(ctx, db):
    """The PRD success criterion. Every field needed to restore working state
    comes back as data, so nothing depends on the model recalling it."""
    await call("project_create", ctx, name="Astra")
    await call(
        "project_record_state", ctx, project="Astra",
        summary="built the memory layer",
        completed=["curator loop", "bitemporal facts"],
        in_flight="conflict detection",
        blocked_on="whether to use HNSW or IVFFlat",
        next_step="benchmark both index types",
    )
    await call("task_create", ctx, title="benchmark HNSW", project="Astra", priority=1)

    # Age everything by 30 days.
    await db.execute(
        sa.text("UPDATE project_sessions SET recorded_at = now() - interval '30 days'")
    )
    await db.execute(
        sa.text("UPDATE projects SET last_worked_at = now() - interval '30 days'")
    )

    result = await call("project_resume", ctx)
    data = result.data

    assert data["found"] is True
    assert data["project"] == "Astra"
    assert data["days_since_session"] == 30
    assert data["completed"] == ["curator loop", "bitemporal facts"]
    assert data["in_flight"] == "conflict detection"
    assert data["blocked_on"] == "whether to use HNSW or IVFFlat"
    assert data["next_step"] == "benchmark both index types"
    assert data["open_tasks"][0]["title"] == "benchmark HNSW"

    # NOV-01: the spoken form names the gap, the last finished item, and the next action.
    assert "30 days ago" in result.message
    assert "bitemporal facts" in result.message
    assert "benchmark both index types" in result.message


async def test_resume_reports_that_the_repo_is_not_indexed(ctx):
    """NOV-07, as data rather than something Nova must remember to disclaim."""
    await call("project_create", ctx, name="Astra", repo_path="/home/k/astra")
    result = await call("project_resume", ctx)
    assert result.data["repo_indexed"] is False


async def test_a_project_with_no_recorded_session_says_state_was_never_captured(ctx):
    await call("project_create", ctx, name="Astra")
    result = await call("project_resume", ctx)
    assert result.data["has_recorded_state"] is False
    assert "never captured" in result.message


async def test_resume_picks_the_most_recently_worked_project(ctx, db):
    await call("project_create", ctx, name="Old Thing")
    await call("project_create", ctx, name="Current Thing")
    await db.execute(
        sa.text("UPDATE projects SET last_worked_at = now() - interval '90 days' "
                "WHERE name = 'Old Thing'")
    )
    await db.execute(
        sa.text("UPDATE projects SET last_worked_at = now() WHERE name = 'Current Thing'")
    )

    result = await call("project_resume", ctx)
    assert result.data["project"] == "Current Thing"


# ── Tasks ────────────────────────────────────────────────────────────────────

async def test_task_is_created_against_a_project(ctx, db):
    await call("project_create", ctx, name="Astra")
    result = await call("task_create", ctx, title="wire the router", project="Astra", priority=1)
    assert result.ok

    row = (
        await db.execute(
            sa.text("SELECT t.title, t.priority, t.state, p.name FROM tasks t "
                    "JOIN projects p ON p.id = t.project_id")
        )
    ).one()
    assert (row.title, row.priority, row.state) == ("wire the router", 1, "todo")


async def test_a_task_can_exist_without_a_project(ctx, db):
    result = await call("task_create", ctx, title="renew the domain")
    assert result.ok
    assert result.data["project"] is None


async def test_priority_is_clamped_to_the_allowed_range(ctx, db):
    """The check constraint would reject 9; clamping keeps a sloppy model call
    from turning into a failed write."""
    await call("task_create", ctx, title="a", priority=9)
    await call("task_create", ctx, title="b", priority=-2)

    rows = (await db.execute(sa.text("SELECT priority FROM tasks ORDER BY title"))).scalars().all()
    assert rows == [5, 1]


async def test_task_due_date_is_parsed_from_speech(ctx, db):
    await call("task_create", ctx, title="submit the form", due="friday")
    due = (await db.execute(sa.text("SELECT due_on FROM tasks"))).scalar_one()
    assert due is not None


async def test_task_list_orders_by_priority(ctx):
    await call("task_create", ctx, title="low priority thing", priority=5)
    await call("task_create", ctx, title="urgent thing", priority=1)

    result = await call("task_list", ctx)
    assert [t["title"] for t in result.data["tasks"]] == ["urgent thing", "low priority thing"]


async def test_no_open_tasks_is_stated_plainly(ctx):
    result = await call("task_list", ctx)
    assert result.data["count"] == 0
    assert "no open tasks" in result.message.lower()


async def test_completing_a_task_reports_what_is_left(ctx, db):
    await call("task_create", ctx, title="first thing")
    await call("task_create", ctx, title="second thing")

    result = await call("task_complete", ctx, match="first")
    assert result.ok
    assert result.data["remaining"] == 1

    state = (
        await db.execute(sa.text("SELECT state FROM tasks WHERE title = 'first thing'"))
    ).scalar_one()
    assert state == "done"


async def test_ambiguous_completion_asks_which_one(ctx):
    await call("task_create", ctx, title="write the router tests")
    await call("task_create", ctx, title="write the memory tests")

    result = await call("task_complete", ctx, match="write the")
    assert result.needs_confirmation
    assert len(result.data["candidates"]) == 2


async def test_completing_a_missing_task_fails_loudly(ctx):
    result = await call("task_complete", ctx, match="nothing like this")
    assert not result.ok


async def test_recording_a_blocker_also_sets_the_state(ctx, db):
    """Otherwise blocked_reason and state can drift apart."""
    await call("task_create", ctx, title="deploy to oracle")
    result = await call(
        "task_update", ctx, match="oracle", blocked_reason="waiting on the account"
    )
    assert result.data["state"] == "blocked"

    row = (
        await db.execute(sa.text("SELECT state, blocked_reason FROM tasks"))
    ).one()
    assert (row.state, row.blocked_reason) == ("blocked", "waiting on the account")


async def test_blocked_tasks_can_be_listed_on_their_own(ctx):
    await call("task_create", ctx, title="deploy to oracle")
    await call("task_create", ctx, title="write the docs")
    await call("task_update", ctx, match="oracle", blocked_reason="no account yet")

    result = await call("task_list", ctx, state="blocked")
    assert result.data["count"] == 1
    assert result.data["tasks"][0]["blocked_reason"] == "no account yet"


async def test_an_unknown_state_is_refused(ctx):
    await call("task_create", ctx, title="a thing")
    result = await call("task_update", ctx, match="thing", state="finished-ish")
    assert not result.ok


async def test_update_with_nothing_to_change_is_refused(ctx):
    await call("task_create", ctx, title="a thing")
    result = await call("task_update", ctx, match="thing")
    assert not result.ok


# ── Boundaries ───────────────────────────────────────────────────────────────

async def test_nova_cannot_write_an_expense(ctx):
    with pytest.raises(ToolNotAllowed):
        await dispatch(
            "expense_log", {"text": "spent 200"}, allowed=NOVA_TOOLS, context=ctx
        )


async def test_nova_cannot_set_a_reminder(ctx):
    """Reminders are Selene's. Enforced by the dispatcher, not the prompt."""
    with pytest.raises(ToolNotAllowed):
        await dispatch(
            "reminder_create", {"text": "remind me tomorrow"},
            allowed=NOVA_TOOLS, context=ctx,
        )


async def test_astraea_may_read_work_data_but_not_write_it():
    from app.orchestration.agents.spec import ASTRAEA

    assert "project_list" in ASTRAEA.tools
    assert "task_list" in ASTRAEA.tools
    assert "project_record_state" not in ASTRAEA.tools
    assert "task_create" not in ASTRAEA.tools


async def test_every_nova_tool_is_registered():
    from app.tools.base import REGISTRY

    missing = [name for name in NOVA_TOOLS if REGISTRY.get(name) is None]
    assert not missing
