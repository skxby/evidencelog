"""Alembic migration environment.

Two things worth knowing:

1. The database URL is resolved as: real environment variable > .env file >
   application settings. It deliberately does NOT just call
   `app.config.get_settings()`: that function is an `lru_cache` singleton, so an
   overridden `DATABASE_URL` inside one process would be silently ignored and the
   migration would run against the *real* database. Silent wrong-target writes are
   exactly what hard rule 4 ("failures must not be silent") forbids.

2. `target_metadata` points at the application's `Base.metadata` so
   `alembic revision --autogenerate` can actually diff the 9 frozen tables.
   Leaving it as None would generate empty migrations without any error - again a
   silent failure.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import dotenv_values
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """Resolve the connection string without trusting the settings cache."""
    for key in ("DATABASE_URL", "database_url"):
        value = os.environ.get(key)
        if value:
            return value
    env_file_values = dotenv_values(".env")
    from_file = env_file_values.get("DATABASE_URL") or env_file_values.get("database_url")
    if from_file:
        return str(from_file)
    return get_settings().database_url


config.set_main_option("sqlalchemy.url", _database_url())

#: The 9 frozen tables. Stage 02 target.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline mode: emit SQL without connecting."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online mode: connect and apply."""
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
            # PostgreSQL can add the deferred events->incidents FK after tables exist
            render_as_batch=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
