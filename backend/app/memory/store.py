"""The memory store: scoped reads and writes over the four tiers.

Every instance carries an agent identity, and every query is filtered by that
agent's access scope before it reaches the database. A confused Lyra asking for
finance facts gets an empty result, not private data — the isolation is
structural, not advisory.

See docs/memory-design.md §5–§6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.namespaces import GLOBAL, AccessScope, scope_for

# Rough char-per-token ratio for English. Good enough for budget enforcement;
# exact counting would need the tokeniser of whichever model is active, which
# varies per agent and is not worth the coupling.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Fact:
    id: int
    namespace: str
    entity: str
    predicate: str
    value: Any
    value_text: str
    confidence: float
    valid_from: datetime
    valid_to: datetime | None
    source_turn: int | None


@dataclass(frozen=True)
class Hint:
    id: int
    kind: str
    content: str
    payload: dict


@dataclass
class ContextBundle:
    """What an agent sees before it reasons.

    Budgets are hard. On overflow the lowest-priority component is dropped whole
    rather than truncated mid-item — a half-included fact is worse than an
    omitted one, because the model treats a fragment as complete.
    """

    agent: str
    global_facts: list[Fact] = field(default_factory=list)
    agent_facts: list[Fact] = field(default_factory=list)
    recent_turns: list[str] = field(default_factory=list)
    semantic: list[str] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    """Components that failed or were dropped. Surfaced to the agent so it can
    say what it cannot see rather than answering as if fully informed."""

    def estimated_tokens(self) -> int:
        chars = sum(
            len(item)
            for item in (
                *(f.value_text for f in self.global_facts),
                *(f.value_text for f in self.agent_facts),
                *self.recent_turns,
                *self.semantic,
                *(h.content for h in self.hints),
            )
        )
        return chars // CHARS_PER_TOKEN


# Per-component token budgets, in drop order (last dropped first).
BUDGETS: dict[str, int] = {
    "global_facts": 250,
    "agent_facts": 400,
    "recent_turns": 500,
    "semantic": 500,
    "hints": 200,
}
TOTAL_BUDGET = sum(BUDGETS.values())


class MemoryStore:
    """Scoped access to memory for one agent."""

    def __init__(self, session: AsyncSession, agent: str) -> None:
        self.session = session
        self.agent = agent
        self.scope: AccessScope = scope_for(agent)

    # ── Facts ────────────────────────────────────────────────────────────────

    async def predicate_cardinality(self, predicate: str) -> str | None:
        row = await self.session.execute(
            sa.text("SELECT cardinality FROM fact_predicates WHERE slug = :p"),
            {"p": predicate},
        )
        return row.scalar_one_or_none()

    async def current_facts(
        self,
        *,
        namespace: str | None = None,
        predicates: list[str] | None = None,
        limit: int = 100,
    ) -> list[Fact]:
        """Facts currently believed and currently true, within the agent's scope."""
        if namespace is not None:
            self.scope.assert_read(namespace)
            namespaces = [namespace]
        else:
            namespaces = sorted(self.scope.readable)

        clauses = [
            "namespace = ANY(:namespaces)",
            "valid_to IS NULL",
            "retracted_at IS NULL",
        ]
        params: dict[str, Any] = {"namespaces": namespaces, "limit": limit}
        if predicates:
            clauses.append("predicate = ANY(:predicates)")
            params["predicates"] = predicates

        result = await self.session.execute(
            sa.text(
                "SELECT id, namespace, entity, predicate, value, value_text, "
                "       confidence, valid_from, valid_to, source_turn "
                f"FROM memory_facts WHERE {' AND '.join(clauses)} "
                "ORDER BY confidence DESC, recorded_at DESC LIMIT :limit"
            ),
            params,
        )
        return [_as_fact(r) for r in result]

    async def facts_as_of(
        self, when: datetime, *, namespace: str, predicate: str | None = None
    ) -> list[Fact]:
        """What was true at a past moment.

        The reason valid time is stored separately from transaction time: this
        answers "what was my rent in March", not "what do I now believe it was".
        """
        self.scope.assert_read(namespace)
        clauses = [
            "namespace = :namespace",
            "valid_from <= :when",
            "(valid_to IS NULL OR valid_to > :when)",
            "retracted_at IS NULL",
        ]
        params: dict[str, Any] = {"namespace": namespace, "when": when}
        if predicate:
            clauses.append("predicate = :predicate")
            params["predicate"] = predicate

        result = await self.session.execute(
            sa.text(
                "SELECT id, namespace, entity, predicate, value, value_text, "
                "       confidence, valid_from, valid_to, source_turn "
                f"FROM memory_facts WHERE {' AND '.join(clauses)} "
                "ORDER BY valid_from DESC"
            ),
            params,
        )
        return [_as_fact(r) for r in result]

    async def record_fact(
        self,
        *,
        namespace: str,
        predicate: str,
        value: dict,
        value_text: str,
        source_turn: int | None = None,
        entity: str = "user",
        confidence: float = 1.0,
        valid_from: datetime | None = None,
    ) -> int:
        """Assert a fact, superseding any conflicting single-valued predecessor.

        Supersession closes the old row's validity window and links it forward.
        Nothing is deleted — "what did I believe in January" stays answerable.
        """
        self.scope.assert_write(namespace)
        valid_from = valid_from or datetime.now(UTC)

        new_id = (
            await self.session.execute(
                sa.text(
                    "INSERT INTO memory_facts "
                    "(namespace, entity, predicate, value, value_text, confidence, "
                    " source_turn, asserted_by, valid_from) "
                    "VALUES (:ns, :entity, :predicate, CAST(:value AS jsonb), :value_text, "
                    "        :confidence, :source_turn, :agent, :valid_from) "
                    "RETURNING id"
                ),
                {
                    "ns": namespace,
                    "entity": entity,
                    "predicate": predicate,
                    "value": _json(value),
                    "value_text": value_text,
                    "confidence": confidence,
                    "source_turn": source_turn,
                    "agent": self.agent,
                    "valid_from": valid_from,
                },
            )
        ).scalar_one()

        # Single-valued predicates supersede; multi-valued accumulate.
        if await self.predicate_cardinality(predicate) == "single":
            await self.session.execute(
                sa.text(
                    "UPDATE memory_facts SET valid_to = :now, superseded_by = :new "
                    "WHERE namespace = :ns AND entity = :entity AND predicate = :predicate "
                    "  AND id <> :new AND valid_to IS NULL AND retracted_at IS NULL"
                ),
                {
                    "now": valid_from,
                    "new": new_id,
                    "ns": namespace,
                    "entity": entity,
                    "predicate": predicate,
                },
            )

        return new_id

    async def retract_fact(self, fact_id: int) -> None:
        """Mark a fact as never having been true — distinct from it becoming untrue."""
        await self.session.execute(
            sa.text(
                "UPDATE memory_facts SET retracted_at = now() "
                "WHERE id = :id AND namespace = ANY(:writable)"
            ),
            {"id": fact_id, "writable": sorted(self.scope.writable)},
        )

    # ── Conflicts ────────────────────────────────────────────────────────────

    async def detect_conflicts(self) -> list[dict]:
        """Single-cardinality predicates with more than one open fact.

        Mechanical because cardinality is declared. Results are queued for
        Astraea's briefing, never raised mid-conversation.
        """
        result = await self.session.execute(
            sa.text(
                "SELECT f.namespace, f.entity, f.predicate, "
                "       array_agg(f.id ORDER BY f.recorded_at) AS ids, count(*) AS n "
                "FROM memory_facts f "
                "JOIN fact_predicates p ON p.slug = f.predicate "
                "WHERE p.cardinality = 'single' "
                "  AND f.valid_to IS NULL AND f.retracted_at IS NULL "
                "  AND f.namespace = ANY(:readable) "
                "GROUP BY f.namespace, f.entity, f.predicate HAVING count(*) > 1"
            ),
            {"readable": sorted(self.scope.readable)},
        )
        return [
            {
                "namespace": r.namespace,
                "entity": r.entity,
                "predicate": r.predicate,
                "fact_ids": list(r.ids),
                "count": r.n,
            }
            for r in result
        ]

    # ── Hints ────────────────────────────────────────────────────────────────

    async def pending_hints(self, limit: int = 10) -> list[Hint]:
        """Unconsumed, unexpired hints addressed to this agent."""
        result = await self.session.execute(
            sa.text(
                "SELECT id, kind, content, payload FROM memory_hints "
                "WHERE target_agent = :agent AND consumed_at IS NULL "
                "  AND expires_at > now() "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"agent": self.agent, "limit": limit},
        )
        return [Hint(id=r.id, kind=r.kind, content=r.content, payload=r.payload) for r in result]

    async def consume_hints(self, hint_ids: list[int]) -> None:
        if not hint_ids:
            return
        await self.session.execute(
            sa.text(
                "UPDATE memory_hints SET consumed_at = now() "
                "WHERE id = ANY(:ids) AND target_agent = :agent"
            ),
            {"ids": hint_ids, "agent": self.agent},
        )

    # ── Semantic recall ──────────────────────────────────────────────────────

    async def semantic_recall(
        self, embedding: list[float], *, k: int = 5
    ) -> list[str]:
        """Nearest chunks within scope, blending similarity with recency.

        Scored 0.6·similarity + 0.3·recency + 0.1·confidence per memory-design.md
        §6. Recency uses a 30-day half-life, so older material must be clearly
        more relevant to displace recent material.
        """
        result = await self.session.execute(
            sa.text(
                "SELECT content, "
                "       (1 - (embedding <=> CAST(:q AS vector))) AS similarity, "
                "       exp(-0.0231 * EXTRACT(EPOCH FROM (now() - occurred_at)) / 86400.0) "
                "         AS recency "
                "FROM memory_chunks WHERE namespace = ANY(:readable) "
                "ORDER BY (0.6 * (1 - (embedding <=> CAST(:q AS vector))) "
                "        + 0.3 * exp(-0.0231 * EXTRACT(EPOCH FROM (now() - occurred_at)) "
                "                    / 86400.0)) DESC "
                "LIMIT :k"
            ),
            {"q": str(embedding), "readable": sorted(self.scope.readable), "k": k},
        )
        return [r.content for r in result]

    async def recent_turns(self, session_id: str, *, limit: int = 6) -> list[str]:
        result = await self.session.execute(
            sa.text(
                "SELECT role, transcript FROM turns WHERE session_id = CAST(:s AS uuid) "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"s": session_id, "limit": limit},
        )
        return [f"{r.role}: {r.transcript}" for r in reversed(list(result))]

    # ── Assembly ─────────────────────────────────────────────────────────────

    async def recall(
        self,
        *,
        session_id: str | None = None,
        query_embedding: list[float] | None = None,
    ) -> ContextBundle:
        """Assemble the context bundle an agent reasons over.

        Every component degrades independently. A failure here must never break
        the turn — an agent that cannot remember says so and continues, whereas
        an agent that cannot write must refuse to confirm. See
        docs/conversation-architecture.md §10.
        """
        bundle = ContextBundle(agent=self.agent)

        try:
            bundle.global_facts = await self.current_facts(namespace=GLOBAL, limit=20)
        except Exception:
            bundle.degraded.append("global_facts")

        owned = sorted(self.scope.writable)[0]
        try:
            bundle.agent_facts = await self.current_facts(namespace=owned, limit=30)
        except Exception:
            bundle.degraded.append("agent_facts")

        if session_id:
            try:
                bundle.recent_turns = await self.recent_turns(session_id)
            except Exception:
                bundle.degraded.append("recent_turns")

        if query_embedding is not None:
            try:
                bundle.semantic = await self.semantic_recall(query_embedding)
            except Exception:
                bundle.degraded.append("semantic")
        else:
            bundle.degraded.append("semantic_unavailable")

        try:
            bundle.hints = await self.pending_hints()
        except Exception:
            bundle.degraded.append("hints")

        _enforce_budget(bundle)
        return bundle


def _enforce_budget(bundle: ContextBundle) -> None:
    """Drop whole components, lowest priority first, until within budget."""
    drop_order = ["semantic", "recent_turns", "hints", "agent_facts"]
    for component in drop_order:
        if bundle.estimated_tokens() <= TOTAL_BUDGET:
            return
        if getattr(bundle, component):
            setattr(bundle, component, [])
            bundle.degraded.append(f"dropped:{component}")


def _as_fact(row) -> Fact:
    return Fact(
        id=row.id,
        namespace=row.namespace,
        entity=row.entity,
        predicate=row.predicate,
        value=row.value,
        value_text=row.value_text,
        confidence=float(row.confidence),
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        source_turn=row.source_turn,
    )


def _json(value: dict) -> str:
    import json

    return json.dumps(value)
