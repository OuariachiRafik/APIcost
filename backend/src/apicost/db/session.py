"""Async engine, session factory, and the RLS session variable.

Row-level security (BUILD_SPEC §7) depends on every user-scoped transaction
announcing who it is running as. :func:`session_scope` does that with
``set_config('app.user_id', ..., true)`` — the ``true`` makes it transaction
local, so a pooled connection can never carry one user's identity into another
user's transaction.

``set_config`` is used rather than ``SET LOCAL`` because ``SET LOCAL`` cannot
take a bind parameter, and interpolating a user id into SQL text is exactly the
kind of shortcut that turns an isolation control into an injection point.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apicost.config import Settings, get_settings

__all__ = [
    "check_postgres",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
    "set_rls_user",
]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        cfg = settings or get_settings()
        _engine = create_async_engine(
            cfg.database_url,
            echo=cfg.db_echo,
            pool_size=cfg.db_pool_size,
            max_overflow=cfg.db_max_overflow,
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def dispose_engine() -> None:
    """Tear down the engine and factory. Called from app shutdown and tests."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def set_rls_user(session: AsyncSession, user_id: str) -> None:
    """Scope an already-open session to ``user_id`` for row-level security.

    Needed by the authentication flow, which necessarily starts unscoped: at
    signup there is no user yet, and at token rotation we identify the user by
    looking the token hash up. Once the user is known, every subsequent write
    in that transaction must be scoped, because the RLS policies on
    ``refresh_tokens`` require a match on ``WITH CHECK``.
    """
    await session.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"), {"user_id": user_id}
    )


@asynccontextmanager
async def session_scope(user_id: str | None = None) -> AsyncIterator[AsyncSession]:
    """Yield a session inside a transaction, scoped to ``user_id`` for RLS.

    Passing ``user_id`` is the second of the two required isolation controls;
    the first is an explicit ``WHERE user_id = ...`` in the query itself
    (CLAUDE.md hard rule 5). Both, always.
    """
    factory = get_sessionmaker()
    async with factory() as session, session.begin():
        if user_id is not None:
            await session.execute(
                text("SELECT set_config('app.user_id', :user_id, true)"),
                {"user_id": user_id},
            )
        yield session


async def check_postgres(timeout: float | None = None) -> bool:
    """Readiness probe: can we round-trip a trivial query?

    Returns ``False`` on any failure rather than raising — ``/readyz`` reports
    status, it does not propagate database errors to the caller.
    """
    settings = get_settings()
    budget = timeout if timeout is not None else settings.readiness_timeout_seconds
    try:
        async with asyncio.timeout(budget):
            engine = get_engine(settings)
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
