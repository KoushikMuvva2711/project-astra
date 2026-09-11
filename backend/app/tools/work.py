"""Nova's tools: projects, resumption state, tasks.

Nova's defining capability is NOV-08 — restoring full working state after a gap.
That quality is set at *close* time, not at open time: a session recorded without
`next_step` turns the next resumption into guesswork, which is the failure the
agent exists to prevent. So `project_record_state` refuses to write a session row
without one, and asks instead. A useless row is worse than no row, because it
looks like state was captured when it was not.

Nova is also the agent that has actually been caught inventing — asked what she
was working on with zero projects on file, she produced a plausible
authentication module and a commit history to match. Every read tool here
therefore returns emptiness as an explicit, quotable fact rather than an empty
list the model can narrate around, and `project_resume` reports whether the repo
is indexed (NOV-07) as data rather than leaving it to her to remember to say.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime

import sqlalchemy as sa

from app.tools.base import ToolContext, ToolResult, tool
from app.tools.when import parse_when

MAX_PRIORITY = 5
MIN_PRIORITY = 1


def _slugify(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:64] or "project"


def _days_since(moment: datetime | None) -> int | None:
    return (datetime.now(UTC) - moment).days if moment else None


async def _resolve_project(context: ToolContext, name: str | None):
    """Find a project by name, slug, or fragment. Most recently worked on wins."""
    if name:
        return (
            await context.session.execute(
                sa.text(
                    "SELECT * FROM projects "
                    "WHERE slug = :exact OR name ILIKE :needle OR slug ILIKE :needle "
                    "ORDER BY last_worked_at DESC NULLS LAST LIMIT 1"
                ),
                {"exact": _slugify(name), "needle": f"%{name.strip()}%"},
            )
        ).one_or_none()

    return (
        await context.session.execute(
            sa.text(
                "SELECT * FROM projects WHERE status = 'active' "
                "ORDER BY last_worked_at DESC NULLS LAST, created_at DESC LIMIT 1"
            )
        )
    ).one_or_none()


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #


@tool(
    name="project_create",
    description="Start tracking a project.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "repo_path": {"type": "string", "description": "Local path, if there is one."},
        },
        "required": ["name"],
    },
    writes=True,
)
async def project_create(
    *,
    context: ToolContext,
    name: str,
    description: str | None = None,
    repo_path: str | None = None,
) -> ToolResult:
    slug = _slugify(name)

    existing = (
        await context.session.execute(
            sa.text("SELECT id, name FROM projects WHERE slug = :slug"), {"slug": slug}
        )
    ).one_or_none()
    if existing:
        return ToolResult.failure(
            f"{existing.name} is already tracked.", project_id=existing.id
        )

    project_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO projects (slug, name, description, repo_path, status, source_turn) "
                "VALUES (:slug, :name, :desc, :repo, 'active', :turn) RETURNING id"
            ),
            {
                "slug": slug,
                "name": name.strip()[:96],
                "desc": description,
                "repo": repo_path,
                "turn": context.turn_id,
            },
        )
    ).scalar_one()

    return ToolResult.success(
        f"Tracking {name}.", project_id=project_id, name=name, slug=slug
    )


@tool(
    name="project_list",
    description=(
        "Projects on record. If this returns zero, there are zero — say so and "
        "ask what to track. Do not describe work that is not listed here."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "active, paused, done, abandoned. Default active.",
            }
        },
    },
)
async def project_list(*, context: ToolContext, status: str = "active") -> ToolResult:
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT p.id, p.name, p.slug, p.status, p.last_worked_at, "
                "  (SELECT COUNT(*) FROM tasks t "
                "     WHERE t.project_id = p.id AND t.state IN ('todo','doing','blocked')"
                "  ) AS open_tasks "
                "FROM projects p WHERE p.status = :status "
                "ORDER BY p.last_worked_at DESC NULLS LAST LIMIT 20"
            ),
            {"status": status},
        )
    ).all()

    if not rows:
        # Stated as a fact rather than returned as an empty list. Nova has
        # invented a project from exactly this vacuum.
        return ToolResult.success(
            f"No {status} projects on record.", count=0, projects=[]
        )

    return ToolResult.success(
        f"{len(rows)} {status} projects.",
        count=len(rows),
        projects=[
            {
                "name": r.name,
                "slug": r.slug,
                "open_tasks": int(r.open_tasks),
                "days_since_worked": _days_since(r.last_worked_at),
            }
            for r in rows
        ],
    )


@tool(
    name="project_resume",
    description=(
        "Restore working state on a project: what was finished, what is "
        "mid-flight, what is blocking, and the intended next step. Call this "
        "before answering any 'where was I' question. Quote the fields verbatim."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Name or fragment. Omit for the most recent.",
            }
        },
    },
)
async def project_resume(
    *, context: ToolContext, project: str | None = None
) -> ToolResult:
    """NOV-01 and NOV-08.

    Every field the agent needs to restore state is returned as data, including
    the gap length and whether the repo is indexed. Nothing here depends on the
    model remembering to look something up or to disclaim something.
    """
    target = await _resolve_project(context, project)

    if target is None:
        total = (
            await context.session.execute(sa.text("SELECT COUNT(*) FROM projects"))
        ).scalar_one()
        if total == 0:
            # `message` is spoken verbatim when the model adds nothing, so it
            # carries no instructions to the model. Guidance goes in `data`,
            # which the model reads but never reads out.
            return ToolResult.success(
                "Nothing on record yet.",
                found=False,
                projects_on_record=0,
                guidance="Ask what they are working on.",
            )
        return ToolResult.failure(
            f"No project matching {project!r}. There are {total} on record.",
            found=False,
            projects_on_record=int(total),
        )

    session_row = (
        await context.session.execute(
            sa.text(
                "SELECT summary, completed, in_flight, blocked_on, next_step, recorded_at "
                "FROM project_sessions WHERE project_id = :pid "
                "ORDER BY recorded_at DESC LIMIT 1"
            ),
            {"pid": target.id},
        )
    ).one_or_none()

    open_tasks = (
        await context.session.execute(
            sa.text(
                "SELECT title, state, priority, blocked_reason FROM tasks "
                "WHERE project_id = :pid AND state IN ('todo','doing','blocked') "
                "ORDER BY priority, created_at LIMIT 10"
            ),
            {"pid": target.id},
        )
    ).all()

    data = {
        "found": True,
        "project": target.name,
        "slug": target.slug,
        "days_since_worked": _days_since(target.last_worked_at),
        # NOV-07 as data: Nova must not discuss code as though she has read it.
        "repo_indexed": False,
        "repo_path": target.repo_path,
        "open_tasks": [
            {
                "title": t.title,
                "state": t.state,
                "priority": t.priority,
                "blocked_reason": t.blocked_reason,
            }
            for t in open_tasks
        ],
    }

    if session_row is None:
        data["has_recorded_state"] = False
        return ToolResult.success(
            f"{target.name}, but the state was never captured. "
            f"{len(open_tasks)} open tasks.",
            guidance="Say the state was never recorded; do not infer what was being done.",
            **data,
        )

    gap = _days_since(session_row.recorded_at)
    data.update(
        has_recorded_state=True,
        last_session_summary=session_row.summary,
        completed=list(session_row.completed or []),
        in_flight=session_row.in_flight,
        blocked_on=session_row.blocked_on,
        next_step=session_row.next_step,
        days_since_session=gap,
    )

    parts = [f"{target.name}, last touched {gap} days ago."]
    if session_row.completed:
        parts.append(f"Finished: {session_row.completed[-1]}.")
    if session_row.in_flight:
        parts.append(f"Mid-flight: {session_row.in_flight}.")
    if session_row.blocked_on:
        parts.append(f"Blocked on: {session_row.blocked_on}.")
    if session_row.next_step:
        parts.append(f"Next: {session_row.next_step}.")

    return ToolResult.success(" ".join(parts), **data)


@tool(
    name="project_record_state",
    description=(
        "Record where a project was left. `next_step` is required — without it "
        "the next resumption is guesswork. If the user has not said what comes "
        "next, ask before calling this."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "summary": {"type": "string", "description": "What this session was."},
            "completed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things actually finished.",
            },
            "in_flight": {"type": "string", "description": "Left half-done."},
            "blocked_on": {"type": "string"},
            "next_step": {"type": "string", "description": "The single next action."},
        },
        "required": ["summary"],
    },
    writes=True,
)
async def project_record_state(
    *,
    context: ToolContext,
    summary: str,
    project: str | None = None,
    completed: list[str] | None = None,
    in_flight: str | None = None,
    blocked_on: str | None = None,
    next_step: str | None = None,
) -> ToolResult:
    """NOV-06, enforced rather than requested.

    The session row's whole value is `next_step`. Writing one without it produces
    a record that looks like captured state and restores nothing, so the tool
    asks instead of writing.
    """
    target = await _resolve_project(context, project)
    if target is None:
        return ToolResult.failure(
            f"No project matching {project!r}."
            if project
            else "No active project to record against."
        )

    if not (next_step and next_step.strip()):
        return ToolResult.confirm(
            f"What's the next action on {target.name}? "
            "Recording a session without one makes the next resumption guesswork.",
            project=target.name,
            missing="next_step",
        )

    session_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO project_sessions "
                "(project_id, summary, completed, in_flight, blocked_on, next_step, source_turn) "
                "VALUES (:pid, :summary, CAST(:completed AS jsonb), :in_flight, "
                "        :blocked, :next, :turn) RETURNING id"
            ),
            {
                "pid": target.id,
                "summary": summary,
                "completed": json.dumps(completed or []),
                "in_flight": in_flight,
                "blocked": blocked_on,
                "next": next_step.strip(),
                "turn": context.turn_id,
            },
        )
    ).scalar_one()

    await context.session.execute(
        sa.text("UPDATE projects SET last_worked_at = now() WHERE id = :pid"),
        {"pid": target.id},
    )

    return ToolResult.success(
        f"Recorded. Next up on {target.name}: {next_step.strip()}.",
        session_id=session_id,
        project=target.name,
        next_step=next_step.strip(),
    )


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


@tool(
    name="task_create",
    description="Add a task. Priority 1 is highest, 5 lowest; 3 is the default.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "project": {"type": "string", "description": "Name or fragment. Optional."},
            "detail": {"type": "string"},
            "priority": {"type": "integer"},
            "estimate_min": {"type": "integer"},
            "due": {"type": "string", "description": "Spoken date, e.g. 'friday'."},
        },
        "required": ["title"],
    },
    writes=True,
)
async def task_create(
    *,
    context: ToolContext,
    title: str,
    project: str | None = None,
    detail: str | None = None,
    priority: int = 3,
    estimate_min: int | None = None,
    due: str | None = None,
) -> ToolResult:
    target = await _resolve_project(context, project) if project else None
    if project and target is None:
        return ToolResult.failure(f"No project matching {project!r}.")

    priority = max(MIN_PRIORITY, min(MAX_PRIORITY, int(priority)))

    due_on: date | None = None
    if due:
        parsed = parse_when(due)
        due_on = parsed.at.date() if parsed.at else None

    task_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO tasks "
                "(project_id, title, detail, state, priority, estimate_min, due_on, source_turn) "
                "VALUES (:pid, :title, :detail, 'todo', :priority, :est, :due, :turn) "
                "RETURNING id"
            ),
            {
                "pid": target.id if target else None,
                "title": title.strip()[:200],
                "detail": detail,
                "priority": priority,
                "est": estimate_min,
                "due": due_on,
                "turn": context.turn_id,
            },
        )
    ).scalar_one()

    where = f" on {target.name}" if target else ""
    return ToolResult.success(
        f"Added{where}.",
        task_id=task_id,
        title=title.strip(),
        project=target.name if target else None,
        priority=priority,
        due_on=due_on.isoformat() if due_on else None,
    )


@tool(
    name="task_list",
    description=(
        "Open tasks, highest priority first. If this returns zero, there are "
        "zero — do not describe work that is not listed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "state": {
                "type": "string",
                "description": "todo, doing, blocked, done. Omit for all open.",
            },
        },
    },
)
async def task_list(
    *, context: ToolContext, project: str | None = None, state: str | None = None
) -> ToolResult:
    target = await _resolve_project(context, project) if project else None
    if project and target is None:
        return ToolResult.failure(f"No project matching {project!r}.")

    clauses = []
    params: dict = {}
    if target:
        clauses.append("t.project_id = :pid")
        params["pid"] = target.id
    if state:
        clauses.append("t.state = :state")
        params["state"] = state
    else:
        clauses.append("t.state IN ('todo','doing','blocked')")

    rows = (
        await context.session.execute(
            sa.text(
                "SELECT t.title, t.state, t.priority, t.estimate_min, t.due_on, "
                "       t.blocked_reason, p.name AS project "
                "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY t.priority, t.due_on NULLS LAST, t.created_at LIMIT 25"
            ),
            params,
        )
    ).all()

    scope = f" on {target.name}" if target else ""
    if not rows:
        # Reflect the filter. Answering "what's blocked" with "no open tasks" is
        # a different claim from the true one, and a wrong one when tasks exist.
        empty = {
            "blocked": "Nothing blocked",
            "doing": "Nothing in progress",
            "todo": "Nothing queued",
            "done": "Nothing completed",
        }.get(state or "", f"No open tasks{scope}")
        return ToolResult.success(f"{empty}{scope if state else ''}.", count=0, tasks=[])

    blocked = sum(1 for r in rows if r.state == "blocked")
    return ToolResult.success(
        f"{len(rows)} open{scope}" + (f", {blocked} blocked." if blocked else "."),
        count=len(rows),
        blocked=blocked,
        tasks=[
            {
                "title": r.title,
                "state": r.state,
                "priority": r.priority,
                "estimate_min": r.estimate_min,
                "due_on": r.due_on.isoformat() if r.due_on else None,
                "blocked_reason": r.blocked_reason,
                "project": r.project,
            }
            for r in rows
        ],
    )


@tool(
    name="task_complete",
    description="Mark a task done. Match on a fragment of its title.",
    parameters={
        "type": "object",
        "properties": {"match": {"type": "string"}},
        "required": ["match"],
    },
    writes=True,
)
async def task_complete(*, context: ToolContext, match: str) -> ToolResult:
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT t.id, t.title, t.project_id, p.name AS project FROM tasks t "
                "LEFT JOIN projects p ON p.id = t.project_id "
                "WHERE t.state IN ('todo','doing','blocked') AND t.title ILIKE :needle "
                "ORDER BY t.priority LIMIT 2"
            ),
            {"needle": f"%{match.strip()}%"},
        )
    ).all()

    if not rows:
        return ToolResult.failure(f"No open task matching {match!r}.")
    if len(rows) > 1:
        return ToolResult.confirm(
            f"Two match {match!r}: {rows[0].title}, and {rows[1].title}. Which one?",
            candidates=[r.title for r in rows],
        )

    target = rows[0]
    await context.session.execute(
        sa.text(
            "UPDATE tasks SET state = 'done', completed_at = now() WHERE id = :id"
        ),
        {"id": target.id},
    )
    if target.project_id:
        await context.session.execute(
            sa.text("UPDATE projects SET last_worked_at = now() WHERE id = :pid"),
            {"pid": target.project_id},
        )

    remaining = (
        await context.session.execute(
            # The cast is not decoration: asyncpg infers parameter types from
            # context, and a bare `:pid IS NULL` gives it nothing to infer from,
            # so it raises AmbiguousParameterError rather than binding NULL.
            sa.text(
                "SELECT COUNT(*) FROM tasks WHERE state IN ('todo','doing','blocked') "
                "AND (CAST(:pid AS bigint) IS NULL OR project_id = CAST(:pid AS bigint))"
            ),
            {"pid": target.project_id},
        )
    ).scalar_one()

    return ToolResult.success(
        f"Done. {int(remaining)} left.",
        task_id=target.id,
        title=target.title,
        project=target.project,
        remaining=int(remaining),
    )


@tool(
    name="task_update",
    description=(
        "Change a task's priority or state, or record what is blocking it. "
        "Match on a fragment of its title."
    ),
    parameters={
        "type": "object",
        "properties": {
            "match": {"type": "string"},
            "priority": {"type": "integer", "description": "1 highest, 5 lowest."},
            "state": {"type": "string", "description": "todo, doing, blocked, dropped."},
            "blocked_reason": {"type": "string"},
        },
        "required": ["match"],
    },
    writes=True,
)
async def task_update(
    *,
    context: ToolContext,
    match: str,
    priority: int | None = None,
    state: str | None = None,
    blocked_reason: str | None = None,
) -> ToolResult:
    if priority is None and state is None and blocked_reason is None:
        return ToolResult.failure("Nothing to change.")

    if state and state not in ("todo", "doing", "blocked", "dropped"):
        return ToolResult.failure(f"Unknown state {state!r}.")

    target = (
        await context.session.execute(
            sa.text(
                "SELECT id, title FROM tasks WHERE title ILIKE :needle "
                "AND state != 'done' ORDER BY priority LIMIT 1"
            ),
            {"needle": f"%{match.strip()}%"},
        )
    ).one_or_none()

    if target is None:
        return ToolResult.failure(f"No open task matching {match!r}.")

    # `blocked_reason` implies the state, so the two cannot drift apart.
    if blocked_reason and not state:
        state = "blocked"

    row = (
        await context.session.execute(
            sa.text(
                "UPDATE tasks SET "
                "  priority = COALESCE(:priority, priority), "
                "  state = COALESCE(:state, state), "
                "  blocked_reason = COALESCE(:reason, blocked_reason) "
                "WHERE id = :id RETURNING title, priority, state"
            ),
            {
                "priority": max(MIN_PRIORITY, min(MAX_PRIORITY, priority))
                if priority is not None
                else None,
                "state": state,
                "reason": blocked_reason,
                "id": target.id,
            },
        )
    ).one()

    return ToolResult.success(
        f"Updated. {row.title} is {row.state}, priority {row.priority}.",
        task_id=target.id,
        title=row.title,
        priority=row.priority,
        state=row.state,
    )


NOVA_TOOLS: list[str] = [
    "project_create",
    "project_list",
    "project_resume",
    "project_record_state",
    "task_create",
    "task_list",
    "task_complete",
    "task_update",
]

# Astraea reports across domains but never writes a specialist's data.
ASTRAEA_WORK_TOOLS: list[str] = ["project_list", "task_list"]
