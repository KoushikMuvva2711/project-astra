"""Shared FastAPI dependencies."""

import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def require_device_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Bearer-token gate on every data endpoint.

    Astra is fronted by a public Cloudflare Tunnel. This token is the only thing
    between the open internet and the user's health, financial, and personal
    data, so it is checked with a constant-time comparison and there is no
    unauthenticated path to anything that reads the database.

    Cloudflare Access in front of this is strongly preferred in production; the
    token is the floor, not the ceiling.
    """
    settings = get_settings()

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, settings.device_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
