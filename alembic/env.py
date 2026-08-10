"""Alembic environment for the PostgreSQL schema."""

from __future__ import annotations

import os
from logging.config import fileConfig
from typing import Any

from sqlalchemy import Connection, engine_from_config, pool, text

from alembic import context
from memseek.locks import advisory_lock_key
from memseek.migrations import MigrationError, normalize_database_url

config = context.config
if config.config_file_name is not None:
    # Migrations run inside API/worker/test processes whose ``memseek.*``
    # loggers already exist; the fileConfig default would silently disable
    # them for the remainder of the process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None
_MIGRATION_LOCK_KEY = advisory_lock_key("schema-migrations")


def _database_url() -> str:
    database_url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
    database_url = str(database_url or config.get_main_option("sqlalchemy.url"))
    if not database_url:
        raise MigrationError("DATABASE_URL is required for online Alembic commands")
    return normalize_database_url(database_url)


def run_migrations_offline() -> None:
    """Render migrations without opening a database connection."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_online_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        connection.execute(
            text("select pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _MIGRATION_LOCK_KEY},
        )
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations transactionally under a PostgreSQL advisory lock."""

    injected_connection = config.attributes.get("connection")
    if isinstance(injected_connection, Connection):
        _run_online_migrations(injected_connection)
        return

    configuration: dict[str, Any] = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run_online_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
