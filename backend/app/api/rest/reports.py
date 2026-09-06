"""Report endpoints.

`?narrate=false` returns the structured data alone — useful for building a
dashboard, and the honest answer when you want the numbers without a model
having touched them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_device_token
from app.db.base import get_db
from app.reports import builders
from app.reports.narrate import BRIEFING_ITEMS, narrate, render_plain

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(require_device_token)],
)


async def _respond(
    db: AsyncSession, report: builders.Report, *, do_narrate: bool, limit: int | None
) -> dict[str, Any]:
    payload = report.as_dict()
    if do_narrate:
        payload["narration"] = await narrate(db, report, limit=limit)
    else:
        payload["narration"] = {
            "text": render_plain(report, limit=limit),
            "narrated": False,
            "sections": [s.key for s in report.sections],
        }
    return payload


@router.get("/briefing")
async def briefing(
    narrate_it: bool = Query(True, alias="narrate"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Today's briefing. Capped at three items — see docs/agents/astraea.md."""
    report = await builders.daily_briefing(db)
    return await _respond(db, report, do_narrate=narrate_it, limit=BRIEFING_ITEMS)


@router.get("/weekly")
async def weekly(
    narrate_it: bool = Query(True, alias="narrate"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The weekly review: trends and cross-domain patterns."""
    report = await builders.weekly_review(db)
    return await _respond(db, report, do_narrate=narrate_it, limit=None)


@router.get("/{domain}")
async def domain(
    domain: str,
    days: int = Query(30, ge=1, le=365),
    narrate_it: bool = Query(True, alias="narrate"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One agent's domain in depth: finance, health, learning, work, or home."""
    if domain not in builders.DOMAINS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown domain {domain!r}; expected one of {list(builders.DOMAINS)}",
        )
    report = await builders.domain_report(db, domain, days=days)
    return await _respond(db, report, do_narrate=narrate_it, limit=None)
