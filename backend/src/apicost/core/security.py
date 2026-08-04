"""Password hashing, JWTs, and proxy-key generation.

Everything in here is a security control, so the choices are deliberate:

* **Argon2id** for passwords (BUILD_SPEC §2), with the library defaults, which
  track current guidance better than any constant we would hardcode here.
* **SHA-256, not Argon2, for proxy keys.** They are 192 bits of CSPRNG output,
  not user-chosen secrets, so there is no dictionary to attack — and they are
  verified on the proxy hot path where an Argon2 verify would blow the latency
  budget outright.
* **Constant-time comparison** everywhere a secret is checked.
* Raw secrets are returned to the caller exactly once and never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from apicost.core.ids import new_id

__all__ = [
    "ACCESS_TOKEN_TTL",
    "PROXY_KEY_PREFIX",
    "REFRESH_TOKEN_TTL",
    "decode_access_token",
    "generate_proxy_key",
    "hash_password",
    "hash_proxy_key",
    "hash_refresh_token",
    "issue_access_token",
    "issue_refresh_token",
    "needs_rehash",
    "verify_password",
    "verify_proxy_key",
]

# BUILD_SPEC §2: JWT access 15 min, rotating refresh 30 d.
ACCESS_TOKEN_TTL: Final = timedelta(minutes=15)
REFRESH_TOKEN_TTL: Final = timedelta(days=30)

PROXY_KEY_PREFIX: Final = "apc_live_"
_PROXY_KEY_BYTES: Final = 24
"""24 random bytes → 32 base62-ish characters, per §4 P1."""

_BASE62: Final = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_JWT_ALGORITHM: Final = "HS256"

_hasher = PasswordHasher()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password. Returns ``False`` rather than raising on any failure.

    A malformed stored hash is a failed login, not a 500 — and never an error
    message that distinguishes "no such user" from "wrong password".
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses outdated parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# ---------------------------------------------------------------------------
# Proxy keys
# ---------------------------------------------------------------------------


def _base62(raw: bytes) -> str:
    value = int.from_bytes(raw, "big")
    if value == 0:
        return _BASE62[0]
    out: list[str] = []
    while value:
        value, remainder = divmod(value, 62)
        out.append(_BASE62[remainder])
    return "".join(reversed(out))


def generate_proxy_key() -> tuple[str, str, str]:
    """Mint a proxy key.

    Returns ``(raw_key, key_hash, last4)``. The raw key is shown to the user
    once and then discarded; only the hash is persisted (UC-05).
    """
    raw = f"{PROXY_KEY_PREFIX}{_base62(secrets.token_bytes(_PROXY_KEY_BYTES))}"
    return raw, hash_proxy_key(raw), raw[-4:]


def hash_proxy_key(raw_key: str) -> str:
    """SHA-256 of a proxy key, hex encoded."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def verify_proxy_key(raw_key: str, expected_hash: str) -> bool:
    """Constant-time comparison of a presented key against a stored hash."""
    return hmac.compare_digest(hash_proxy_key(raw_key), expected_hash)


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------


def issue_refresh_token() -> tuple[str, str]:
    """Mint an opaque refresh token. Returns ``(raw_token, token_hash)``.

    Opaque rather than a JWT on purpose: a refresh token must be revocable, and
    revocation means a database lookup, which removes the only reason to make
    it self-describing.
    """
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------


def issue_access_token(user_id: str, secret: str, *, ttl: timedelta = ACCESS_TOKEN_TTL) -> str:
    """Mint a short-lived JWT access token."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": new_id(),
        "typ": "access",
    }
    return jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict[str, Any] | None:
    """Decode and validate an access token, or ``None`` if it is not usable.

    The algorithm is pinned to a single value: accepting the header's ``alg``
    is how JWT implementations end up honoring ``none``.
    """
    try:
        claims: dict[str, Any] = jwt.decode(token, secret, algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != "access":
        return None
    return claims
