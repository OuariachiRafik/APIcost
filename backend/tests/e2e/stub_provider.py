"""A stand-in for a real LLM provider.

Lets the e2e suite drive the whole proxy path — auth, key decryption,
forwarding, streaming, ledger — without network access, an API key, or spend.
It speaks OpenAI's shape because that is our canonical representation.

Deliberately controllable: the ``model`` field selects behaviour, so a test can
ask for a slow response, an error, or a stream with no usage block without
needing a second stub.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

__all__ = ["build_stub_provider", "received_requests"]

received_requests: list[dict[str, Any]] = []

COMPLETION_TEXT = "Hello from the stub provider."


def _completion(model: str, content: str = COMPLETION_TEXT) -> dict[str, Any]:
    return {
        "id": "chatcmpl-stub-1",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }


def build_stub_provider() -> FastAPI:
    app = FastAPI()

    @app.post("/chat/completions")
    async def chat_completions(request: Request) -> Any:
        body = await request.json()
        received_requests.append(
            {"body": body, "authorization": request.headers.get("authorization")}
        )

        model = body.get("model", "gpt-4o")

        if model == "stub-unauthorized":
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Incorrect API key provided",
                        "type": "invalid_request_error",
                    }
                },
            )

        if model == "stub-rate-limited":
            return JSONResponse(
                status_code=429,
                content={"error": {"message": "Rate limit reached", "type": "rate_limit_error"}},
            )

        if model == "stub-slow":
            await asyncio.sleep(0.5)

        if not body.get("stream"):
            return JSONResponse(_completion(model))

        async def stream() -> Any:
            template = {
                "id": "chatcmpl-stub-1",
                "object": "chat.completion.chunk",
                "created": 1_700_000_000,
                "model": model,
            }
            words = COMPLETION_TEXT.split(" ")

            yield (
                b"data: "
                + json.dumps(
                    {
                        **template,
                        "choices": [
                            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                        ],
                    }
                ).encode()
                + b"\n\n"
            )

            for index, word in enumerate(words):
                piece = word if index == 0 else f" {word}"
                yield (
                    b"data: "
                    + json.dumps(
                        {
                            **template,
                            "choices": [
                                {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                            ],
                        }
                    ).encode()
                    + b"\n\n"
                )
                await asyncio.sleep(0.002)

            final: dict[str, Any] = {
                **template,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            # `stub-no-usage` exercises the estimation path (§6.2).
            if model != "stub-no-usage":
                final["usage"] = {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                }
            yield b"data: " + json.dumps(final).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/embeddings")
    async def embeddings(request: Request) -> Any:
        body = await request.json()
        received_requests.append(
            {"body": body, "authorization": request.headers.get("authorization")}
        )
        return JSONResponse(
            {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "model": body.get("model", "text-embedding-3-small"),
                "usage": {"prompt_tokens": 8, "total_tokens": 8},
            }
        )

    return app
