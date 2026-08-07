"""Provider-key storage: encrypt on the way in, decrypt in memory on the way out.

The rules this module exists to enforce (CLAUDE.md hard rule 4,
CODEBASE_GUIDE §7.1):

* A provider key is encrypted **before the request handler returns**. It is
  never written to a column, a log, or a response.
* Plaintext exists only in process memory, only for the duration of one
  forward, and is overwritten afterwards.
* The only fragment ever shown back to the user is the last four characters.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apicost.core.errors import APICostError
from apicost.core.logging import get_logger
from apicost.vault.kms import KMSClient

if TYPE_CHECKING:
    from redis.asyncio import Redis

_logger = get_logger(__name__)

__all__ = [
    "PROVIDER_KEY_CACHE_TTL_SECONDS",
    "EncryptedProviderKey",
    "ProviderKeyError",
    "cache_provider_key",
    "decrypt_provider_key",
    "encrypt_provider_key",
    "last4",
    "load_cached_provider_key",
    "provider_key_cache_key",
    "purge_provider_key_cache",
    "zeroed",
]

_NONCE_BYTES: Final = 12


class ProviderKeyError(APICostError):
    """Raised when a provider key cannot be encrypted or decrypted."""

    status_code = 500
    title = "Provider Key Error"


class EncryptedProviderKey:
    """The three ciphertext components persisted for one provider key."""

    __slots__ = ("encrypted_key", "nonce", "wrapped_data_key")

    def __init__(self, encrypted_key: bytes, wrapped_data_key: bytes, nonce: bytes) -> None:
        self.encrypted_key = encrypted_key
        self.wrapped_data_key = wrapped_data_key
        self.nonce = nonce

    def __repr__(self) -> str:
        """Never render the ciphertext — reprs end up in logs and tracebacks."""
        return (
            f"EncryptedProviderKey(encrypted_key=<{len(self.encrypted_key)} bytes>, "
            f"wrapped_data_key=<{len(self.wrapped_data_key)} bytes>)"
        )


def last4(raw_key: str) -> str:
    """The last four characters, the only fragment we ever display (UC-02)."""
    return raw_key[-4:]


@contextmanager
def zeroed(buffer: bytearray) -> Iterator[bytearray]:
    """Yield a mutable buffer and overwrite it on exit.

    This is best effort, not a guarantee. CPython may have copied the bytes
    elsewhere (an intermediate ``bytes`` object, a moved allocation), and
    nothing at this level can reach those. It meaningfully shortens the window
    in which a heap dump yields a usable key; it does not eliminate it.
    """
    try:
        yield buffer
    finally:
        for index in range(len(buffer)):
            buffer[index] = 0


async def encrypt_provider_key(kms: KMSClient, raw_key: str) -> EncryptedProviderKey:
    """Encrypt a provider key under a fresh per-key data key.

    A new data key per provider key means compromising one does not expose the
    others, and rotation never has to re-encrypt anything but its own row.
    """
    if not raw_key:
        raise ProviderKeyError("provider key is empty")

    plaintext_key, wrapped_data_key = await kms.generate_data_key()
    data_key = bytearray(plaintext_key)
    try:
        nonce = os.urandom(_NONCE_BYTES)
        encrypted = AESGCM(bytes(data_key)).encrypt(nonce, raw_key.encode(), None)
    except Exception as exc:
        raise ProviderKeyError("could not encrypt provider key") from exc
    finally:
        for index in range(len(data_key)):
            data_key[index] = 0

    return EncryptedProviderKey(encrypted, wrapped_data_key, nonce)


async def decrypt_provider_key(kms: KMSClient, stored: EncryptedProviderKey) -> str:
    """Decrypt a provider key into memory.

    Called from ``proxy/pipeline.py`` immediately before forwarding, and
    nowhere else. The returned string must not be logged, stored, or included
    in any response.
    """
    data_key = bytearray(await kms.unwrap(stored.wrapped_data_key))
    try:
        plaintext = AESGCM(bytes(data_key)).decrypt(stored.nonce, stored.encrypted_key, None)
    except Exception as exc:
        raise ProviderKeyError("could not decrypt provider key") from exc
    finally:
        for index in range(len(data_key)):
            data_key[index] = 0

    return plaintext.decode()


# ---------------------------------------------------------------------------
# Hot-path cache
# ---------------------------------------------------------------------------
#
# The proxy needs this blob on every request, and fetching it from Postgres
# each time put ~15 ms on the critical path — the data plane is not supposed to
# touch Postgres at all (CODEBASE_GUIDE §2).
#
# What goes into Redis is **ciphertext plus a KMS-wrapped data key**, never
# plaintext and never the data key itself. A Redis compromise yields nothing
# without the KMS master key, which is the same position a stolen Postgres dump
# leaves an attacker in — the threat model envelope encryption already assumes.
#
# The staleness this introduces is bounded two ways: a 60 s TTL, and an
# explicit purge in the same operation as any delete or rotation. That is the
# same contract proxy-key revocation already meets (UC-07), and for the same
# reason: a credential the user removed must stop working promptly.

PROVIDER_KEY_CACHE_PREFIX: Final = "apicost:pk:"
PROVIDER_KEY_CACHE_TTL_SECONDS: Final = 60


def provider_key_cache_key(user_id: str, provider: str) -> str:
    return f"{PROVIDER_KEY_CACHE_PREFIX}{user_id}:{provider}"


async def cache_provider_key(
    redis: Redis, user_id: str, provider: str, stored: EncryptedProviderKey
) -> None:
    """Cache the encrypted blob. Failures are swallowed — this is an optimization."""
    payload = json.dumps(
        {
            "k": base64.b64encode(stored.encrypted_key).decode(),
            "w": base64.b64encode(stored.wrapped_data_key).decode(),
            "n": base64.b64encode(stored.nonce).decode(),
        },
        separators=(",", ":"),
    )
    try:
        await redis.set(
            provider_key_cache_key(user_id, provider),
            payload,
            ex=PROVIDER_KEY_CACHE_TTL_SECONDS,
        )
    except Exception:
        _logger.debug("provider_key_cache_write_failed", subsystem="vault")


async def load_cached_provider_key(
    redis: Redis, user_id: str, provider: str
) -> EncryptedProviderKey | None:
    """Read the cached blob, or ``None`` on a miss or any failure."""
    try:
        raw = await redis.get(provider_key_cache_key(user_id, provider))
    except Exception:
        _logger.warning("provider_key_cache_read_failed", subsystem="vault")
        return None

    if not raw:
        return None

    try:
        data = json.loads(raw)
        return EncryptedProviderKey(
            encrypted_key=base64.b64decode(data["k"]),
            wrapped_data_key=base64.b64decode(data["w"]),
            nonce=base64.b64decode(data["n"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, binascii.Error):
        # An entry written by an older shape. Treat as a miss rather than
        # serving something half-understood.
        return None


async def purge_provider_key_cache(redis: Redis, user_id: str, provider: str) -> None:
    """Drop a cached key.

    Must run in the same operation as the database delete or rotation. Without
    it, a key the user removed keeps being usable for up to the TTL — the exact
    failure UC-07 exists to prevent, one layer down.
    """
    try:
        await redis.delete(provider_key_cache_key(user_id, provider))
    except Exception:
        _logger.warning("provider_key_cache_purge_failed", subsystem="vault")
