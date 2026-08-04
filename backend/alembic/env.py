"""Alembic environment, async.

The database URL comes from ``apicost.config.Settings``, never from
``alembic.ini`` and never from a direct environment read (CLAUDE.md hard
rule 8).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from apicost.config import get_settings

# Importing the models module registers every table on Base.metadata so
# `alembic revision --autogenerate` can see them.
from apicost.db import models  # noqa: F401
from apicost.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Migrations run as the schema owner, not as the application role.

    The application connects with a role that has no DDL rights and, crucially,
    cannot bypass row-level security.
    """
    return get_settings().database_admin_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect with the async engine and run migrations."""
    engine = create_async_engine(_database_url(), poolclass=None)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
