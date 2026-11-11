from __future__ import annotations

import asyncio

import pytest

from vanna.core.llm import LlmRequest
from vanna.core.user import User
from vanna.integrations.openai import OpenAILlmService


class _FakeCompletions:
    def __init__(self) -> None:
        self.payload = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def create(self, **kwargs):
        self.payload = kwargs
        self.started.set()
        await self.release.wait()
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type("Message", (), {"content": "ok", "tool_calls": []})(),
                            "finish_reason": "stop",
                        },
                    )()
                ],
                "usage": None,
            },
        )()


@pytest.mark.asyncio
@pytest.mark.parametrize("temperature", [0.0, 0.7])
async def test_openai_async_call_yields_and_preserves_sampling_settings(temperature) -> None:
    service = OpenAILlmService.__new__(OpenAILlmService)
    completions = _FakeCompletions()
    service.model = "test-model"
    service.extra_body = {}
    service._client = type(
        "Client", (), {"chat": type("Chat", (), {"completions": completions})()}
    )()
    task = asyncio.create_task(service.send_request(LlmRequest(
        messages=[], user=User(id="openai-async-test"),
        temperature=temperature, max_tokens=800,
    )))
    await asyncio.wait_for(completions.started.wait(), timeout=1)
    assert not task.done()
    completions.release.set()
    response = await task

    assert response.content == "ok"
    assert completions.payload == {
        "model": "test-model", "messages": [], "temperature": temperature,
        "max_tokens": 800, "stream": False,
    }


@pytest.mark.asyncio
async def test_provider_body_is_sent_for_both_request_modes() -> None:
    import httpx

    payloads = []

    def handle(request):
        import json

        payload = json.loads(request.content)
        payloads.append(payload)
        if payload.get("stream"):
            event = {"id": "test", "object": "chat.completion.chunk", "created": 0,
                     "model": "deepseek-flash", "choices": [
                         {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"}}]})

    service = OpenAILlmService(model="deepseek-flash", api_key="test-placeholder",
        base_url="https://example.invalid", extra_body={"thinking": {"type": "disabled"}},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    request = LlmRequest(messages=[], user=User(id="provider-body-test"), max_tokens=50)
    try:
        assert (await service.send_request(request)).content == "ok"
        chunks = [chunk async for chunk in service.stream_request(request)]
        assert any(chunk.content == "ok" for chunk in chunks)
        assert [p["stream"] for p in payloads] == [False, True]
        assert all(p["thinking"] == {"type": "disabled"} for p in payloads)
    finally:
        await service.aclose()
