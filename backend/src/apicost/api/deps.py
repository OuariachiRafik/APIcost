"""Shared dependencies for the control-plane API.

The important one is :func:`get_current_user`. Besides authenticating the
request, it sets the RLS session variable on the *same* session the route
handlers will use — FastAPI caches dependency results per request, so
``Depends(get_session)`` resolves to one session object throughout.

That coupling is deliberate. If setting ``app.user_id`` were its own optional
dependency, a route could forget it and silently lose the database-layer half
of tenant isolation while still looking correct in review.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apicost.config import Settings, get_settings
from apicost.core.errors import AuthenticationError, NotFoundError
from apicost.core.security import decode_access_token
from apicost.db.models import Project, User
from apicost.db.session import session_scope
from apicost.vault.kms import KMSClient, get_kms_client

__all__ = [
    "CurrentUser",
    "DbSession",
    "Kms",
    "SettingsDep",
    "get_current_user",
    "get_session",
    "require_project",
]

_bearer = HTTPBearer(auto_error=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a transactional session for the request."""
    async with session_scope() as session:
        yield session


def get_settings_dep() -> Settings:
    return get_settings()


def get_kms(settings: Annotated[Settings, Depends(get_settings_dep)]) -> KMSClient:
    return get_kms_client(settings)


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    """Authenticate the bearer token and scope the session to that user."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token")

    claims = decode_access_token(credentials.credentials, settings.jwt_secret.get_secret_value())
    if claims is None:
        raise AuthenticationError("Invalid or expired token")

    user_id = claims.get("sub")
    if not isinstance(user_id, str):
        raise AuthenticationError("Invalid token subject")

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is not active")

    # The database-layer half of tenant isolation (hard rule 5). Transaction
    # local, so a pooled connection cannot carry this into another request.
    await session.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"), {"user_id": user.id}
    )
    return user


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]
Kms = Annotated[KMSClient, Depends(get_kms)]


async def require_project(project_id: str, user: User, session: AsyncSession) -> Project:
    """Load a project the user owns, or 404.

    The ``user_id`` filter is the application-layer control. It is not
    redundant with RLS: both are required, and this is the one that produces a
    clean 404 instead of an empty result the caller has to interpret.
    """
    result = await session.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise NotFoundError("Project not found")
    return project
