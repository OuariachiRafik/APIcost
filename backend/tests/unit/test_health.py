"""Health endpoints on both planes — P0 acceptance criterion 2.

The live check against real Postgres and Redis lives in
``tests/integration/test_readiness.py``. Here the dependency probes are
substituted so the matrix of up/down combinations is exhaustive and fast.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import apicost.app as app_module


@pytest.fixture
def _deps_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok() -> bool:
        return True

    monkeypatch.setattr(app_module, "check_postgres", ok)
    monkeypatch.setattr(app_module, "check_redis", ok)


def _patch_deps(monkeypatch: pytest.MonkeyPatch, *, postgres: bool, redis: bool) -> None:
    async def pg() -> bool:
        return postgres

    async def rd() -> bool:
        return redis

    monkeypatch.setattr(app_module, "check_postgres", pg)
    monkeypatch.setattr(app_module, "check_redis", rd)


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


async def test_proxy_healthz(proxy_client: AsyncClient) -> None:
    response = await proxy_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "proxy"}


async def test_api_healthz(api_client: AsyncClient) -> None:
    response = await api_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


async def test_healthz_ignores_dependencies(
    proxy_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness must not fail on a Postgres blip, or we get restarted mid-incident."""
    _patch_deps(monkeypatch, postgres=False, redis=False)
    response = await proxy_client.get("/healthz")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_deps_up")
async def test_proxy_readyz_200(proxy_client: AsyncClient) -> None:
    response = await proxy_client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["service"] == "proxy"
    assert body["checks"] == {"postgres": True, "redis": True}


@pytest.mark.usefixtures("_deps_up")
async def test_api_readyz_200(api_client: AsyncClient) -> None:
    response = await api_client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["service"] == "api"


@pytest.mark.parametrize(
    ("postgres", "redis"),
    [(False, True), (True, False), (False, False)],
)
async def test_readyz_503_when_a_dependency_is_down(
    proxy_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    postgres: bool,
    redis: bool,
) -> None:
    _patch_deps(monkeypatch, postgres=postgres, redis=redis)

    response = await proxy_client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"postgres": postgres, "redis": redis}


# ---------------------------------------------------------------------------
# Request id propagation
# ---------------------------------------------------------------------------


async def test_request_id_headers_are_set(proxy_client: AsyncClient) -> None:
    response = await proxy_client.get("/healthz")
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 26
    assert response.headers["x-apicost-request-id"] == request_id


async def test_request_ids_are_unique_per_request(proxy_client: AsyncClient) -> None:
    first = await proxy_client.get("/healthz")
    second = await proxy_client.get("/healthz")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_client_supplied_request_id_is_not_trusted(
    proxy_client: AsyncClient,
) -> None:
    """The id reaches logs and the ledger; it must be ours, not the caller's."""
    response = await proxy_client.get("/healthz", headers={"X-Request-Id": "injected-value"})
    assert response.headers["x-request-id"] != "injected-value"
