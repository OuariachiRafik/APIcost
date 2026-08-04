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

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apicost.core.errors import APICostError
from apicost.vault.kms import KMSClient

__all__ = [
    "EncryptedProviderKey",
    "ProviderKeyError",
    "decrypt_provider_key",
    "encrypt_provider_key",
    "last4",
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
