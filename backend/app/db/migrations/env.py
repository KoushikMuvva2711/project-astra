from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing app.db.models registers every model on Base.metadata; without it
# autogenerate would see an empty schema and propose dropping everything.
import app.db.models  # noqa: F401
from app.config import get_settings
from app.db.base import Base

config = context.config

# Only fall back to the configured database when the caller has not named one.
# The test harness sets this explicitly to point migrations at the test
# database; overriding it unconditionally would migrate the dev database while
# the tests ran against an empty one.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().sync_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Keep pgvector's internal objects out of autogenerate diffs."""
    return not (type_ == "table" and name == "vector")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
