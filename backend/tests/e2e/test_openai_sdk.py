"""P2 acceptance criterion 1, driven by the real SDK.

    "an unmodified `openai` Python SDK client pointed at the proxy works for
     both streaming and non-streaming calls"

So these tests import the actual ``openai`` package and change exactly one
thing — ``base_url``. If any of this needed an SDK patch, a custom transport,
or a shim, the product's core promise would be false.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from openai import APIStatusError, AsyncOpenAI

from tests.e2e.conftest import LiveServer, provision_account
from tests.e2e.stub_provider import COMPLETION_TEXT, received_requests

pytestmark = pytest.mark.integration


def sdk_client(proxy: LiveServer, proxy_key: str) -> AsyncOpenAI:
    """An unmodified SDK client. One config value changes: base_url."""
    return AsyncOpenAI(base_url=f"{proxy.url}/v1", api_key=proxy_key, max_retries=0)


@pytest.mark.usefixtures("clean_all")
async def test_non_streaming_completion(live_proxy: LiveServer, api_base: AsyncClient) -> None:
    proxy_key = await provision_account(api_base, "sdk-unary@example.com")
    client = sdk_client(live_proxy, proxy_key)

    response = await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
    )

    assert response.choices[0].message.content == COMPLETION_TEXT
    assert response.usage is not None
    assert response.usage.prompt_tokens == 12
    assert response.usage.completion_tokens == 7
    # The SDK parsed it, which is the real assertion — a body we had altered
    # would have raised during validation.
    assert response.model == "gpt-4o"


@pytest.mark.usefixtures("clean_all")
async def test_streaming_completion(live_proxy: LiveServer, api_base: AsyncClient) -> None:
    proxy_key = await provision_account(api_base, "sdk-stream@example.com")
    client = sdk_client(live_proxy, proxy_key)

    stream = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    pieces: list[str] = []
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            pieces.append(chunk.choices[0].delta.content)

    assert "".join(pieces) == COMPLETION_TEXT
    assert len(pieces) > 1, "arrived as a single chunk — the stream was buffered"


@pytest.mark.usefixtures("clean_all")
async def test_the_users_own_provider_key_is_what_reaches_the_provider(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """We forward with their decrypted key, never their proxy key."""
    proxy_key = await provision_account(api_base, "sdk-key@example.com")
    client = sdk_client(live_proxy, proxy_key)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
    )

    assert received_requests, "the stub provider was never called"
    forwarded = received_requests[-1]["authorization"]
    assert forwarded == "Bearer sk-proj-StubProviderKey0123456789"
    assert proxy_key not in forwarded


@pytest.mark.usefixtures("clean_all")
async def test_apicost_metadata_rides_in_headers_not_the_body(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """BUILD_SPEC §0.5. A stray body field is a breaking change for the caller."""
    proxy_key = await provision_account(api_base, "sdk-headers@example.com")

    async with AsyncClient() as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {proxy_key}"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.headers["x-apicost-cache"] == "miss"
    assert response.headers["x-apicost-model-used"] == "gpt-4o"
    assert response.headers["x-apicost-request-id"]

    body = response.json()
    assert set(body) == {"id", "object", "created", "model", "choices", "usage"}
    assert not any(key.lower().startswith("apicost") for key in body)


@pytest.mark.usefixtures("clean_all")
async def test_provider_errors_reach_the_caller_verbatim(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """By design (CODEBASE_GUIDE §12): their error handling keeps working."""
    proxy_key = await provision_account(api_base, "sdk-error@example.com")
    client = sdk_client(live_proxy, proxy_key)

    with pytest.raises(APIStatusError) as excinfo:
        await client.chat.completions.create(
            model="stub-unauthorized", messages=[{"role": "user", "content": "hi"}]
        )

    assert excinfo.value.status_code == 401
    assert "Incorrect API key" in str(excinfo.value)


@pytest.mark.usefixtures("clean_all")
async def test_rate_limit_is_passed_through(live_proxy: LiveServer, api_base: AsyncClient) -> None:
    proxy_key = await provision_account(api_base, "sdk-429@example.com")
    client = sdk_client(live_proxy, proxy_key)

    with pytest.raises(APIStatusError) as excinfo:
        await client.chat.completions.create(
            model="stub-rate-limited", messages=[{"role": "user", "content": "hi"}]
        )

    assert excinfo.value.status_code == 429


@pytest.mark.usefixtures("clean_all")
async def test_a_revoked_key_is_refused(live_proxy: LiveServer, api_base: AsyncClient) -> None:
    proxy_key = await provision_account(api_base, "sdk-revoked@example.com")
    client = sdk_client(live_proxy, proxy_key)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )

    signup = await api_base.post(
        "/auth/login",
        json={"email": "sdk-revoked@example.com", "password": "a-very-long-password"},
    )
    auth = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    projects = await api_base.get("/projects", headers=auth)
    project_id = projects.json()[0]["id"]
    keys = await api_base.get(f"/projects/{project_id}/proxy-keys", headers=auth)
    await api_base.delete(f"/proxy-keys/{keys.json()[0]['id']}", headers=auth)

    with pytest.raises(APIStatusError) as excinfo:
        await client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    assert excinfo.value.status_code == 401


@pytest.mark.usefixtures("clean_all")
async def test_embeddings_are_passed_through(live_proxy: LiveServer, api_base: AsyncClient) -> None:
    proxy_key = await provision_account(api_base, "sdk-embed@example.com")
    client = sdk_client(live_proxy, proxy_key)

    response = await client.embeddings.create(model="text-embedding-3-small", input="hello")

    assert len(response.data[0].embedding) == 3


@pytest.mark.usefixtures("clean_all")
async def test_unauthenticated_request_is_refused(live_proxy: LiveServer) -> None:
    async with AsyncClient() as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            json={"model": "gpt-4o", "messages": []},
        )

    assert response.status_code == 401
    # OpenAI's error envelope, not RFC 7807 — the caller is an SDK.
    assert "error" in response.json()
    assert "message" in response.json()["error"]


@pytest.mark.usefixtures("clean_all")
async def test_a_provider_key_pasted_by_mistake_gets_a_useful_message(
    live_proxy: LiveServer,
) -> None:
    async with AsyncClient() as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": "Bearer sk-proj-SomeoneUsedTheWrongKey123"},
            json={"model": "gpt-4o", "messages": []},
        )

    assert response.status_code == 401
    message = response.json()["error"]["message"]
    assert "apc_live_" in message
    # ...and the key they pasted is not echoed back.
    assert "SomeoneUsedTheWrongKey123" not in response.text
