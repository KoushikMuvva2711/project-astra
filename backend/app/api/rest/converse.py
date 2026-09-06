"""Text conversation endpoint — the Phase 1 vertical slice.

Voice arrives in Phase 2 as a WebSocket over the same orchestration. Keeping
Phase 1 text-only is deliberate: debugging memory and routing through an audio
pipeline is miserable, and the hard, novel part of this system is the memory
layer, which is far easier to get right while everything is still inspectable.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_device_token
from app.db.base import get_db
from app.logging_config import get_logger
from app.memory.curator import emit, get_redis
from app.orchestration.graph import run_turn

log = get_logger(__name__)

router = APIRouter(prefix="/converse", tags=["converse"])


class ConverseRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    asr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    channel: str = Field(default="text")


class ConverseResponse(BaseModel):
    agent: str
    display_name: str
    text: str
    session_id: str
    turn_id: int | None
    route_method: str
    route_confidence: float
    tool_calls: list[str]
    needs_confirmation: bool
    degraded: list[str]
    latency_ms: int


async def _default_user(db: AsyncSession) -> uuid.UUID:
    """Astra is single-tenant, but the user is a real row rather than an implicit
    global, so multi-user is a schema change rather than a rewrite."""
    existing = (
        await db.execute(sa.text("SELECT id FROM users ORDER BY created_at LIMIT 1"))
    ).scalar_one_or_none()
    if existing:
        return existing

    user_id = uuid.uuid4()
    await db.execute(
        sa.text("INSERT INTO users (id, display_name) VALUES (:i, 'Koushik')"),
        {"i": user_id},
    )
    return user_id


@router.post("", dependencies=[Depends(require_device_token)])
async def converse(
    request: ConverseRequest, db: AsyncSession = Depends(get_db)
) -> ConverseResponse:
    from app.orchestration.agents.spec import get_spec

    user_id = await _default_user(db)

    try:
        result = await run_turn(
            db,
            user_id=user_id,
            transcript=request.text,
            asr_confidence=request.asr_confidence,
            channel=request.channel,
        )
    except Exception as exc:
        log.error("converse.failed", error=str(exc), text=request.text[:120])
        raise HTTPException(status_code=500, detail=f"turn failed: {exc}") from exc

    # Events go to the bus after the turn is committed and never block the
    # response. Curation happens in a separate worker.
    if result.events:
        redis = get_redis()
        try:
            for event in result.events:
                await emit(redis, event)
        finally:
            await redis.aclose()

    return ConverseResponse(
        agent=result.agent,
        display_name=get_spec(result.agent).display_name,
        text=result.text,
        session_id=result.session_id,
        turn_id=result.turn_id,
        route_method=result.route.method.value,
        route_confidence=float(result.route.confidence),
        tool_calls=result.tool_calls,
        needs_confirmation=result.needs_confirmation,
        degraded=result.degraded,
        latency_ms=result.latency_ms,
    )


@router.get("/cost", dependencies=[Depends(require_device_token)])
async def cost(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Month-to-date cloud spend against the ceiling, broken down by agent and model."""
    from app.llm.registry import get_registry

    return await get_registry().spend_report(db)


@router.post("/curate", dependencies=[Depends(require_device_token)])
async def curate(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Drain the event bus on demand.

    The curator normally runs as a background worker. This endpoint exists so a
    test or a developer can force a drain and observe propagation deterministically
    rather than racing a timer.
    """
    from app.memory.curator import Curator

    redis = get_redis()
    try:
        processed = await Curator(db, redis).drain()
    finally:
        await redis.aclose()
    return {"processed": processed}
