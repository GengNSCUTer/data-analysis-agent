from __future__ import annotations

import asyncio
import threading

import pytest

from vanna.core.llm import LlmRequest
from vanna.core.user import User
from vanna.integrations.openai import OpenAILlmService


class _FakeCompletions:
    def __init__(self) -> None:
        self.thread_id: int | None = None

    def create(self, **kwargs):
        self.thread_id = threading.get_ident()
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
async def test_openai_non_streaming_call_runs_off_event_loop() -> None:
    service = OpenAILlmService.__new__(OpenAILlmService)
    completions = _FakeCompletions()
    service.model = "test-model"
    service._client = type(
        "Client", (), {"chat": type("Chat", (), {"completions": completions})()}
    )()
    event_loop_thread = threading.get_ident()

    response = await service.send_request(
        LlmRequest(messages=[], user=User(id="openai-thread-test"))
    )

    assert response.content == "ok"
    assert completions.thread_id is not None
    assert completions.thread_id != event_loop_thread
