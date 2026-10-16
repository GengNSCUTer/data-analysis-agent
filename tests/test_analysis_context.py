from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from data_analysis_agent.analysis_context import AnalysisContextBuilder
from data_analysis_agent.analysis_state import AnalysisState
from data_analysis_agent.budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
from data_analysis_agent.history_summary import summary_prompt
from data_analysis_agent.token_budget import ContextCapacityError, TokenCounter
from vanna.core.storage import Conversation, Message
from vanna.core.user import User


class MemoryStore:
    def __init__(self, conversation):
        self.conversation = conversation
        self.reads = []

    async def get_conversation(self, conversation_id, user):
        if user.id != self.conversation.user.id:
            return None
        return self.conversation.model_copy(deep=True)

    async def get_turn_range(self, conversation_id, user, *, start, before, limit_turns=20):
        assert before >= start
        self.reads.append((start, before, limit_turns))
        rows = [m for m in self.conversation.messages if start <= m.metadata["message_index"] < before]
        starts = [m.metadata["message_index"] for m in rows if m.role == "user"]
        end = starts[limit_turns] if len(starts) > limit_turns else before
        return [m for m in rows if m.metadata["message_index"] < end]

    async def save_history_summary(self, conversation_id, user, *, scope, expected_end, sources, summary, attempt):
        state = AnalysisState.from_mapping(self.conversation.metadata["working_memory"])
        saved = self.conversation.metadata.get("history_summary")
        end = saved["source_message_end"] if saved else state.history_start_index - 1
        if AnalysisContextBuilder.scope(state) != scope or end != expected_end:
            return False
        actual = {m.metadata["message_id"] for m in self.conversation.messages}
        if any(m.metadata["message_id"] not in actual for m in sources):
            return False
        if summary:
            self.conversation.metadata["history_summary"] = summary
        self.conversation.metadata["summary_attempt"] = attempt
        return True

    async def get_history_page(self, conversation_id, user, *, before, limit):
        return [m for m in self.conversation.messages if m.metadata["message_index"] < before][-limit:]


class Summarizer:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def __call__(self, turns, source):
        self.calls.append((turns, source, CURRENT_BUDGET.get()))
        if self.fail:
            raise RuntimeError("unavailable")
        return "分析目标：理解销售分布。\n讨论与纠正：用户否定按卖家地区统计。\n待解决与下一步：解释差异原因。\n表达偏好：先结论后解释。"


def fixture(turns=20, text="短对话"):
    messages = [Message(role=role, content=f"{n} {text}",
        metadata={"message_index": n * 2 + offset, "message_id": n * 2 + offset + 1})
        for n in range(turns) for offset, role in enumerate(("user", "assistant"))]
    state = AnalysisState(task_id="task", workspace_id="olist-demo", dataset_version="data",
                          metric_version="metric", filters=("customer_state=SP",))
    conversation = Conversation(id="context-test", user=User(id="owner"), messages=messages,
        metadata={"message_count": len(messages), "working_memory": state.as_dict()})
    return conversation, state, MemoryStore(conversation), Summarizer()


def small_budget(**changes):
    return replace(RequestBudget(), history_compaction_trigger_tokens=3000,
                   history_recent_target_tokens=1600, history_summary_target_tokens=200,
                   history_summary_max_tokens=500, history_summary_input_tokens=2400, **changes)


async def drain(builder):
    await asyncio.gather(*list(builder.tasks.values()))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_twenty_short_turns_are_loaded_by_cursor_without_summary():
    conversation, state, store, provider = fixture(45)
    # The initial metadata fetch may include only one DB page.
    initial = conversation.model_copy(deep=True)
    initial.messages = initial.messages[-4:]
    builder = AnalysisContextBuilder(provider)
    evidence = BudgetUsage(RequestBudget())
    packet = await builder.build(initial, state, store, evidence)
    assert len(packet["recent_dialogue"]) == 90
    assert packet["recent_start_index"] == 0
    assert len(store.reads) == 3
    builder.schedule(conversation.id, conversation.user, store)
    await drain(builder)
    assert not provider.calls


@pytest.mark.asyncio
async def test_background_compacts_to_target_and_fresh_builder_reuses_summary():
    conversation, state, store, provider = fixture(25, "中" * 150)
    budget = small_budget()
    builder = AnalysisContextBuilder(provider, budget=budget)
    packet = await builder.build(conversation, state, store, BudgetUsage(budget))
    assert len(packet["recent_dialogue"]) == 50 and not provider.calls
    foreground = BudgetUsage(budget)
    token = CURRENT_BUDGET.set(foreground)
    builder.schedule(conversation.id, conversation.user, store)
    CURRENT_BUDGET.reset(token)
    await drain(builder)
    saved = conversation.metadata["history_summary"]
    assert saved["source_message_end"] > 0
    attempt = conversation.metadata["summary_attempt"]
    assert attempt["raw_tokens_after"] < attempt["raw_tokens_before"]
    assert attempt["raw_tokens_after"] <= budget.history_recent_target_tokens
    assert all(call[2] is not foreground for call in provider.calls)
    new = AnalysisContextBuilder(provider, budget=budget)
    evidence = BudgetUsage(budget)
    packet = await new.build(conversation, state, store, evidence)
    assert packet["narrative_summary"] == saved["text"]
    assert packet["recent_start_index"] == saved["source_message_end"] + 1
    assert evidence.catalog_trace["context_packet"]["recent_tokens"] <= budget.history_recent_target_tokens
    assert state.filters == ("customer_state=SP",)
    calls = len(provider.calls)
    new.schedule(conversation.id, conversation.user, store)
    await drain(new)
    assert len(provider.calls) == calls


