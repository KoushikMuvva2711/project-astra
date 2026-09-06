"""Astraea's curator: consumes the cross-agent event bus, off the response path.

Redis Streams with a consumer group rather than pub/sub, because the curator must
be able to crash and resume from its cursor. Memory that silently drops writes is
worse than no memory — the user cannot tell the difference until it matters.

Everything here is asynchronous by design. Curation adds no latency to a turn: an
agent emits an event and returns immediately, and the hints it produces surface
in some *later* turn's context bundle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.logging_config import get_logger
from app.memory.propagation import Event, hints_for

log = get_logger(__name__)

STREAM = "astra:events"
GROUP = "curator"
CONSUMER = "curator-1"


async def emit(redis: Redis, event: dict[str, Any]) -> str:
    """Publish an event. Called on the response path, so it must stay trivial."""
    return await redis.xadd(
        STREAM,
        {
            "type": event["type"],
            "agent": event["agent"],
            "turn_id": str(event.get("turn_id") or ""),
            "payload": json.dumps(event.get("payload") or {}),
        },
    )


async def ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


class Curator:
    """Consumes events, writes global facts, propagates hints, flags conflicts."""

    def __init__(self, session: AsyncSession, redis: Redis) -> None:
        self.session = session
        self.redis = redis

    async def process(self, event: Event, *, stream_id: str | None = None) -> list[int]:
        """Handle one event. Returns the ids of hints written."""
        await self._record_event(event, stream_id)
        hint_ids = await self._propagate(event)
        await self._extract_facts(event)
        await self._detect_conflicts()
        return hint_ids

    async def _record_event(self, event: Event, stream_id: str | None) -> None:
        """Durable audit trail, and the replay source if the cursor is ever lost."""
        await self.session.execute(
            sa.text(
                "INSERT INTO memory_events "
                "(stream_id, type, agent, payload, source_turn, occurred_at, processed_at) "
                "VALUES (:sid, :type, :agent, CAST(:payload AS jsonb), :turn, now(), now()) "
                "ON CONFLICT (stream_id) DO NOTHING"
            ),
            {
                "sid": stream_id,
                "type": event.type,
                "agent": event.agent,
                "payload": json.dumps(event.payload),
                "turn": event.turn_id,
            },
        )

    async def _propagate(self, event: Event) -> list[int]:
        written: list[int] = []
        for hint in hints_for(event):
            expires = datetime.now(UTC) + timedelta(days=hint.ttl_days)
            hint_id = (
                await self.session.execute(
                    sa.text(
                        "INSERT INTO memory_hints "
                        "(target_agent, kind, content, payload, source_turn, expires_at) "
                        "VALUES (:agent, :kind, :content, CAST(:payload AS jsonb), "
                        "        :turn, :expires) RETURNING id"
                    ),
                    {
                        "agent": hint.target_agent,
                        "kind": hint.kind,
                        "content": hint.content,
                        "payload": json.dumps(event.payload),
                        "turn": event.turn_id,
                        "expires": expires,
                    },
                )
            ).scalar_one()
            written.append(hint_id)

        if written:
            # NB: structlog reserves the keyword `event` for the log message
            # itself, so contextual fields must not be named `event`.
            log.info(
                "curator.propagated",
                event_type=event.type,
                source=event.agent,
                hints=len(written),
            )
        return written

    async def _extract_facts(self, event: Event) -> None:
        """Derive global facts from domain events.

        Rule-based rather than model-based for now. Extraction quality matters
        more than coverage here: a wrong fact in the global namespace is visible
        to every agent, and a small local model is not reliable enough to be
        trusted with that. A cheap cloud model can take this over once one is
        configured, and the episodic log can be reprocessed to backfill.
        """
        if event.type != "expense.logged":
            return

        category = event.payload.get("category")
        if category != "rent":
            return

        amount = event.payload.get("amount_minor")
        if not amount:
            return

        await self.session.execute(
            sa.text(
                "UPDATE memory_facts SET valid_to = now(), retracted_at = NULL "
                "WHERE namespace = 'global' AND predicate = 'monthly_rent' "
                "  AND valid_to IS NULL AND retracted_at IS NULL"
            )
        )
        await self.session.execute(
            sa.text(
                "INSERT INTO memory_facts "
                "(namespace, entity, predicate, value, value_text, asserted_by, valid_from, "
                " source_turn, confidence) "
                "VALUES ('global','user','monthly_rent', CAST(:v AS jsonb), :t, 'astraea', "
                "        now(), :turn, 0.8)"
            ),
            {
                "v": json.dumps({"minor": amount}),
                "t": f"rent appears to be {amount // 100} rupees",
                "turn": event.turn_id,
            },
        )

    async def _detect_conflicts(self) -> None:
        """Queue contradictions for the daily briefing, never mid-conversation."""
        rows = (
            await self.session.execute(
                sa.text(
                    "SELECT f.namespace, f.entity, f.predicate, "
                    "       array_agg(f.id ORDER BY f.recorded_at) AS ids "
                    "FROM memory_facts f "
                    "JOIN fact_predicates p ON p.slug = f.predicate "
                    "WHERE p.cardinality = 'single' "
                    "  AND f.valid_to IS NULL AND f.retracted_at IS NULL "
                    "GROUP BY f.namespace, f.entity, f.predicate HAVING count(*) > 1"
                )
            )
        ).all()

        for row in rows:
            existing = (
                await self.session.execute(
                    sa.text(
                        "SELECT 1 FROM memory_conflicts WHERE namespace = :ns "
                        "AND entity = :e AND predicate = :p AND state = 'open'"
                    ),
                    {"ns": row.namespace, "e": row.entity, "p": row.predicate},
                )
            ).scalar_one_or_none()
            if existing:
                continue

            await self.session.execute(
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
            log.info("curator.conflict_detected", predicate=row.predicate)

    async def drain(self, *, limit: int = 100) -> int:
        """Consume pending events. Used by the worker loop and by tests."""
        await ensure_group(self.redis)
        entries = await self.redis.xreadgroup(
            GROUP, CONSUMER, {STREAM: ">"}, count=limit, block=10
        )
        processed = 0
        for _stream, messages in entries or []:
            for stream_id, fields in messages:
                try:
                    event = Event(
                        type=_text(fields, "type"),
                        agent=_text(fields, "agent"),
                        payload=json.loads(_text(fields, "payload") or "{}"),
                        turn_id=int(_text(fields, "turn_id") or 0) or None,
                    )
                    await self.process(event, stream_id=_key(stream_id))
                    await self.session.commit()
                    await self.redis.xack(STREAM, GROUP, stream_id)
                    processed += 1
                except Exception as exc:
                    # Leave it unacked so it is retried rather than lost.
                    await self.session.rollback()
                    log.error("curator.failed", stream_id=_key(stream_id), error=str(exc))
        return processed


def _key(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _text(fields: dict, key: str) -> str:
    value = fields.get(key.encode()) or fields.get(key)
    return value.decode() if isinstance(value, bytes) else (value or "")


def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)
