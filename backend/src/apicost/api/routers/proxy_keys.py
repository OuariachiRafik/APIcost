"""Proxy key issuance and revocation — UC-05, UC-07.

Two properties this module is responsible for:

* The raw ``apc_live_...`` value is returned **exactly once**, at creation.
  Only its SHA-256 hash is stored, so nothing — not a database dump, not a
  support request, not another endpoint — can recover it afterwards.
* Revocation takes effect in under a second. That requires purging the Redis
  auth cache in the same operation as the database write; the DB row alone
  would leave the key working until the 60 s cache TTL lapsed (UC-07).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from apicost.api.deps import CurrentUser, DbSession, require_project
from apicost.core.errors import NotFoundError
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.core.security import generate_proxy_key
from apicost.db.models import ProxyKey
from apicost.db.redis import get_redis
from apicost.proxy.auth import purge_auth_cache

router = APIRouter(tags=["proxy-keys"])

_logger = get_logger(__name__)


class CreateProxyKeyRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)


class ProxyKeyResponse(BaseModel):
    id: str
    project_id: str
    name: str | None
    last4: str
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class CreatedProxyKeyResponse(ProxyKeyResponse):
    """Creation response only.

    ``key`` is the single time the raw value exists outside the caller's
    machine. It is absent from every other response model in this file.
    """

    key: str


@router.post(
    "/projects/{project_id}/proxy-keys",
    response_model=CreatedProxyKeyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_proxy_key(
    project_id: str,
    payload: CreateProxyKeyRequest,
    user: CurrentUser,
    session: DbSession,
) -> CreatedProxyKeyResponse:
    """Issue a proxy key for a project. The raw key is shown once."""
    project = await require_project(project_id, user, session)

    raw_key, key_hash, key_last4 = generate_proxy_key()
    proxy_key = ProxyKey(
        id=new_id(),
        user_id=user.id,
        project_id=project.id,
        proxy_key_hash=key_hash,
        key_last4=key_last4,
        name=payload.name,
    )
    session.add(proxy_key)
    await session.flush()

    _logger.info("proxy_key_issued", user_id=user.id, project_id=project.id, key_id=proxy_key.id)

    return CreatedProxyKeyResponse(
        id=proxy_key.id,
        project_id=project.id,
        name=proxy_key.name,
        last4=key_last4,
        created_at=proxy_key.created_at,
        revoked_at=None,
        last_used_at=None,
        key=raw_key,
    )


@router.get("/projects/{project_id}/proxy-keys", response_model=list[ProxyKeyResponse])
async def list_proxy_keys(
    project_id: str, user: CurrentUser, session: DbSession
) -> list[ProxyKeyResponse]:
    project = await require_project(project_id, user, session)
    result = await session.execute(
        select(ProxyKey)
        .where(ProxyKey.project_id == project.id, ProxyKey.user_id == user.id)
        .order_by(ProxyKey.created_at.desc())
    )
    return [
        ProxyKeyResponse(
            id=key.id,
            project_id=key.project_id,
            name=key.name,
            last4=key.key_last4,
            created_at=key.created_at,
            revoked_at=key.revoked_at,
            last_used_at=key.last_used_at,
        )
        for key in result.scalars()
    ]


@router.delete("/proxy-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_proxy_key(key_id: str, user: CurrentUser, session: DbSession) -> None:
    """Revoke a proxy key immediately (UC-07).

    Scoped to the caller's own keys, so revoking one project's key cannot
    affect another user's — or another project's — access.
    """
    result = await session.execute(
        select(ProxyKey).where(ProxyKey.id == key_id, ProxyKey.user_id == user.id)
    )
    proxy_key = result.scalar_one_or_none()
    if proxy_key is None:
        raise NotFoundError("Proxy key not found")

    if proxy_key.revoked_at is None:
        proxy_key.revoked_at = datetime.now(UTC)
        await session.flush()

    # Same operation as the DB write, per CODEBASE_GUIDE §7.2.
    await purge_auth_cache(get_redis(), proxy_key.proxy_key_hash)

    _logger.info(
        "proxy_key_revoked",
        user_id=user.id,
        project_id=proxy_key.project_id,
        key_id=key_id,
    )
