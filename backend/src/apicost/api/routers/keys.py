"""Provider key management — UC-02, UC-03.

The response models here are the enforcement point for "the user never sees
their key again". There is deliberately no field on any response model in this
file capable of carrying key material, so no handler can leak one by
accident — the schema would reject it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from apicost.api.deps import CurrentUser, DbSession, Kms
from apicost.core.errors import ConflictError, NotFoundError
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.db.models import ProviderKey
from apicost.vault.provider_keys import encrypt_provider_key, last4

router = APIRouter(prefix="/keys", tags=["keys"])

_logger = get_logger(__name__)

Provider = Literal["openai", "anthropic", "gemini"]


class AddKeyRequest(BaseModel):
    provider: Provider
    api_key: str = Field(min_length=8, max_length=512)


class KeyResponse(BaseModel):
    """Everything the API will ever say about a stored provider key.

    Exactly the fields BUILD_SPEC §4 P1 permits: "and nothing else".
    """

    id: str
    provider: str
    last4: str
    is_active: bool
    added_at: datetime
    last_used_at: datetime | None


@router.post("", response_model=KeyResponse, status_code=status.HTTP_201_CREATED)
async def add_key(
    payload: AddKeyRequest, user: CurrentUser, session: DbSession, kms: Kms
) -> KeyResponse:
    """Store a provider key.

    Encryption happens before this handler returns, and the plaintext is never
    written anywhere (CLAUDE.md hard rule 4).
    """
    raw_key = payload.api_key.strip()

    existing = await session.execute(
        select(ProviderKey.id).where(
            ProviderKey.user_id == user.id,
            ProviderKey.provider == payload.provider,
            ProviderKey.is_active.is_(True),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError(
            f"An active {payload.provider} key already exists. Delete it first to rotate (UC-03)."
        )

    encrypted = await encrypt_provider_key(kms, raw_key)

    key = ProviderKey(
        id=new_id(),
        user_id=user.id,
        provider=payload.provider,
        encrypted_key=encrypted.encrypted_key,
        wrapped_data_key=encrypted.wrapped_data_key,
        nonce=encrypted.nonce,
        key_last4=last4(raw_key),
    )
    session.add(key)
    await session.flush()

    # Note what happened, never what the key was.
    _logger.info("provider_key_added", user_id=user.id, provider=payload.provider, key_id=key.id)

    return KeyResponse(
        id=key.id,
        provider=key.provider,
        last4=key.key_last4,
        is_active=key.is_active,
        added_at=key.added_at,
        last_used_at=key.last_used_at,
    )


@router.get("", response_model=list[KeyResponse])
async def list_keys(user: CurrentUser, session: DbSession) -> list[KeyResponse]:
    """List stored provider keys, metadata only."""
    result = await session.execute(
        select(ProviderKey)
        .where(ProviderKey.user_id == user.id)
        .order_by(ProviderKey.added_at.desc())
    )
    return [
        KeyResponse(
            id=key.id,
            provider=key.provider,
            last4=key.key_last4,
            is_active=key.is_active,
            added_at=key.added_at,
            last_used_at=key.last_used_at,
        )
        for key in result.scalars()
    ]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(key_id: str, user: CurrentUser, session: DbSession) -> None:
    """Remove a provider key (UC-03).

    Hard delete, not a flag: the ciphertext has no further purpose, and the
    least risky place for a former credential is nowhere.
    """
    result = await session.execute(
        select(ProviderKey).where(ProviderKey.id == key_id, ProviderKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise NotFoundError("Provider key not found")

    await session.delete(key)
    _logger.info(
        "provider_key_deleted",
        user_id=user.id,
        provider=key.provider,
        key_id=key_id,
        at=datetime.now(UTC).isoformat(),
    )
