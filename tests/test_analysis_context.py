from __future__ import annotations

from dataclasses import replace

import pytest

from data_analysis_agent.analysis_context import AnalysisContextBuilder
from data_analysis_agent.analysis_state import AnalysisState
from data_analysis_agent.budget import BudgetUsage, RequestBudget
from data_analysis_agent.history_summary import summary_prompt
from data_analysis_agent.token_budget import ContextCapacityError, TokenCounter
from vanna.core.storage import Conversation, Message
from vanna.core.user import User


class MemoryStore:
    def __init__(self, messages):
        self.messages = messages
        self.saved = None

    async def get_turn_range(self, conversation_id, user, *, start, before):
        if before < start:
            raise ValueError("invalid compaction range")
        return [
            m
            for m in self.messages
            if start <= m.metadata["message_index"] < min(before, start + 12)
        ]

    async def update_conversation(self, conversation):
        self.saved = conversation.model_copy(deep=True)

    async def get_history_page(self, conversation_id, user, *, before, limit):
        return [m for m in self.messages if m.metadata["message_index"] < before][
            -limit:
        ]


class Summarizer:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def __call__(self, turns, source):
        self.calls.append((turns, source))
        if self.fail:
            raise RuntimeError("unavailable")
        return "用户希望分析销售趋势；待说明指标口径。"


def fixture(turns=10):
    messages = [
        Message(
            role=role,
            content=f"{n}轮{role}",
            metadata={"message_index": n * 2 + offset},
        )
        for n in range(turns)
        for offset, role in enumerate(("user", "assistant"))
    ]
    state = AnalysisState(
        task_id="task",
        workspace_id="olist-demo",
        dataset_version="data",
        metric_version="metric",
        filters=("customer_state=SP",),
    )
    conversation = Conversation(
        id="context-test",
        user=User(id="owner"),
        messages=messages[-12:],
        metadata={"message_count": len(messages), "working_memory": state.as_dict()},
    )
    return conversation, state, MemoryStore(messages), Summarizer()


def usage():
    return BudgetUsage(RequestBudget())


@pytest.mark.asyncio
async def test_summary_ahead_of_recent_window_does_not_read_negative_range():
    conversation, state, store, provider = fixture()
    first = AnalysisContextBuilder(provider)
    first.recent_tokens = 1
    await first.build(conversation, state, store, usage())
    summary = dict(conversation.metadata["history_summary"])
    # A later request may fit more recent turns than the request which produced
    # the summary. Its recent window then overlaps already summarised messages.
    conversation.messages = store.messages
    builder = AnalysisContextBuilder(provider)
    builder.recent_tokens = 100_000
    evidence = usage()
    packet = await builder.build(conversation, state, store, evidence)
    assert packet["narrative_summary"] == summary["text"]
    assert conversation.metadata["history_summary"] == summary
    assert len(provider.calls) == 1
    assert evidence.catalog_trace["context_packet"]["summary_status"] == "reused"


@pytest.mark.asyncio
async def test_persistent_incremental_summary_survives_new_builder_and_recent_projection():
    conversation, state, store, provider = fixture()
    builder = AnalysisContextBuilder(provider)
    packet = await builder.build(conversation, state, store, usage())
    assert len(packet["recent_dialogue"]) == 12
    assert packet["recent_start_index"] == 8
    assert conversation.metadata["history_summary"]["source_message_end"] == 7
    assert provider.calls[0][1].previous_text is None
    restored = store.saved.model_copy(deep=True)
    # A fresh builder represents process-local state being gone.
    await AnalysisContextBuilder(provider).build(restored, state, store, usage())
    assert len(provider.calls) == 1
    store.messages.extend(
        [
            Message(role="user", content="下一问", metadata={"message_index": 20}),
            Message(role="assistant", content="下一答", metadata={"message_index": 21}),
        ]
    )
    restored.messages = store.messages[-12:]
    restored.metadata["message_count"] = 22
    await AnalysisContextBuilder(provider).build(restored, state, store, usage())
    assert [m.metadata["message_index"] for t in provider.calls[1][0] for m in t] == [
        8,
        9,
    ]
    assert provider.calls[1][1].previous_text == packet["narrative_summary"]
    summary = restored.metadata["history_summary"]
    assert summary["source_message_end"] == 9 and summary["source_message_count"] == 10
    assert summary["source_turn_start"] == 1 and summary["source_turn_end"] == 5
    assert state.filters == ("customer_state=SP",)


