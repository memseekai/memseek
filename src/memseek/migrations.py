"""Programmatic Alembic integration for the operational CLI."""

from __future__ import annotations

import hashlib
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command

_INITIAL_MIGRATION = "001_init.sql"
_INITIAL_MIGRATION_SHA256 = "8c21d7f08b9b56e71955f0b42363a10bb0ca4ff38b77078483e6b46730f1c161"


class MigrationError(RuntimeError):
    """Raised when the Alembic environment or a normative migration is invalid."""


class NormativeMigrationChangedError(MigrationError):
    """Raised when the immutable normative SQL no longer matches its revision."""


def normalize_database_url(database_url: str) -> str:
    """Select SQLAlchemy's psycopg driver without changing URL credentials."""

    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    elif url.drivername != "postgresql+psycopg":
        raise MigrationError(f"unsupported migration database driver: {url.drivername}")
    return url.render_as_string(hide_password=False)


def verify_normative_migration(directory: Path) -> Path:
    """Verify the external SQL asset embedded by the initial Alembic revision."""

    path = directory / _INITIAL_MIGRATION
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"cannot read normative migration: {path}") from exc
    checksum = hashlib.sha256(raw).hexdigest()
    if checksum != _INITIAL_MIGRATION_SHA256:
        raise NormativeMigrationChangedError(
            f"normative migration checksum mismatch: {_INITIAL_MIGRATION}"
        )
    return path


def build_alembic_config(
    database_url: str,
    config_path: Path = Path("alembic.ini"),
    migrations_path: Path = Path("migrations"),
) -> Config:
    """Build an Alembic config without persisting database credentials to disk."""

    if not config_path.is_file():
        raise MigrationError(f"Alembic configuration does not exist: {config_path}")
    verify_normative_migration(migrations_path)
    config = Config(str(config_path))
    config.attributes["database_url"] = normalize_database_url(database_url)
    config.attributes["normative_migrations_path"] = str(migrations_path.resolve())
    return config


def _upgrade_database(connection: Connection, config: Config) -> None:
    """Run Alembic with the synchronous facade of an async connection."""

    config.attributes["connection"] = connection
    try:
        command.upgrade(config, "head")
    finally:
        config.attributes.pop("connection", None)


async def apply_migrations(
    database_url: str,
    config_path: Path = Path("alembic.ini"),
    migrations_path: Path = Path("migrations"),
) -> str:
    """Upgrade to Alembic head without blocking the application's event loop."""

    config = build_alembic_config(database_url, config_path, migrations_path)
    scripts = ScriptDirectory.from_config(config)
    head = scripts.get_current_head()
    if head is None:
        raise MigrationError("Alembic environment has no head revision")

    engine = create_async_engine(
        str(config.attributes["database_url"]),
        poolclass=NullPool,
        connect_args={
            "application_name": "memseek-migrate",
            "options": "-c timezone=UTC",
        },
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade_database, config)
    finally:
        await engine.dispose()
    return head
