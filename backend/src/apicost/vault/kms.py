"""Key management — BUILD_SPEC §6.9.

Envelope encryption has two layers. A per-user **data key** encrypts the
provider key; a **master key** held by the KMS wraps that data key. The
database stores only ciphertext and wrapped data keys, so a database dump
without the KMS is inert.

``LocalKMS`` is for development and ``AwsKmsClient`` for production. Their
interfaces are identical, so switching is a config change and nothing else —
which is the point: an interface that only fits the dev implementation would
be discovered to be wrong at the worst possible moment.
"""

from __future__ import annotations

import os
from typing import Any, Final, Protocol, runtime_checkable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apicost.config import Settings, get_settings
from apicost.core.errors import APICostError

__all__ = [
    "DATA_KEY_BYTES",
    "AwsKmsClient",
    "KMSClient",
    "KMSError",
    "LocalKMS",
    "get_kms_client",
]

DATA_KEY_BYTES: Final = 32
"""256-bit data keys, per §4 P1."""

_NONCE_BYTES: Final = 12
_HKDF_INFO: Final = b"apicost-local-kms-master-key-v1"


class KMSError(APICostError):
    """Raised when wrapping or unwrapping fails.

    Deliberately carries no detail about the key material involved.
    """

    status_code = 500
    title = "Key Management Error"


@runtime_checkable
class KMSClient(Protocol):
    """The contract both implementations satisfy."""

    async def generate_data_key(self) -> tuple[bytes, bytes]:
        """Return ``(plaintext_data_key, wrapped_data_key)``."""
        ...

    async def unwrap(self, wrapped: bytes) -> bytes:
        """Return the plaintext data key for a wrapped one."""
        ...


class LocalKMS:
    """Development KMS backed by a master key from configuration.

    The configured master key is stretched through HKDF rather than used
    directly, so a short or low-entropy dev value still yields a well-formed
    256-bit AES key instead of failing at encryption time.
    """

    def __init__(self, master_key: str) -> None:
        if not master_key:
            raise KMSError("KMS master key is not configured")
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=DATA_KEY_BYTES,
            salt=None,
            info=_HKDF_INFO,
        ).derive(master_key.encode())

    async def generate_data_key(self) -> tuple[bytes, bytes]:
        data_key = os.urandom(DATA_KEY_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        wrapped = nonce + AESGCM(self._key).encrypt(nonce, data_key, None)
        return data_key, wrapped

    async def unwrap(self, wrapped: bytes) -> bytes:
        if len(wrapped) <= _NONCE_BYTES:
            raise KMSError("wrapped data key is malformed")
        nonce, ciphertext = wrapped[:_NONCE_BYTES], wrapped[_NONCE_BYTES:]
        try:
            return AESGCM(self._key).decrypt(nonce, ciphertext, None)
        except Exception as exc:
            raise KMSError("could not unwrap data key") from exc


class AwsKmsClient:
    """Production KMS backed by AWS KMS.

    ``boto3`` is imported lazily so the dependency is only required where this
    implementation is actually used; local development and CI never touch it.
    """

    def __init__(self, key_id: str, region_name: str | None = None) -> None:
        self._key_id = key_id
        self._region_name = region_name
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - prod-only path
                raise KMSError("AWS KMS support requires boto3") from exc
            self._client = boto3.client("kms", region_name=self._region_name)
        return self._client

    async def generate_data_key(self) -> tuple[bytes, bytes]:  # pragma: no cover
        import asyncio

        def _call() -> tuple[bytes, bytes]:
            response = self._get_client().generate_data_key(KeyId=self._key_id, KeySpec="AES_256")
            return response["Plaintext"], response["CiphertextBlob"]

        return await asyncio.to_thread(_call)

    async def unwrap(self, wrapped: bytes) -> bytes:  # pragma: no cover
        import asyncio

        def _call() -> bytes:
            response = self._get_client().decrypt(CiphertextBlob=wrapped, KeyId=self._key_id)
            plaintext: bytes = response["Plaintext"]
            return plaintext

        return await asyncio.to_thread(_call)


def get_kms_client(settings: Settings | None = None) -> KMSClient:
    """Build the KMS client for the current environment."""
    cfg = settings or get_settings()
    if cfg.kms_provider == "aws":
        return AwsKmsClient(cfg.kms_key_id, cfg.kms_region or None)
    return LocalKMS(cfg.kms_master_key.get_secret_value())
