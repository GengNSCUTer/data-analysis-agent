from __future__ import annotations

import pytest

from data_analysis_agent.budget import BudgetUsage, RequestBudget
from data_analysis_agent.context_builder import ContextBudgetFilter
from vanna.core.storage import Message


class _SummaryCounter:
    calls = 0

    async def __call__(self, turns, source):
        self.calls += 1
        return "- 用户先讨论业务指标，当前摘要仅用于对话连续性。"


class _FailingSummary:
    async def __call__(self, turns, source):
        raise RuntimeError("provider unavailable")


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
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert filtered[1].content == "当前问题"
    assert "旧问题" not in "".join(message.content or "" for message in filtered)
    assert "历史边界" in filtered[0].content
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
async def test_context_filter_records_content_free_history_provenance() -> None:
    messages = [
        Message(role="user", content="历史问题，不应进入压缩消息" * 8),
        Message(role="assistant", content="历史回答" * 8),
        Message(role="user", content="当前问题"),
        Message(role="assistant", content="当前回答"),
    ]
    usage = BudgetUsage(RequestBudget(max_context_chars=180, max_context_messages=5))

    filtered = await ContextBudgetFilter(180, 5, usage).filter_messages(messages)

    assert usage.context_budget is not None
    assert usage.context_budget["summary_inserted"] is True
    assert usage.context_budget["omitted_turns"] == 1
    assert len(usage.context_budget["summary"]["source_sha256"]) == 64
    assert "历史问题" not in filtered[0].content
    assert "SQL" in filtered[0].content


@pytest.mark.asyncio
async def test_context_filter_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        ContextBudgetFilter(0, 4)
    with pytest.raises(ValueError):
        ContextBudgetFilter(100, 0)


@pytest.mark.asyncio
async def test_context_filter_uses_semantic_summary_and_reuses_by_source_digest() -> None:
    messages = [
        Message(role="user", content="历史问题：分析销售额" * 8),
        Message(role="assistant", content="历史回答：已准备查询" * 8),
        Message(role="user", content="当前问题"),
        Message(role="assistant", content="当前回答"),
    ]
    provider = _SummaryCounter()
    context_filter = ContextBudgetFilter(
        180, 5, max_tokens=10_000, summary_provider=provider
    )

    first = await context_filter.filter_messages(messages)
    second = await context_filter.filter_messages(messages)

    assert provider.calls == 1
    assert "用户先讨论业务指标" in first[0].content
    assert first[0].content == second[0].content
    assert "历史问题" not in first[0].content


@pytest.mark.asyncio
async def test_token_budget_can_trigger_compression_even_when_char_budget_fits() -> None:
    messages = [
        Message(role="user", content="第一轮"),
        Message(role="assistant", content="第一轮回答"),
        Message(role="user", content="第二轮"),
        Message(role="assistant", content="第二轮回答"),
    ]
    usage = BudgetUsage(RequestBudget(max_context_chars=10_000, max_context_messages=10))
    filtered = await ContextBudgetFilter(
        10_000, 10, usage, max_tokens=50
    ).filter_messages(messages)

    assert usage.context_budget is not None
    assert usage.context_budget["summary_inserted"] is True
    assert usage.context_budget["tokenizer_mode"] in {
        "estimated",
        "local_tokenizer_estimate",
        "exact",
    }
    assert filtered[0].role == "system"


@pytest.mark.asyncio
async def test_summary_failure_falls_back_to_provenance_boundary() -> None:
    messages = [
        Message(role="user", content="旧问题" * 20),
        Message(role="assistant", content="旧回答" * 20),
        Message(role="user", content="当前问题"),
    ]
    filtered = await ContextBudgetFilter(
        140, 5, summary_provider=_FailingSummary()
    ).filter_messages(messages)

    assert filtered[0].role == "system"
    assert "服务器历史边界" in filtered[0].content
    assert "旧问题" not in filtered[0].content
