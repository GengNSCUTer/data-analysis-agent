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
