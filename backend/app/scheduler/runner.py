"""The scheduler process.

Runs inside the API process for now. When the backend moves to a VPS this is the
piece to split out first — it is the only component that must keep running when
nobody is talking to the system.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from app.db.base import SessionLocal
from app.logging_config import get_logger
from app.memory.curator import Curator, get_redis
from app.scheduler.jobs import fire_due_reminders, run_maintenance

log = get_logger(__name__)

# Cadences. Curation drains often because hints are only useful while fresh;
# maintenance runs hourly because none of it is urgent and all of it is cheap.
CURATOR_INTERVAL_SECONDS = 20
REMINDER_INTERVAL_SECONDS = 60
MAINTENANCE_INTERVAL_SECONDS = 3600


class Scheduler:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self.last_run: dict[str, Any] = {}

    async def _loop(self, name: str, interval: int, coro) -> None:
        """Run `coro` forever on an interval, surviving individual failures.

        A crash in one loop must not stop the others, and must not spin: on
        failure we wait the full interval before retrying rather than hot-looping
        against a broken database.
        """
        while not self._stopping.is_set():
            started = datetime.now(UTC)
            try:
                async with SessionLocal() as db:
                    result = await coro(db)
                    await db.commit()
                self.last_run[name] = {
                    "at": started.isoformat(),
                    "ok": True,
                    "result": result,
                }
            except Exception as exc:
                self.last_run[name] = {
                    "at": started.isoformat(),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                log.error("scheduler.loop_failed", loop=name, error=str(exc))

            # Sleep for the interval, but wake immediately on shutdown.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)

    async def _drain_curator(self, db) -> int:
        redis = get_redis()
        try:
            return await Curator(db, redis).drain()
        finally:
            await redis.aclose()

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(
                self._loop("curator", CURATOR_INTERVAL_SECONDS, self._drain_curator)
            ),
            asyncio.create_task(
                self._loop("reminders", REMINDER_INTERVAL_SECONDS, fire_due_reminders)
            ),
            asyncio.create_task(
                self._loop("maintenance", MAINTENANCE_INTERVAL_SECONDS, run_maintenance)
            ),
        ]
        log.info("scheduler.started", loops=len(self._tasks))

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        log.info("scheduler.stopped")

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._tasks) and not self._stopping.is_set(),
            "loops": {
                "curator": {"interval_seconds": CURATOR_INTERVAL_SECONDS},
                "reminders": {"interval_seconds": REMINDER_INTERVAL_SECONDS},
                "maintenance": {"interval_seconds": MAINTENANCE_INTERVAL_SECONDS},
            },
            "last_run": self.last_run,
        }


scheduler = Scheduler()
