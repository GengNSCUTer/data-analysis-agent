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
async def test_context_filter_reuses_persisted_packet_across_instances() -> None:
    messages = [
        Message(role="user", content="历史问题：分析销售额" * 8),
        Message(role="assistant", content="历史回答：已准备查询" * 8),
        Message(role="user", content="当前问题"),
        Message(role="assistant", content="当前回答"),
    ]
    from data_analysis_agent.history_summary import HistorySummary
    usage = BudgetUsage(RequestBudget())
    summary = HistorySummary.from_turns([messages[:2]], source_turn_start=1, reason="message_budget")
    usage.record_history_summary(summary.as_evidence(), "用户先讨论业务指标")
    for index, message in enumerate(messages):
        message.metadata["message_index"] = index
    usage.context_packet = {"narrative_summary": "用户先讨论业务指标", "recent_start_index": 2, "current_turn_start_index": 4}
    first = await ContextBudgetFilter(180, 5, usage).filter_messages(messages)
    second = await ContextBudgetFilter(180, 5, usage).filter_messages(messages)
    assert "用户先讨论业务指标" in first[0].content
    assert first[0].content == second[0].content
    assert "历史问题" not in first[0].content


@pytest.mark.asyncio
async def test_token_budget_reports_tokenizer_mode() -> None:
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
async def test_missing_persistent_summary_uses_provenance_boundary() -> None:
    messages = [
        Message(role="user", content="旧问题" * 20),
        Message(role="assistant", content="旧回答" * 20),
        Message(role="user", content="当前问题"),
    ]
    filtered = await ContextBudgetFilter(
        140, 5
    ).filter_messages(messages)

    assert filtered[0].role == "system"
    assert "服务器历史边界" in filtered[0].content
    assert "旧问题" not in filtered[0].content


@pytest.mark.asyncio
async def test_online_projection_drops_old_sql_tools_but_preserves_current_tool_pair():
    from vanna.core.tool import ToolCall
    messages = [
        Message(role="user", content="上次问题"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="old", name="run_sql", arguments={"sql": "OLD SQL"})]),
        Message(role="tool", content="OLD RAW RESULT", tool_call_id="old"),
        Message(role="assistant", content="上次回答"),
        Message(role="user", content="本次问题"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="new", name="run_sql", arguments={"sql": "CURRENT SQL"})]),
        Message(role="tool", content="CURRENT SUMMARY", tool_call_id="new"),
    ]
    for i, message in enumerate(messages):
        message.metadata["message_index"] = i
    usage = BudgetUsage(RequestBudget())
    usage.context_packet = {"recent_start_index": 0, "current_turn_start_index": 4, "narrative_summary": None}
    result = await ContextBudgetFilter(10000, 20, usage).filter_messages(messages)
    assert [m.role for m in result] == ["user", "assistant", "user", "assistant", "tool"]
    assert result[-2].tool_calls[0].id == "new" and result[-1].tool_call_id == "new"
    assert "OLD" not in str(result)
