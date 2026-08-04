"""Authentication — UC-01.

Refresh tokens rotate on every use and are tracked as a **family**: all tokens
descending from one login share a ``family_id``. Presenting a token that was
already consumed means the token leaked — the legitimate client would have
rotated past it — so the whole family is revoked rather than just that token
(BUILD_SPEC §4 P1). That turns a stolen refresh token into a detectable event
instead of silent, indefinite access.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update

from apicost.api.deps import CurrentUser, DbSession, SettingsDep
from apicost.core.errors import AuthenticationError, ConflictError
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.core.security import (
    REFRESH_TOKEN_TTL,
    hash_password,
    hash_refresh_token,
    issue_access_token,
    issue_refresh_token,
    needs_rehash,
    verify_password,
)
from apicost.db.models import RefreshToken, User
from apicost.db.session import session_scope, set_rls_user

router = APIRouter(prefix="/auth", tags=["auth"])

_logger = get_logger(__name__)


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    timezone: str = "UTC"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: str
    email: str
    timezone: str
    plan_id: str
    created_at: datetime


def _normalize_email(email: str) -> str:
    return email.strip().lower()


async def _revoke_family(user_id: str, family_id: str) -> None:
    """Revoke every live token in a family, in a transaction of its own.

    Used by the reuse-detection path, which must persist the revocation and
    *then* fail the request.
    """
    async with session_scope(user_id=user_id) as session:
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )


async def _issue_token_pair(
    session: DbSession, user: User, secret: str, family_id: str
) -> TokenResponse:
    # refresh_tokens' RLS policy requires a scoped session on every write, and
    # auth necessarily begins unscoped. Scope it now that the user is known.
    await set_rls_user(session, user.id)

    raw_refresh, refresh_hash = issue_refresh_token()
    session.add(
        RefreshToken(
            id=new_id(),
            user_id=user.id,
            family_id=family_id,
            token_hash=refresh_hash,
            expires_at=datetime.now(UTC) + REFRESH_TOKEN_TTL,
        )
    )
    return TokenResponse(
        access_token=issue_access_token(user.id, secret),
        refresh_token=raw_refresh,
        expires_in=900,
    )


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupRequest, session: DbSession, settings: SettingsDep
) -> TokenResponse:
    """Create an account and log in.

    A duplicate email returns 409, which does disclose that an account exists.
    For a developer tool where the alternative is a confusing silent success,
    that tradeoff is the right one; the mitigation is rate limiting, not
    ambiguity.
    """
    email = _normalize_email(payload.email)

    existing = await session.execute(select(User.id).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("An account with that email already exists")

    user = User(
        id=new_id(),
        email=email,
        password_hash=hash_password(payload.password),
        timezone=payload.timezone,
    )
    session.add(user)
    await session.flush()

    _logger.info("user_signed_up", user_id=user.id)
    return await _issue_token_pair(session, user, settings.jwt_secret.get_secret_value(), new_id())


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: DbSession, settings: SettingsDep) -> TokenResponse:
    """Exchange credentials for a token pair."""
    email = _normalize_email(payload.email)
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # One message for every failure mode: unknown email, wrong password, and
    # deactivated account are indistinguishable to the caller.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthenticationError("Invalid email or password")
    if not user.is_active:
        raise AuthenticationError("Invalid email or password")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    _logger.info("user_logged_in", user_id=user.id)
    return await _issue_token_pair(session, user, settings.jwt_secret.get_secret_value(), new_id())


@router.post("/refresh-token", response_model=TokenResponse)
async def refresh_token(
    payload: RefreshRequest, session: DbSession, settings: SettingsDep
) -> TokenResponse:
    """Rotate a refresh token.

    Reuse of a consumed token revokes the entire family.
    """
    token_hash = hash_refresh_token(payload.refresh_token)
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    stored = result.scalar_one_or_none()

    if stored is None:
        raise AuthenticationError("Invalid refresh token")

    now = datetime.now(UTC)

    if stored.consumed_at is not None:
        # In its own transaction, deliberately. This handler answers 401, and
        # raising would roll the request's transaction back — taking the
        # revocation with it and leaving the leaked family live. The whole
        # point of detecting reuse is the revocation, so it must outlive the
        # error that reports it.
        await _revoke_family(stored.user_id, stored.family_id)
        _logger.warning(
            "refresh_token_reuse_detected",
            user_id=stored.user_id,
            family_id=stored.family_id,
        )
        raise AuthenticationError("Refresh token has already been used")

    if stored.revoked_at is not None or stored.expires_at <= now:
        raise AuthenticationError("Refresh token is no longer valid")

    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is not active")

    stored.consumed_at = now
    return await _issue_token_pair(
        session, user, settings.jwt_secret.get_secret_value(), stored.family_id
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, session: DbSession) -> None:
    """Revoke the presented token's whole family.

    Succeeds even for an unknown token: logout must never be a way to probe
    which tokens exist, and a client trying to log out should never be stuck.
    """
    token_hash = hash_refresh_token(payload.refresh_token)
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        return

    await set_rls_user(session, stored.user_id)
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == stored.family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    _logger.info("user_logged_out", user_id=stored.user_id)


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser) -> UserResponse:
    """The authenticated account."""
    return UserResponse(
        id=user.id,
        email=user.email,
        timezone=user.timezone,
        plan_id=user.plan_id,
        created_at=user.created_at,
    )