@pytest.mark.asyncio
async def test_summary_failure_keeps_all_unsummarised_originals_and_can_resume():
    conversation, state, store, provider = fixture(20, "中" * 100)
    budget = small_budget()
    builder = AnalysisContextBuilder(provider, budget=budget)
    provider.fail = True
    builder.schedule(conversation.id, conversation.user, store)
    await drain(builder)
    assert not conversation.metadata.get("history_summary")
    assert conversation.metadata["summary_attempt"]["error_type"] == "RuntimeError"
    assert conversation.metadata["summary_attempt"]["raw_tokens_after"] == conversation.metadata["summary_attempt"]["raw_tokens_before"]
    packet = await builder.build(conversation, state, store, BudgetUsage(budget))
    assert len(packet["recent_dialogue"]) == 40 and packet["recent_start_index"] == 0
    provider.fail = False
    builder.schedule(conversation.id, conversation.user, store)
    await drain(builder)
    assert conversation.metadata["history_summary"]
    previous = dict(conversation.metadata["history_summary"])
    # Failed next increment must preserve the last valid summary.
    for n in range(20, 40):
        for offset, role in enumerate(("user", "assistant")):
            conversation.messages.append(Message(role=role, content="中" * 150,
                metadata={"message_index": n * 2 + offset, "message_id": n * 2 + offset + 1}))
    conversation.metadata["message_count"] = len(conversation.messages)
    provider.fail = True
    builder.schedule(conversation.id, conversation.user, store)
    await drain(builder)
    assert conversation.metadata["history_summary"] == previous
    packet = await builder.build(conversation, state, store, BudgetUsage(budget))
    assert packet["recent_start_index"] == previous["source_message_end"] + 1


@pytest.mark.asyncio
async def test_oversized_turn_is_split_without_premature_watermark():
    conversation, state, store, _ = fixture(3)
    conversation.messages[0].content = "第一部分。" * 1600 + "必须保留尾部"
    seen, previous_texts = [], []

    async def summarize(turns, source):
        assert "history_summary" not in conversation.metadata
        prompt = summary_prompt(turns, previous_text=source.previous_text, target_tokens=200)
        assert TokenCounter().count_text(prompt) <= 2400
        seen.extend(m.content for turn in turns for m in turn)
        previous_texts.append(source.previous_text)
        return "分析目标：理解业务；待解决与下一步：保留用户纠正。"

    builder = AnalysisContextBuilder(summarize, budget=small_budget())
    builder.schedule(conversation.id, conversation.user, store)
    await drain(builder)
    assert "".join(seen) == conversation.messages[0].content + conversation.messages[1].content
    assert any(previous_texts)
    assert conversation.metadata["history_summary"]["source_message_end"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["clear", "edit", "version"])
async def test_inflight_summary_cannot_restore_invalidated_state(mutation):
    conversation, state, store, _ = fixture(20, "中" * 150)
    started, release = asyncio.Event(), asyncio.Event()

    async def summarize(turns, source):
        started.set()
        await release.wait()
        return "分析目标：用户希望比较销售分布。"

    builder = AnalysisContextBuilder(summarize, budget=small_budget())
    builder.schedule(conversation.id, conversation.user, store)
    await started.wait()
    if mutation == "clear":
        conversation.metadata["working_memory"] = replace(state, task_id="new").as_dict()
    elif mutation == "version":
        conversation.metadata["working_memory"] = replace(state, metric_version="new").as_dict()
    else:
        conversation.messages[0] = conversation.messages[0].model_copy(update={"metadata": {"message_index": 0, "message_id": 999}})
    release.set()
    await drain(builder)
    assert not conversation.metadata.get("history_summary")


@pytest.mark.asyncio
async def test_only_two_workers_and_close_cancels_without_saving_partial_summary():
    started, release = asyncio.Event(), asyncio.Event()
    active, maximum, calls = 0, 0, 0

    async def summarize(turns, source):
        nonlocal active, maximum, calls
        active += 1
        calls += 1
        maximum = max(maximum, active)
        if active == 2:
            started.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return "分析目标：比较分布。"

    builder = AnalysisContextBuilder(summarize, budget=small_budget())
    stores = []
    for n in range(3):
        conversation, _, store, _ = fixture(20, "中" * 150)
        conversation.id = f"context-{n}"
        stores.append(store)
        builder.schedule(conversation.id, conversation.user, store)
        builder.schedule(conversation.id, conversation.user, store)
    await asyncio.wait_for(started.wait(), 2)
    assert maximum == 2 and calls == 2 and len(builder.tasks) == 3
    await builder.close()
    assert active == 0 and not builder.tasks
    assert all(not s.conversation.metadata.get("history_summary") for s in stores)


