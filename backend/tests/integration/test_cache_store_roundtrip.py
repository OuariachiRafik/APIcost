"""The exact-hash store/lookup round trip.

This exists because of a bug that was invisible from the outside: `store()`
wrote a bare entry id into Redis while `_lookup_exact` expected a JSON blob, so
every exact lookup failed to parse and returned a miss. The cache still
*worked* — it fell through to embedding plus a vector search — so every
behavioural test passed. It just cost 30 ms more per hit than it should have.

The end-to-end tests could not catch it: an identical prompt embeds to a cosine
similarity of ~1.0, so a vector hit is indistinguishable from an exact hit by
its result. Only asserting on the mechanism catches this class of bug.
"""

from __future__ import annotations

import pytest

from apicost.cache.semantic import (
    exact_cache_key,
    lookup_exact,
    lookup_similar,
    prompt_hash,
    store,
)
from apicost.core.ids import new_id
from apicost.db.redis import get_redis
from apicost.db.session import session_scope
from apicost.vault.kms import LocalKMS

pytestmark = pytest.mark.integration

PROMPT = "model=gpt-4o\nuser: what is the capital of France"
BODY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "gpt-4o",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Paris."}}],
}


@pytest.fixture
def kms() -> LocalKMS:
    return LocalKMS("round-trip-test-master-key")


async def _seed_user(session_user: str) -> None:
    from sqlalchemy import text

    from apicost.db.session import get_admin_engine

    async with get_admin_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES (:id, :email, 'x') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": session_user, "email": f"{session_user}@example.com"},
        )


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_store_then_lookup_exact_round_trips(kms: LocalKMS) -> None:
    """What `store` writes must be what `lookup_exact` can read."""
    user_id, project_id = new_id(), new_id()
    await _seed_user(user_id)

    async with session_scope(user_id=user_id) as session:
        entry_id = await store(
            session,
            get_redis(),
            kms,
            user_id=user_id,
            project_id=project_id,
            normalized_prompt=PROMPT,
            embedding=[0.1] * 384,
            body=BODY,
            model_used="gpt-4o",
            tokens_in=11,
            tokens_out=7,
            ttl_seconds=300,
        )

    assert entry_id is not None

    hit = await lookup_exact(
        get_redis(), kms, user_id=user_id, project_id=project_id, normalized_prompt=PROMPT
    )

    assert hit is not None, (
        "the exact path missed its own write — store and lookup disagree on the format"
    )
    assert hit.exact is True
    assert hit.similarity == 1.0
    assert hit.body == BODY
    assert hit.model_used == "gpt-4o"
    assert hit.tokens_in == 11
    assert hit.tokens_out == 7
    assert hit.entry_id == entry_id


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_the_redis_entry_holds_ciphertext_not_a_pointer(kms: LocalKMS) -> None:
    """The exact path must not need Postgres — that is what makes it fast."""
    user_id, project_id = new_id(), new_id()
    await _seed_user(user_id)

    async with session_scope(user_id=user_id) as session:
        await store(
            session,
            get_redis(),
            kms,
            user_id=user_id,
            project_id=project_id,
            normalized_prompt=PROMPT,
            embedding=[0.1] * 384,
            body=BODY,
            model_used="gpt-4o",
            tokens_in=1,
            tokens_out=1,
            ttl_seconds=300,
        )

    raw = await get_redis().get(exact_cache_key(user_id, project_id, prompt_hash(PROMPT)))
    assert raw is not None

    import json

    entry = json.loads(raw)
    assert {"i", "p", "w", "n", "m", "ti", "to"} <= set(entry), (
        "the Redis entry is not self-sufficient; the exact path would need Postgres"
    )
    # And it is still ciphertext.
    assert "Paris" not in raw


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_a_lookup_with_no_stored_entry_misses(kms: LocalKMS) -> None:
    hit = await lookup_exact(
        get_redis(),
        kms,
        user_id=new_id(),
        project_id=new_id(),
        normalized_prompt="nothing was ever stored for this",
    )
    assert hit is None


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_the_vector_path_finds_what_store_wrote(kms: LocalKMS) -> None:
    """The other half: the Postgres row must be usable too."""
    user_id, project_id = new_id(), new_id()
    await _seed_user(user_id)
    embedding = [0.1] * 384

    async with session_scope(user_id=user_id) as session:
        await store(
            session,
            get_redis(),
            kms,
            user_id=user_id,
            project_id=project_id,
            normalized_prompt=PROMPT,
            embedding=embedding,
            body=BODY,
            model_used="gpt-4o",
            tokens_in=3,
            tokens_out=4,
            ttl_seconds=300,
        )

    async with session_scope(user_id=user_id) as session:
        hit = await lookup_similar(
            session,
            kms,
            user_id=user_id,
            project_id=project_id,
            embedding=embedding,
            threshold=0.95,
        )

    assert hit is not None
    assert hit.exact is False
    assert hit.body == BODY
