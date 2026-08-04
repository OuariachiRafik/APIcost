"""RFC 7807 error rendering — BUILD_SPEC §8, hard rule 3."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from apicost.core.errors import (
    PROBLEM_JSON,
    APICostError,
    BudgetExceededError,
    NotFoundError,
    register_exception_handlers,
    unhandled_exception_handler,
)
from apicost.core.logging import bind_request_id, reset_request_id

LEAKED_KEY = "sk-proj-Leaked00000000000000000000"


def _request(path: str = "/things/1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
        }
    )


def test_problem_document_shape() -> None:
    problem = NotFoundError("no such project").to_problem(instance="/projects/9")

    assert problem["status"] == 404
    assert problem["title"] == "Not Found"
    assert problem["detail"] == "no such project"
    assert problem["instance"] == "/projects/9"
    assert problem["type"] == "about:blank"


def test_problem_detail_is_redacted() -> None:
    """An error message is a response body. Hard rule 3 covers response bodies."""
    problem = APICostError(f"provider rejected {LEAKED_KEY}").to_problem()
    assert LEAKED_KEY not in problem["detail"]


def test_problem_carries_request_id() -> None:
    token = bind_request_id("01JERRORREQUEST0000000000")
    try:
        problem = NotFoundError("gone").to_problem()
    finally:
        reset_request_id(token)
    assert problem["request_id"] == "01JERRORREQUEST0000000000"


def test_extra_fields_are_included() -> None:
    problem = BudgetExceededError("daily limit reached", limit_usd=25.0).to_problem()
    assert problem["status"] == 402
    assert problem["limit_usd"] == 25.0


async def test_unhandled_exception_never_echoes_its_message() -> None:
    """We cannot audit a message we did not write, so we do not forward it."""
    response = await unhandled_exception_handler(
        _request(), RuntimeError(f"boom while using {LEAKED_KEY}")
    )

    body = bytes(response.body).decode()
    assert response.status_code == 500
    assert LEAKED_KEY not in body
    assert "boom" not in body
    assert "An unexpected error occurred." in body


async def test_handlers_render_problem_json_through_the_app() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/missing")
    async def missing() -> None:
        raise NotFoundError("project not found")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/missing")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["detail"] == "project not found"


async def test_http_exception_uses_the_same_shape() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/no-such-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert set(response.json()) >= {"type", "title", "status", "detail"}


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (NotFoundError(), 404),
        (BudgetExceededError(), 402),
        (APICostError(), 500),
    ],
)
def test_status_codes(error: APICostError, expected_status: int) -> None:
    assert error.status_code == expected_status
