"""Shared test fixtures.

Tests run against a dedicated `<db>_test` database, created and migrated on
first use. They never touch the development database.

This is not belt-and-braces. Rollback-based isolation is only as strong as the
code under test: `run_maintenance` commits after every job on purpose, so that
one failing job cannot roll back another's work. Run that against a session
whose isolation depends on rollback, and the fixture's TRUNCATE gets committed —
permanently destroying whatever was in the database. That happened. A separate
database makes the whole class of failure impossible rather than relying on
every future job being careful about transactions.
"""

import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

TEST_DB_SUFFIX = "_test"

# Volatile tables cleared at the start of each test. Reference data seeded by
# migrations 0002 and 0004 (agents, categories, predicates, foods) is left alone.
VOLATILE_TABLES = [
    # finance
    "expenses", "budgets", "bills", "subscriptions", "savings_goals",
    # health
    "meal_items", "meals", "exercise_sets", "workouts", "health_samples",
    "body_metrics",
    # home
    "reminders", "inventory_items", "documents", "warranties", "trips",
    # work
    "tasks", "project_sessions", "projects",
    # learning
    "study_sessions", "curriculum_items", "learning_tracks", "papers",
    "applications",
    # memory and core
    "memory_hints", "memory_events", "memory_facts", "memory_conflicts",
    "memory_chunks", "cost_ledger", "session_summaries", "turns", "sessions",
    "users",
]


def _test_database_name() -> str:
    return get_settings().postgres_db + TEST_DB_SUFFIX


def _test_database_url(*, sync: bool = False) -> str:
    settings = get_settings()
    base = settings.sync_database_url if sync else settings.database_url
    return base.rsplit("/", 1)[0] + "/" + _test_database_name()


@pytest.fixture(scope="session", autouse=True)
def test_database() -> str:
    """Create and migrate the test database once per run.

    Synchronous on purpose — CREATE DATABASE cannot run inside a transaction,
    and Alembic is a sync API. Doing this with a plain psycopg2 connection avoids
    the event-loop-scope contortions an async session fixture would need.
    """
    settings = get_settings()
    name = _test_database_name()

    admin_url = settings.sync_database_url.rsplit("/", 1)[0] + "/postgres"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    admin.dispose()

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", _test_database_url(sync=True))
    command.upgrade(config, "head")

    return _test_database_url()


@pytest.fixture
async def db(test_database: str):
    """A rolled-back session on a per-test engine against the test database.

    The app's module-level engine cannot be reused: its pool holds asyncpg
    connections bound to whichever event loop created them, and pytest-asyncio
    gives each test a fresh loop. NullPool plus a per-test engine guarantees
    every connection is opened and disposed inside the loop that uses it.

    TRUNCATE gives each test empty tables. Code under test may commit — that is
    safe here precisely because this is not the development database.
    """
    engine = create_async_engine(test_database, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(
                sa.text(f"TRUNCATE {', '.join(VOLATILE_TABLES)} CASCADE")
            )
            await session.commit()
            yield session
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def turn_id(db: AsyncSession) -> int:
    """A turn to hang provenance off, so source_turn foreign keys resolve."""
    user_id = uuid.uuid4()
    await db.execute(
        sa.text("INSERT INTO users (id, display_name) VALUES (:i, 'Test')"), {"i": user_id}
    )
    session_id = uuid.uuid4()
    await db.execute(
        sa.text("INSERT INTO sessions (id, user_id, active_agent) VALUES (:s, :u, 'vega')"),
        {"s": session_id, "u": user_id},
    )
    result = await db.execute(
        sa.text(
            "INSERT INTO turns (session_id, agent, role, transcript) "
            "VALUES (:s, 'vega', 'user', 'spent 180 on coffee') RETURNING id"
        ),
        {"s": session_id},
    )
    return result.scalar_one()
