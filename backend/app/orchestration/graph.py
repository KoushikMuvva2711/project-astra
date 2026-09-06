"""Turn execution: route, admit, recall, reason, commit.

The lifecycle from docs/conversation-architecture.md §3, minus the voice stages.
Steps 1-2 (capture, transcribe) and 7 (speak) land in Phase 2; the shape here is
built so they slot in without restructuring.

Two things are deliberate:

  * Curation is emitted, never awaited. It must not sit on the response path.
  * Domain writes commit only when the turn succeeds. A partially applied write
    that the agent then reports as saved is the worst outcome in the system.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.provider import Message, ToolCall
from app.llm.registry import get_registry
from app.logging_config import get_logger
from app.memory.store import ContextBundle, MemoryStore
from app.orchestration.agents.spec import AgentSpec, get_spec
from app.orchestration.intents import detect as detect_intent
from app.orchestration.router import RouteMethod, RouteResult, resolve
from app.tools import finance, health  # noqa: F401  — registers agent tools
from app.tools.base import REGISTRY, ToolContext, ToolNotAllowed, dispatch

log = get_logger(__name__)

MAX_TOOL_ITERATIONS = 6


@dataclass
class TurnResult:
    agent: str
    text: str
    route: RouteResult
    session_id: str
    turn_id: int | None = None
    tool_calls: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    degraded: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0


async def _open_session(
    db: AsyncSession, user_id: uuid.UUID, agent: str, channel: str
) -> tuple[str, str | None]:
    """Continue the open session, or open one. Returns (session_id, previous_agent).

    A session times out after ASTRA_SESSION_TIMEOUT_SECONDS of silence, and a
    different wake name closes it — see conversation-architecture.md §2.
    """
    settings = get_settings()
    row = (
        await db.execute(
            sa.text(
                "SELECT id, active_agent FROM sessions "
                "WHERE user_id = :u AND closed_at IS NULL "
                "  AND last_activity_at > now() - make_interval(secs => :timeout) "
                "ORDER BY last_activity_at DESC LIMIT 1"
            ),
            {"u": user_id, "timeout": settings.session_timeout_seconds},
        )
    ).one_or_none()

    if row is not None and row.active_agent == agent:
        await db.execute(
            sa.text("UPDATE sessions SET last_activity_at = now() WHERE id = :i"),
            {"i": row.id},
        )
        return str(row.id), None

    previous = None
    if row is not None:
        previous = row.active_agent
        await db.execute(
            sa.text(
                "UPDATE sessions SET closed_at = now(), close_reason = 'agent_switch' "
                "WHERE id = :i"
            ),
            {"i": row.id},
        )

    session_id = uuid.uuid4()
    await db.execute(
        sa.text(
            "INSERT INTO sessions (id, user_id, active_agent, channel) "
            "VALUES (:i, :u, :a, :c)"
        ),
        {"i": session_id, "u": user_id, "a": agent, "c": channel},
    )
    return str(session_id), previous


async def _active_agent(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    settings = get_settings()
    return (
        await db.execute(
            sa.text(
                "SELECT active_agent FROM sessions "
                "WHERE user_id = :u AND closed_at IS NULL "
                "  AND last_activity_at > now() - make_interval(secs => :timeout) "
                "ORDER BY last_activity_at DESC LIMIT 1"
            ),
            {"u": user_id, "timeout": settings.session_timeout_seconds},
        )
    ).scalar_one_or_none()


async def _record_turn(
    db: AsyncSession,
    *,
    session_id: str,
    agent: str,
    role: str,
    transcript: str,
    route: RouteResult | None = None,
    asr_confidence: float | None = None,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int | None = None,
) -> int:
    return (
        await db.execute(
            sa.text(
                "INSERT INTO turns (session_id, agent, role, transcript, asr_confidence, "
                " route_confidence, route_method, model_used, tokens_in, tokens_out, "
                " latency_ms) "
                "VALUES (CAST(:s AS uuid), :a, :r, :t, :asr, :rc, :rm, :model, :tin, "
                "        :tout, :lat) RETURNING id"
            ),
            {
                "s": session_id,
                "a": agent,
                "r": role,
                "t": transcript,
                "asr": asr_confidence,
                "rc": float(route.confidence) if route else None,
                "rm": route.method.value if route else None,
                "model": model,
                "tin": tokens_in,
                "tout": tokens_out,
                "lat": latency_ms,
            },
        )
    ).scalar_one()


# What each agent's records live in. Used to tell an agent, factually, how much
# it can actually see — see _inventory below.
AGENT_TABLES: dict[str, tuple[str, ...]] = {
    "vega": ("expenses", "budgets", "bills", "subscriptions"),
    "lyra": ("meals", "workouts", "health_samples", "body_metrics"),
    "nova": ("projects", "project_sessions", "tasks"),
    "athena": ("learning_tracks", "curriculum_items", "study_sessions", "applications"),
    "selene": ("reminders", "inventory_items", "documents", "warranties"),
    "astraea": (),
}


async def _inventory(db: AsyncSession, agent: str) -> str | None:
    """A factual statement of what records this agent actually has.

    An agent with no tools and no data has nothing to anchor to, and will fill
    the vacuum: Nova, asked what she was working on with zero projects on file,
    invented a plausible authentication module and a commit history to match.
    No guard caught it — nothing was claimed as saved and no figure was quoted.

    Putting real counts in the context replaces the vacuum with a fact. "You
    have 0 projects on record" is much harder to talk past than an instruction
    not to make things up, because it is evidence rather than a rule.
    """
    tables = AGENT_TABLES.get(agent, ())
    if not tables:
        return None

    counts = []
    for table in tables:
        try:
            n = (await db.execute(sa.text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()
        except Exception:
            continue
        counts.append(f"{table}: {int(n)}")

    if not counts:
        return None

    total = sum(int(c.split(": ")[1]) for c in counts)
    line = "Records you can currently see — " + ", ".join(counts) + "."
    if total == 0:
        line += (
            " You have no data on record. Say so plainly if asked; do not "
            "describe activity you cannot see."
        )
    return line


def _build_messages(
    spec: AgentSpec,
    bundle: ContextBundle,
    utterance: str,
    inventory: str | None = None,
) -> list[Message]:
    """System prompt, retrieved context, then the request."""
    context_lines: list[str] = []
    if inventory:
        context_lines.append(inventory)

    if bundle.global_facts:
        context_lines.append("What you know about them:")
        context_lines += [f"- {f.value_text}" for f in bundle.global_facts]

    if bundle.agent_facts:
        context_lines.append("From your own records:")
        context_lines += [f"- {f.value_text}" for f in bundle.agent_facts]

    if bundle.hints:
        # Cross-agent context the curator delivered. The agent uses it silently;
        # it does not announce that another agent told it something.
        context_lines.append("Recent context from elsewhere in the system:")
        context_lines += [f"- {h.content}" for h in bundle.hints]

    if bundle.recent_turns:
        context_lines.append("Earlier in this conversation:")
        context_lines += bundle.recent_turns

    messages = [Message(role="system", content=spec.system_prompt)]
    if context_lines:
        messages.append(Message(role="system", content="\n".join(context_lines)))
    messages.append(Message(role="user", content=utterance))
    return messages


async def run_turn(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    transcript: str,
    asr_confidence: float | None = None,
    channel: str = "text",
) -> TurnResult:
    """Execute one turn end to end."""
    started = datetime.now(UTC)

    # 3. ROUTE — an open session changes the default for an unnamed utterance.
    current = await _active_agent(db, user_id)
    route = resolve(transcript, active_agent=current)

    if route.needs_clarification:
        session_id, _ = await _open_session(db, user_id, "astraea", channel)
        options = " or ".join(get_spec(a).display_name for a in route.candidates)
        text = f"I didn't catch which agent you meant. Was that {options}?"
        turn_id = await _record_turn(
            db, session_id=session_id, agent="astraea", role="user",
            transcript=transcript, route=route, asr_confidence=asr_confidence,
        )
        await _record_turn(
            db, session_id=session_id, agent="astraea", role="agent", transcript=text,
        )
        return TurnResult(
            agent="astraea", text=text, route=route, session_id=session_id,
            turn_id=turn_id, needs_confirmation=True,
        )

    spec = get_spec(route.agent)

    # 4. ADMIT
    session_id, previous = await _open_session(db, user_id, route.agent, channel)

    turn_id = await _record_turn(
        db, session_id=session_id, agent=route.agent, role="user",
        transcript=transcript, route=route, asr_confidence=asr_confidence,
    )

    # 5. RECALL — degrades component by component; never fails the turn.
    store = MemoryStore(db, route.agent)
    bundle = await store.recall(session_id=session_id)

    # 6. REASON
    registry = get_registry()
    inventory = await _inventory(db, route.agent)
    messages = _build_messages(spec, bundle, route.body or transcript, inventory)
    tool_schemas = REGISTRY.schemas_for(spec.tools)
    context = ToolContext(
        session=db, agent=route.agent, turn_id=turn_id, asr_confidence=asr_confidence
    )

    called: list[str] = []
    events: list[dict[str, Any]] = []
    needs_confirmation = False
    degraded = list(bundle.degraded)
    response = None

    # Deterministic fast path. For unambiguous utterances the tool runs before
    # the model, so the write cannot depend on the model choosing to emit a call.
    # See app/orchestration/intents.py for why this is not an optimisation.
    fast_path_result = None
    intent = detect_intent(route.agent, route.body or transcript)
    if intent is not None and intent.tool in spec.tools:
        result, event = await _run_tool(
            ToolCall(id="intent", name=intent.tool, arguments=intent.arguments),
            spec,
            context,
        )
        fast_path_result = result
        called.append(intent.tool)
        if event:
            events.append(event)
        if result.needs_confirmation:
            needs_confirmation = True

        messages.append(
            Message(
                role="system",
                content=(
                    "This action has already been performed on the user's behalf. "
                    "Report its outcome in your own voice. Quote any figures "
                    "exactly as given. Do not call a tool to repeat it.\n"
                    f"{_render_tool_result(result)}"
                ),
            )
        )
        if intent.blocks_write_tools:
            # Remove write tools for the rest of the turn so the model cannot
            # log the same spend a second time.
            tool_schemas = [
                schema
                for schema in tool_schemas
                if not (t := REGISTRY.get(schema["name"])) or not t.writes
            ]

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await registry.complete(
            messages,
            role=spec.model_role,
            tools=tool_schemas or None,
            max_tokens=spec.max_response_tokens,
            temperature=spec.temperature,
            session=db,
            agent=route.agent,
        )
        if response.degraded:
            degraded.append(response.degraded)

        if not response.wants_tools:
            break

        messages.append(
            Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
        )
        for call in response.tool_calls:
            result, event = await _run_tool(call, spec, context)
            called.append(call.name)
            if event:
                events.append(event)
            if result.needs_confirmation:
                needs_confirmation = True
            messages.append(
                Message(
                    role="tool",
                    content=_render_tool_result(result),
                    tool_call_id=call.id,
                )
            )
    else:
        # Iteration cap hit. Degrade honestly rather than presenting a partial
        # answer as complete.
        degraded.append("tool loop exceeded its iteration budget")

    text = (response.text if response else "").strip() or (
        "I didn't get that saved. Say it again?"
        if needs_confirmation
        else "Sorry, I couldn't complete that."
    )

    # An ambiguous amount is a question, not a failure. Ask it deterministically
    # so the two candidate readings are always named (VEG-06) rather than left
    # to a model that may just say "sorry, say that again".
    question = _clarifying_question(fast_path_result) if fast_path_result else None
    if question:
        text = question
    elif _prefer_tool_wording(fast_path_result, text):
        # On a deterministic turn the tool already knows the answer and its
        # message is written in the agent's own voice, so the model's only
        # contribution is phrasing. A weak model makes that contribution
        # negative: observed outputs included empty strings, leaked tool-call
        # JSON, and one stock sentence repeated for every input.
        #
        # Revisit when a capable model is configured — the model's own phrasing
        # is better when it is actually reading the tool result.
        if not _is_speakable(text):
            degraded.append("model produced unusable text; used the tool's wording")
        text = fast_path_result.message
    else:
        text, suppressed = _guard_false_confirmation(text, wrote=bool(events))
        if suppressed:
            degraded.append("suppressed an unsupported confirmation")

        if fast_path_result is not None:
            text, substituted = _guard_figures(
                text, fast_path_result, intent.tool if intent else None
            )
            if substituted:
                degraded.append("substituted the tool's own wording for the figure")

    latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

    await _record_turn(
        db, session_id=session_id, agent=route.agent, role="agent", transcript=text,
        model=response.model if response else None,
        tokens_in=response.tokens_in if response else 0,
        tokens_out=response.tokens_out if response else 0,
        latency_ms=latency_ms,
    )

    if bundle.hints:
        await store.consume_hints([h.id for h in bundle.hints])

    return TurnResult(
        agent=route.agent,
        text=text,
        route=route,
        session_id=session_id,
        turn_id=turn_id,
        tool_calls=called,
        needs_confirmation=needs_confirmation,
        degraded=degraded,
        events=events,
        latency_ms=latency_ms,
    )


_CONFIRMATION_CLAIM = re.compile(
    r"\b(logged|saved|recorded|noted down|added it|set it|updated|done)\b",
    re.IGNORECASE,
)


def _guard_false_confirmation(text: str, *, wrote: bool) -> tuple[str, bool]:
    """Never let a claimed write stand when no write happened.

    A language model asked to log a spend will happily produce "Logged. 180
    rupees, coffee." whether or not the tool ran — this was observed directly on
    a small local model. The user then stops tracking that spend themselves, and
    every subsequent total is quietly wrong.

    The prompt already forbids this. Prompts are guidance for a cooperative
    model; this holds regardless of which model is behind the agent, which is the
    only reason it is worth having.
    """
    if wrote or not _CONFIRMATION_CLAIM.search(text):
        return text, False
    return (
        "I couldn't save that — nothing was recorded. Say it again?",
        True,
    )


_LEAKED_TOOL_CALL = re.compile(
    r'^\s*[{\[]|"arguments"\s*:|"tool_call|"function"\s*:', re.IGNORECASE
)


def _is_speakable(text: str) -> bool:
    """Reject output that is not a spoken reply.

    Small models sometimes emit nothing at all after a tool result, or emit the
    JSON of another tool call into the text channel — both observed live. Either
    reaching the user is worse than falling back to the tool's own wording, and
    on a voice interface the JSON case is unusable noise.
    """
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    return not _LEAKED_TOOL_CALL.search(stripped)


def _prefer_tool_wording(result, text: str) -> bool:
    """Whether the tool's message should stand in for the model's reply."""
    if result is None or not result.ok or not result.message:
        return False
    # If the model clearly read the result — it echoes a figure the tool
    # returned — keep its phrasing, which is likely richer.
    for value in result.data.values():
        digits = re.sub(r"\D", "", str(value))
        if len(digits) >= 2 and digits in re.sub(r"\D", "", text):
            return False
    return True


def _clarifying_question(result) -> str | None:
    """Name both candidate readings when an amount was ambiguous.

    "Sorry, say that again" makes the user repeat themselves into the same
    misrecognition. Naming the two values lets them answer in one word.
    """
    if not result.needs_confirmation:
        return None

    heard = result.data.get("heard")
    alternative = result.data.get("alternative")
    if heard and alternative:
        return f"Was that {heard} or {alternative}?"
    if heard:
        return f"I heard {heard}. Is that right?"
    return "How much was that?"


# The field each read tool's answer actually turns on. Declared per tool rather
# than inferred from the payload: a result carries several numbers, and guarding
# an incidental one (a `days_left` inside a list) would fire on correct answers.
_HEADLINE_COUNT = {
    "reminder_list": "count",
    "grocery_list": "count",
    "document_expiry": "count",
    "inventory_status": "low_count",
    "workout_history": "session_count",
}

# Counts are small and must be spoken as words — the invariants require it — so a
# digit-substring test would reject "one reminder" as a fabrication. Both forms
# count as stating the number.
_COUNT_WORDS = {
    0: ("zero", "no", "none", "nothing", "empty", "not any"),
    1: ("one", "a single"),
    2: ("two", "both", "a couple"),
    3: ("three",), 4: ("four",), 5: ("five",), 6: ("six",), 7: ("seven",),
    8: ("eight",), 9: ("nine",), 10: ("ten",), 11: ("eleven",), 12: ("twelve",),
}


def _states_count(text: str, value: int) -> bool:
    if re.search(rf"\b{value}\b", text):
        return True
    words = _COUNT_WORDS.get(value)
    return bool(words and re.search(rf"\b(?:{'|'.join(words)})\b", text, re.IGNORECASE))


def _guard_figures(text: str, result, tool_name: str | None = None) -> tuple[str, bool]:
    """Ensure a stated figure is the tool's figure.

    PRD FR-T2 says numbers come from SQL, never from the model. The prompt says
    so too, but a weak model will still narrate around a tool result it did not
    read — one was observed answering "I don't have access to the user's
    information" immediately after a successful query.

    When the response fails to contain the figure the tool returned, the tool's
    own wording is used instead. The agent loses some voice on that turn; it does
    not lose correctness, and correctness is the one thing Vega cannot trade.

    Two shapes of figure are guarded. Money totals compare on digits, because
    they are always spoken as digits and the model may reformat the separators.
    Counts compare on digits *or* the number word, because "one reminder" is the
    required rendering and rejecting it would substitute a correct answer.
    """
    if not result.ok or result.needs_confirmation:
        return text, False

    count_key = _HEADLINE_COUNT.get(tool_name or "")
    if count_key is not None:
        count = result.data.get(count_key)
        if isinstance(count, int) and not _states_count(text, count):
            return result.message or text, True
        return text, False

    figure = result.data.get("total") or result.data.get("amount")
    if not figure:
        return text, False

    # Compare on digits alone: "3,250 rupees" and "3250 rupees" are the same
    # claim, and the model may reformat separators.
    digits = re.sub(r"\D", "", str(figure))
    if digits and digits in re.sub(r"\D", "", text):
        return text, False

    return result.message or str(figure), True


async def _run_tool(call: ToolCall, spec: AgentSpec, context: ToolContext):
    """Dispatch one tool call, translating a refusal into a result the model can read."""
    try:
        result = await dispatch(
            call.name, call.arguments, allowed=spec.tools, context=context
        )
    except ToolNotAllowed as exc:
        log.warning("tool.refused", agent=spec.name, tool=call.name)
        from app.tools.base import ToolResult

        return ToolResult.failure(str(exc)), None

    event = None
    if result.ok and not result.needs_confirmation:
        event = _event_for(call.name, spec, result, context)
    return result, event


def _event_for(tool_name: str, spec: AgentSpec, result, context: ToolContext):
    """Emit the cross-agent event a successful write implies."""
    if tool_name == "expense_log":
        return {
            "type": "expense.logged",
            "agent": spec.name,
            "turn_id": context.turn_id,
            "payload": {
                "category": result.data.get("category"),
                "amount_minor": result.data.get("amount_minor"),
                "expense_id": result.data.get("expense_id"),
            },
        }
    if tool_name == "expense_correct":
        return {
            "type": "expense.corrected",
            "agent": spec.name,
            "turn_id": context.turn_id,
            "payload": {"amount_minor": result.data.get("amount_minor")},
        }
    if tool_name == "reminder_create":
        return {
            "type": "reminder.set",
            "agent": spec.name,
            "turn_id": context.turn_id,
            "payload": {
                "subject": result.data.get("subject"),
                "due_at": result.data.get("due_at"),
                "recurrence": result.data.get("recurrence"),
            },
        }
    if tool_name in ("grocery_add", "inventory_consume", "grocery_clear"):
        return {
            "type": "inventory.changed",
            "agent": spec.name,
            "turn_id": context.turn_id,
            "payload": {
                "item": result.data.get("item"),
                "remaining": result.data.get("remaining"),
                "added_to_list": result.data.get("added_to_list"),
                "restocked": tool_name == "grocery_clear",
            },
        }
    return None


def _render_tool_result(result) -> str:
    """Tool output as the model sees it.

    Figures are presented as already-formatted strings so the model has a correct
    phrasing to quote and no reason to compute one.
    """
    import json

    payload = {"ok": result.ok, "message": result.message, **result.data}
    if result.needs_confirmation:
        payload["ACTION_REQUIRED"] = (
            "Nothing was saved. Ask the user which amount they meant, then call "
            "expense_log again with confirmed_minor set."
        )
    return json.dumps(payload, default=str)


__all__ = ["TurnResult", "run_turn", "RouteMethod"]
