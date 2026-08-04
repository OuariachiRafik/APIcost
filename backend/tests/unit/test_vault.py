"""Envelope encryption — vault/kms.py and vault/provider_keys.py.

The property under test throughout: the stored components must be useless on
their own. Anything recoverable from ciphertext alone is a reportable incident,
not a test failure (CLAUDE.md hard rule 4).
"""

from __future__ import annotations

import pytest

from apicost.vault.kms import DATA_KEY_BYTES, KMSError, LocalKMS
from apicost.vault.provider_keys import (
    ProviderKeyError,
    decrypt_provider_key,
    encrypt_provider_key,
    last4,
    zeroed,
)

PROVIDER_KEY = "sk-proj-RealLookingProviderKey0123456789"


@pytest.fixture
def kms() -> LocalKMS:
    return LocalKMS("test-master-key-material")


# ---------------------------------------------------------------------------
# KMS
# ---------------------------------------------------------------------------


async def test_data_key_round_trip(kms: LocalKMS) -> None:
    plaintext, wrapped = await kms.generate_data_key()
    assert len(plaintext) == DATA_KEY_BYTES
    assert plaintext != wrapped
    assert await kms.unwrap(wrapped) == plaintext


async def test_each_data_key_is_fresh(kms: LocalKMS) -> None:
    first, _ = await kms.generate_data_key()
    second, _ = await kms.generate_data_key()
    assert first != second


async def test_unwrap_rejects_tampered_ciphertext(kms: LocalKMS) -> None:
    _, wrapped = await kms.generate_data_key()
    tampered = bytearray(wrapped)
    tampered[-1] ^= 0xFF
    with pytest.raises(KMSError):
        await kms.unwrap(bytes(tampered))


async def test_unwrap_rejects_truncated_input(kms: LocalKMS) -> None:
    with pytest.raises(KMSError):
        await kms.unwrap(b"short")


async def test_wrong_master_key_cannot_unwrap(kms: LocalKMS) -> None:
    _, wrapped = await kms.generate_data_key()
    with pytest.raises(KMSError):
        await LocalKMS("a-different-master-key").unwrap(wrapped)


def test_missing_master_key_is_rejected_at_construction() -> None:
    with pytest.raises(KMSError):
        LocalKMS("")


# ---------------------------------------------------------------------------
# Provider keys
# ---------------------------------------------------------------------------


async def test_provider_key_round_trip(kms: LocalKMS) -> None:
    stored = await encrypt_provider_key(kms, PROVIDER_KEY)
    assert await decrypt_provider_key(kms, stored) == PROVIDER_KEY


async def test_ciphertext_does_not_contain_the_key(kms: LocalKMS) -> None:
    stored = await encrypt_provider_key(kms, PROVIDER_KEY)
    blob = stored.encrypted_key + stored.wrapped_data_key + stored.nonce
    assert PROVIDER_KEY.encode() not in blob
    assert b"sk-proj" not in blob


async def test_same_key_encrypts_differently_each_time(kms: LocalKMS) -> None:
    """Fresh data key and nonce per call — no deterministic ciphertext."""
    first = await encrypt_provider_key(kms, PROVIDER_KEY)
    second = await encrypt_provider_key(kms, PROVIDER_KEY)
    assert first.encrypted_key != second.encrypted_key
    assert first.nonce != second.nonce
    assert first.wrapped_data_key != second.wrapped_data_key


async def test_tampered_ciphertext_is_rejected(kms: LocalKMS) -> None:
    """AES-GCM is authenticated: a flipped bit fails, it does not decrypt to junk."""
    stored = await encrypt_provider_key(kms, PROVIDER_KEY)
    corrupted = bytearray(stored.encrypted_key)
    corrupted[0] ^= 0xFF
    stored.encrypted_key = bytes(corrupted)

    with pytest.raises(ProviderKeyError):
        await decrypt_provider_key(kms, stored)


async def test_repr_never_renders_ciphertext(kms: LocalKMS) -> None:
    stored = await encrypt_provider_key(kms, PROVIDER_KEY)
    rendered = repr(stored)
    assert PROVIDER_KEY not in rendered
    assert stored.encrypted_key.hex() not in rendered
    assert "bytes>" in rendered


async def test_empty_key_is_rejected(kms: LocalKMS) -> None:
    with pytest.raises(ProviderKeyError):
        await encrypt_provider_key(kms, "")


def test_last4() -> None:
    assert last4(PROVIDER_KEY) == PROVIDER_KEY[-4:]
    assert len(last4(PROVIDER_KEY)) == 4


def test_zeroed_overwrites_the_buffer() -> None:
    buffer = bytearray(b"sensitive-material")
    with zeroed(buffer) as active:
        assert bytes(active) == b"sensitive-material"
    assert bytes(buffer) == bytes(len(b"sensitive-material"))


def test_zeroed_overwrites_even_when_the_body_raises() -> None:
    buffer = bytearray(b"secret")
    with pytest.raises(RuntimeError), zeroed(buffer):
        raise RuntimeError("boom")
    assert bytes(buffer) == bytes(6)
