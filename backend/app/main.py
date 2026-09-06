"""Project Astra — FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.rest import converse, health, reports
from app.config import get_settings
from app.db.base import engine
from app.logging_config import configure_logging, get_logger
from app.scheduler.runner import scheduler

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(
        "astra.starting",
        env=settings.env,
        model_profile=settings.model_profile,
        timezone=settings.timezone,
    )
    # Self-maintenance runs from process start: closing stale sessions,
    # summarising, decaying confidence, firing reminders, draining the curator.
    # The user should never have to trigger any of it.
    scheduler.start()
    yield
    await scheduler.stop()
    await engine.dispose()
    log.info("astra.stopped")


app = FastAPI(
    title="Project Astra",
    description="Voice-first personal AI constellation",
    version="0.1.0",
    lifespan=lifespan,
    # Docs are useful locally and are attack surface on a public tunnel.
    docs_url="/docs" if get_settings().env == "development" else None,
    redoc_url=None,
)

# The PWA is served from a different origin than the API in development.
# Production tightens this to the tunnel hostname.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"]
    if get_settings().env == "development"
    else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(converse.router)
app.include_router(reports.router)


@app.get("/maintenance")
async def maintenance_status() -> dict:
    """What the scheduler has done and when. The system reporting on itself."""
    return scheduler.status()


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "astra", "version": "0.1.0"}