@pytest.mark.asyncio
async def test_hard_ceiling_runs_one_sync_relief_and_never_silently_drops_history():
    conversation, state, store, provider = fixture(30, "中" * 150)
    budget = small_budget(max_history_tokens=6000)
    builder = AnalysisContextBuilder(provider, budget=budget)
    provider.fail = True
    with pytest.raises(ContextCapacityError):
        await builder.build(conversation, state, store, BudgetUsage(budget))
    assert len(provider.calls) == 1 and not conversation.metadata.get("history_summary")
    await builder.close()


@pytest.mark.asyncio
async def test_soft_state_target_does_not_reject_valid_large_state():
    conversation, state, store, provider = fixture()
    state = replace(state, pending_question="中" * 5000)
    evidence = BudgetUsage(RequestBudget())
    await AnalysisContextBuilder(provider).build(conversation, state, store, evidence)
    assert evidence.catalog_trace["context_packet"]["state_above_target"]


@pytest.mark.asyncio
async def test_system_overhead_can_force_compaction_below_normal_recent_target():
    conversation, state, store, provider = fixture(20, "中" * 100)
    builder = AnalysisContextBuilder(provider)
    evidence = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(evidence)
    try:
        packet = await builder.build(conversation, state, store, evidence)
        assert evidence.catalog_trace["context_packet"]["recent_tokens"] < 16000
        await evidence.context_compactor()
        assert evidence.context_packet["recent_start_index"] > packet["recent_start_index"]
        assert evidence.context_packet["current_turn_start_index"] == packet["current_turn_start_index"]
    finally:
        CURRENT_BUDGET.reset(token)
    assert provider.calls and conversation.metadata["history_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("task_id", "other"), ("workspace_id", "other"),
    ("dataset_version", "new"), ("metric_version", "new")])
async def test_summary_scope_does_not_leak_to_new_task(field, value):
    conversation, state, store, provider = fixture(20, "中" * 150)
    builder = AnalysisContextBuilder(provider, budget=small_budget())
    builder.schedule(conversation.id, conversation.user, store)
    await drain(builder)
    changed = replace(state, **{field: value}, history_start_index=40)
    packet = await builder.build(conversation, changed, store, BudgetUsage(builder.budget))
    assert packet["narrative_summary"] is None and not packet["recent_dialogue"]


def test_summary_prompt_keeps_corrections_and_excludes_tool_payloads():
    text = "不要按卖家地区。改为客户地区。先结论后解释，原因还没讨论。"
    prompt = summary_prompt([[Message(role="user", content=text),
        Message(role="tool", content="RAW_SQL_RESULT")]], previous_text="先前目标", target_tokens=2000)
    assert text in prompt and "先前目标" in prompt and "RAW_SQL_RESULT" not in prompt
    assert "讨论与纠正" in prompt and "表达偏好" in prompt and "2000" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,accepted", [("2017年起的已验证GMV为42", True), ("GMV为999999", False)])
async def test_result_explanation_gets_verified_scope_and_narrative_without_new_numbers(answer, accepted):
    from types import SimpleNamespace
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.result_artifact import ResultArtifact
    from vanna.core.llm import LlmResponse

    requests = []
    class Agent:
        async def _send_llm_request(self, request):
            requests.append(request)
            return LlmResponse(content=answer)

    artifact = replace(ResultArtifact.from_trusted_usage(request_id="explanation", summary="GMV为42",
        catalog_trace={}, chart_rendered=False), query_scope={"metric_ids": ["gmv"],
        "dimensions": ["customer_state"], "time_range": {"start": "2017-01-01", "end": "2018-01-01"}})
    state = AnalysisState(previous_result_artifact=artifact, previous_result_summary="GMV为42")
    evidence = BudgetUsage(RequestBudget())
    evidence.context_packet = {"recent_dialogue": [{"role": "user", "content": "讨论业务口径"}],
                               "narrative_summary": "用户想理解州分布；这不是结果事实。"}
    token = CURRENT_BUDGET.set(evidence)
    try:
        handler = BudgetedChatHandler(Agent(), RequestBudget(), None)
        result = [r async for r in handler._tool_free_answer(SimpleNamespace(message="解释一下",
            conversation_id="explanation", request_id="explanation"),
            SimpleNamespace(intent="result_followup"), state, User(id="owner"))]
    finally:
        CURRENT_BUDGET.reset(token)
    assert requests[0].tools is None
    prompt = requests[0].messages[0].content
    assert "2017-01-01" in prompt and "customer_state" in prompt and "讨论业务口径" in prompt
    assert (result[-1] == answer) is accepted
    assert "999999" not in result[-1]
