"""What keeps running after the caller cancels an in-flight chat turn."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from vanna.core.llm import LlmRequest
from vanna.core.user import User
from vanna.integrations.openai.llm import OpenAILlmService


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _chunk(content: str | None = None, finish_reason: str | None = None) -> str:
    payload = {
        "id": "chunk",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {} if content is None else {"content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


@pytest.mark.asyncio
async def test_cancelling_the_model_stream_aborts_the_provider_but_sql_thread_finishes() -> None:
    state = {"started": False, "finished": False, "cancelled": False, "polls": 0}
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> StreamingResponse:
        async def body():
            state["started"] = True
            yield _chunk("你")
            try:
                for _ in range(30):
                    state["polls"] += 1
                    if await request.is_disconnected():
                        state["cancelled"] = True
                        return
                    await asyncio.sleep(0.1)
                yield _chunk(finish_reason="stop")
                yield "data: [DONE]\n\n"
                state["finished"] = True
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        return StreamingResponse(body(), media_type="text/event-stream")

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started

        service = OpenAILlmService(
            model="test-model",
            api_key="test-key",
            base_url=f"http://127.0.0.1:{port}/v1",
            connect_timeout_seconds=2,
        )

        async def hold_stream() -> None:
            # Stay blocked on the next provider byte, which is where a browser
            # disconnect cancels the SSE task during model generation.
            async for _chunk_item in service.stream_request(
                LlmRequest(messages=[], user=User(id="cancel-test"))
            ):
                state["client_saw_token"] = _chunk_item.content or state.get("client_saw_token")

        model_task = asyncio.create_task(hold_stream())
        for _ in range(50):
            if state.get("client_saw_token"):
                break
            await asyncio.sleep(0.02)
        assert state.get("client_saw_token") == "你"
        await asyncio.sleep(0.05)
        model_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await model_task
        await asyncio.sleep(1.0)
        assert state["started"] is True
        assert state["cancelled"] is True
        assert state["finished"] is False
    finally:
        server.should_exit = True
        await server_task

    sql_finished = threading.Event()

    def slow_sql() -> None:
        time.sleep(0.3)
        sql_finished.set()

    sql_task = asyncio.create_task(asyncio.to_thread(slow_sql))
    await asyncio.sleep(0.05)
    sql_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sql_task

    assert sql_finished.is_set() is False
    assert sql_finished.wait(1.0) is True
