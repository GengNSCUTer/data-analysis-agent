from __future__ import annotations

import pytest

from data_analysis_agent.budget import BudgetUsage, RequestBudget
from data_analysis_agent.context_builder import ContextBudgetFilter
from vanna.core.storage import Message


@pytest.mark.asyncio
async def test_context_filter_keeps_newest_complete_turn() -> None:
    messages = [
        Message(role="user", content="旧问题"),
        Message(role="assistant", content="旧回答"),
        Message(role="user", content="当前问题"),
        Message(role="assistant", content="准备调用工具", tool_calls=[]),
        Message(role="tool", content="当前工具结果"),
        Message(role="assistant", content="当前结论"),
    ]
    usage = BudgetUsage(RequestBudget(max_context_chars=180, max_context_messages=5))

    filtered = await ContextBudgetFilter(180, 5, usage).filter_messages(messages)

    assert [message.role for message in filtered] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert filtered[0].content == "当前问题"
    assert usage.context_truncated is True
    assert usage.context_chars <= 180


@pytest.mark.asyncio
async def test_context_filter_does_not_return_orphaned_old_tool_result() -> None:
    messages = [
        Message(role="user", content="第一轮"),
        Message(role="assistant", content="调用", tool_calls=[]),
        Message(role="tool", content="很长的旧结果" * 100),
        Message(role="assistant", content="第一轮结论"),
        Message(role="user", content="第二轮追问"),
        Message(role="assistant", content="第二轮结论"),
    ]

    filtered = await ContextBudgetFilter(120, 4).filter_messages(messages)

    assert filtered[0].role == "user"
    assert filtered[0].content == "第二轮追问"
    assert all(
        not (message.role == "tool" and index == 0)
        for index, message in enumerate(filtered)
    )


@pytest.mark.asyncio
async def test_context_filter_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        ContextBudgetFilter(0, 4)
    with pytest.raises(ValueError):
        ContextBudgetFilter(100, 0)