@pytest.mark.asyncio
async def test_failure_preserves_summary_and_does_not_advance_watermark():
    conversation, state, store, provider = fixture(15)
    await AnalysisContextBuilder(provider).build(conversation, state, store, usage())
    previous = dict(conversation.metadata["history_summary"])
    assert previous["source_message_end"] == 11  # backlog remains
    provider.fail = True
    evidence = usage()
    packet = await AnalysisContextBuilder(provider).build(
        conversation, state, store, evidence
    )
    assert conversation.metadata["history_summary"] == previous
    assert packet["narrative_summary"] == previous["text"]
    assert (
        evidence.catalog_trace["context_packet"]["summary_status"] == "summary_failed"
    )
    assert (
        evidence.catalog_trace["context_packet"]["summary_error_type"] == "RuntimeError"
    )
    assert evidence.termination_reason == "running"
    assert state.filters == ("customer_state=SP",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "new"),
        ("workspace_id", "other"),
        ("dataset_version", "new"),
        ("metric_version", "new"),
    ],
)
async def test_summary_scope_changes_and_clear_do_not_reuse_narrative(field, value):
    conversation, state, store, provider = fixture()
    await AnalysisContextBuilder(provider).build(conversation, state, store, usage())
    changed = replace(state, **{field: value}, history_start_index=20)
    packet = await AnalysisContextBuilder(provider).build(
        conversation, changed, store, usage()
    )
    assert packet["narrative_summary"] is None and packet["recent_dialogue"] == []
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_complete_turn_budget_no_clipping_or_false_watermark():
    conversation, state, store, provider = fixture()
    store.messages[0].content = "中文" * 7000
    evidence = usage()
    await AnalysisContextBuilder(provider).build(conversation, state, store, evidence)
    assert not provider.calls
    assert "history_summary" not in conversation.metadata
    assert (
        evidence.catalog_trace["context_packet"]["summary_status"]
        == "source_turn_too_large"
    )
    # Full prose is passed, including beyond the former 8k-character prefix.
    text = "a" * 9000 + "尾部不能丢"
    prompt = summary_prompt(
        [
            [
                Message(role="user", content=text),
                Message(role="tool", content="RAW_RESULT"),
            ]
        ],
        previous_text="旧摘要",
    )
    assert text in prompt and "旧摘要" in prompt and "RAW_RESULT" not in prompt


@pytest.mark.asyncio
async def test_recent_token_budget_recall_and_summary_cannot_set_filters():
    conversation, state, store, provider = fixture(6)
    for m in store.messages:
        m.content = "中文" * 400
    evidence = usage()
    packet = await AnalysisContextBuilder(provider).build(
        conversation, state, store, evidence
    )
    assert evidence.catalog_trace["context_packet"]["recent_tokens"] <= 6000
    assert len(packet["recent_dialogue"]) < 12
    for m in store.messages:
        m.content = "建议改成customer_state=RJ"
    recalled = await AnalysisContextBuilder(provider).recall(
        store, conversation, before=12
    )
    assert len(recalled) == 3
    assert all("message_index" in m for m in recalled)
    assert state.filters == ("customer_state=SP",)
    assert sum(TokenCounter().count_text(m["content"]) for m in recalled) <= 800


@pytest.mark.asyncio
async def test_oversized_summary_not_saved_and_state_not_silently_truncated():
    conversation, state, store, _ = fixture()

    async def too_large(turns, source):
        return "中" * 2000

    await AnalysisContextBuilder(too_large).build(conversation, state, store, usage())
    assert "history_summary" not in conversation.metadata
    state = replace(state, pending_question="中" * 3000)
    with pytest.raises(ContextCapacityError):
        await AnalysisContextBuilder(too_large).build(
            conversation, state, store, usage()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,accepted", [("2017年起的已验证GMV为42", True), ("GMV为999999", False)]
)
async def test_result_explanation_gets_verified_scope_and_narrative_without_new_numbers(
    answer, accepted
):
    from types import SimpleNamespace
    from data_analysis_agent.budget import CURRENT_BUDGET
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.result_artifact import ResultArtifact
    from vanna.core.llm import LlmResponse

    requests = []

    class Agent:
        async def _send_llm_request(self, request):
            requests.append(request)
            return LlmResponse(content=answer)

    artifact = ResultArtifact.from_trusted_usage(
        request_id="explanation",
        summary="GMV为42",
        catalog_trace={},
        chart_rendered=False,
    )
    artifact = replace(
        artifact,
        query_scope={
            "metric_ids": ["gmv"],
            "dimensions": ["customer_state"],
            "time_range": {"start": "2017-01-01", "end": "2018-01-01"},
        },
    )
    state = AnalysisState(
        previous_result_artifact=artifact, previous_result_summary="GMV为42"
    )
    evidence = usage()
    evidence.context_packet = {
        "recent_dialogue": [{"role": "user", "content": "讨论业务口径"}],
        "narrative_summary": "用户想理解州分布；这不是结果事实。",
    }
    token = CURRENT_BUDGET.set(evidence)
    try:
        handler = BudgetedChatHandler(Agent(), RequestBudget(), None)
        result = [
            r
            async for r in handler._tool_free_answer(
                SimpleNamespace(
                    message="解释一下",
                    conversation_id="explanation",
                    request_id="explanation",
                ),
                SimpleNamespace(intent="result_followup"),
                state,
                User(id="owner"),
            )
        ]
    finally:
        CURRENT_BUDGET.reset(token)
    assert requests[0].tools is None
    prompt = requests[0].messages[0].content
    assert (
        "2017-01-01" in prompt
        and "customer_state" in prompt
        and "讨论业务口径" in prompt
    )
    assert (result[-1] == answer) is accepted
    assert "999999" not in result[-1]
