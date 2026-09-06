"""Health endpoints.

/health/live is intentionally unauthenticated — the Cloudflare Tunnel and Docker
both need to probe it, and it reveals nothing. Everything that touches the
database requires the device token.
"""

from typing import Any

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_device_token
from app.config import get_settings
from app.db.base import get_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness only. No dependencies, no data, no auth."""
    return {"status": "ok"}


@router.get("/ready", dependencies=[Depends(require_device_token)])
async def ready(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Readiness: are Postgres, pgvector, Redis, and the migrations all in place?"""
    settings = get_settings()
    checks: dict[str, Any] = {}
    healthy = True

    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
        healthy = False

    try:
        result = await db.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        version = result.scalar_one_or_none()
        checks["pgvector"] = version or "not installed"
        if version is None:
            healthy = False
    except Exception as exc:
        checks["pgvector"] = f"error: {type(exc).__name__}"
        healthy = False

    try:
        result = await db.execute(text("SELECT version_num FROM alembic_version"))
        checks["migration"] = result.scalar_one_or_none() or "none"
    except Exception:
        checks["migration"] = "not migrated"
        healthy = False

    redis: Redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
        healthy = False
    finally:
        await redis.aclose()

    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/agents", dependencies=[Depends(require_device_token)])
async def agents(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Registered agents. Confirms migration 0002 seeded correctly."""
    result = await db.execute(
        text("SELECT name, display_name, namespace, enabled FROM agents ORDER BY name")
    )
    return {
        "agents": [
            {"name": r.name, "display_name": r.display_name,
             "namespace": r.namespace, "enabled": r.enabled}
            for r in result
        ]
    }
