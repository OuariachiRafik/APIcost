"""Password hashing, proxy keys, and JWTs — core/security.py."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from apicost.core.security import (
    PROXY_KEY_PREFIX,
    decode_access_token,
    generate_proxy_key,
    hash_password,
    hash_proxy_key,
    hash_refresh_token,
    issue_access_token,
    issue_refresh_token,
    verify_password,
    verify_proxy_key,
)

SECRET = "unit-test-signing-secret"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def test_password_round_trip() -> None:
    digest = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", digest)
    assert not verify_password("wrong horse battery staple", digest)


def test_password_hash_is_argon2id() -> None:
    assert hash_password("a-long-enough-password").startswith("$argon2id$")


def test_password_hashes_are_salted() -> None:
    """Identical passwords must not produce identical digests."""
    assert hash_password("same-password-here") != hash_password("same-password-here")


def test_plaintext_password_is_absent_from_the_digest() -> None:
    assert "correct horse" not in hash_password("correct horse battery staple")


@pytest.mark.parametrize("garbage", ["", "not-a-hash", "$argon2id$broken"])
def test_verify_returns_false_on_malformed_hash(garbage: str) -> None:
    """A corrupt stored hash is a failed login, never a 500."""
    assert verify_password("anything", garbage) is False


# ---------------------------------------------------------------------------
# Proxy keys
# ---------------------------------------------------------------------------


def test_proxy_key_shape() -> None:
    raw, _, last4 = generate_proxy_key()
    assert raw.startswith(PROXY_KEY_PREFIX)
    body = raw[len(PROXY_KEY_PREFIX) :]
    assert re.fullmatch(r"[0-9A-Za-z]+", body)
    assert len(body) >= 30
    assert raw.endswith(last4)


def test_proxy_keys_are_unique() -> None:
    assert len({generate_proxy_key()[0] for _ in range(2_000)}) == 2_000


def test_proxy_key_hash_is_not_reversible_to_the_key() -> None:
    raw, key_hash, _ = generate_proxy_key()
    assert raw not in key_hash
    assert len(key_hash) == 64


def test_verify_proxy_key() -> None:
    raw, key_hash, _ = generate_proxy_key()
    assert verify_proxy_key(raw, key_hash)
    assert not verify_proxy_key(raw + "x", key_hash)
    assert not verify_proxy_key(generate_proxy_key()[0], key_hash)


def test_proxy_key_hash_is_stable() -> None:
    raw, key_hash, _ = generate_proxy_key()
    assert hash_proxy_key(raw) == key_hash


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------


def test_refresh_tokens_are_unique_and_hashed() -> None:
    raw_a, hash_a = issue_refresh_token()
    raw_b, hash_b = issue_refresh_token()
    assert raw_a != raw_b
    assert hash_a != hash_b
    assert hash_refresh_token(raw_a) == hash_a
    assert raw_a not in hash_a


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------


def test_access_token_round_trip() -> None:
    token = issue_access_token("01JUSER0000000000000000000", SECRET)
    claims = decode_access_token(token, SECRET)
    assert claims is not None
    assert claims["sub"] == "01JUSER0000000000000000000"
    assert claims["typ"] == "access"


def test_access_token_rejects_wrong_secret() -> None:
    token = issue_access_token("01JUSER0000000000000000000", SECRET)
    assert decode_access_token(token, "a-different-secret") is None


def test_expired_access_token_is_rejected() -> None:
    token = issue_access_token("01JUSER0000000000000000000", SECRET, ttl=timedelta(seconds=-1))
    assert decode_access_token(token, SECRET) is None


def test_unsigned_token_is_rejected() -> None:
    """The `alg` header must not be honored — that is the `none` attack."""
    import base64
    import json

    def b64(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'attacker', 'typ': 'access'})}."
    assert decode_access_token(forged, SECRET) is None


def test_garbage_token_is_rejected() -> None:
    assert decode_access_token("not.a.jwt", SECRET) is None
    assert decode_access_token("", SECRET) is None
