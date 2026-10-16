from __future__ import annotations

import os
import asyncio
import uuid

import pytest

from data_analysis_agent.conversation_store import (
    ConversationStateConflict,
    InvalidConversationId,
    PostgresConversationStore,
)
from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from vanna.core.storage import Conversation, Message
from vanna.core.tool import ToolCall
from vanna.core.user import User


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def require_project_database() -> None:
    if os.getenv("RUN_PROJECT_DB") != "1":
        pytest.skip(
            "set RUN_PROJECT_DB=1 to run against the project-local PostgreSQL instance"
        )


@pytest.fixture
def store() -> PostgresConversationStore:
    return PostgresConversationStore(PostgresConnectionSettings.from_environment())


@pytest.mark.asyncio
async def test_store_round_trip_preserves_tool_message_and_scopes_owner(
    store: PostgresConversationStore,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    owner = User(id=f"store-owner-{suffix}", group_memberships=["analyst"])
    other = User(id=f"store-other-{suffix}", group_memberships=["analyst"])
    conversation_id = f"store-conversation-{suffix}"
    conversation = Conversation(
        id=conversation_id,
        user=owner,
        metadata={
            "working_memory": {
                "metric_ids": ["gmv"],
                "time_range": {"start": "2017-01-01", "end": "2017-12-31"},
                "pending_missing": [],
            }
        },
        messages=[
            Message(role="user", content="按州统计订单"),
            Message(
                role="assistant",
                content="查询中",
                tool_calls=[
                    ToolCall(id="tc-1", name="run_sql", arguments={"sql": "SELECT 1"})
                ],
            ),
            Message(
                role="tool", content="结果不应出现在列表 DTO 中", tool_call_id="tc-1"
            ),
            Message(role="assistant", content="完成"),
        ],
    )

    await store.update_conversation(conversation)
    loaded = await store.get_conversation(conversation_id, owner)
    assert loaded is not None
    assert [message.role for message in loaded.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert loaded.messages[1].tool_calls[0].name == "run_sql"
    assert loaded.metadata["title"] == "按州统计订单"
    assert loaded.metadata["message_count"] == 4
    assert loaded.metadata["working_memory"]["metric_ids"] == ["gmv"]
    assert await store.get_conversation(conversation_id, other) is None
    assert len(await store.list_conversations(owner, limit=500)) == 1
    assert await store.delete_conversation(conversation_id, other) is False
    assert await store.delete_conversation(conversation_id, owner) is True


@pytest.mark.asyncio
async def test_store_rejects_invalid_pagination(
    store: PostgresConversationStore,
) -> None:
    user = User(id="pagination-test", group_memberships=["analyst"])
    with pytest.raises(ValueError):
        await store.list_conversations(user, limit=0)
    with pytest.raises(ValueError):
        await store.list_conversations(user, offset=-1)


@pytest.mark.asyncio
async def test_store_rejects_malformed_conversation_ids(
    store: PostgresConversationStore,
) -> None:
    user = User(id="malformed-id-test", group_memberships=["analyst"])
    for conversation_id in ("../secret", "", "a" * 129, "-starts-with-dash"):
        with pytest.raises(InvalidConversationId):
            await store.get_conversation(conversation_id, user)


@pytest.mark.asyncio
async def test_store_hides_empty_starter_conversations(
    store: PostgresConversationStore,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    user = User(id=f"starter-owner-{suffix}", group_memberships=["analyst"])
    empty_id = f"starter-empty-{suffix}"
    populated_id = f"starter-populated-{suffix}"
    try:
        await store.update_conversation(Conversation(id=empty_id, user=user))
        await store.update_conversation(
            Conversation(
                id=populated_id,
                user=user,
                messages=[Message(role="user", content="查看订单")],
            )
        )

        conversations = await store.list_conversations(user)
        ids = {conversation.id for conversation in conversations}
        assert populated_id in ids
        assert empty_id not in ids
    finally:
        await store.delete_conversation(empty_id, user)
        await store.delete_conversation(populated_id, user)


@pytest.mark.asyncio
async def test_store_persists_workspace_and_rejects_stale_state(
    store: PostgresConversationStore,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    user = User(id=f"workspace-owner-{suffix}", group_memberships=["analyst"])
    conversation_id = f"workspace-conversation-{suffix}"
    try:
        conversation = Conversation(
            id=conversation_id,
            user=user,
            metadata={"workspace_id": "olist-demo", "state_version": 0},
            messages=[Message(role="user", content="查看订单")],
        )
        await store.update_conversation(conversation)
        assert conversation.metadata["workspace_id"] == "olist-demo"
        first_version = conversation.metadata["state_version"]
        assert first_version == 1

        loaded = await store.get_conversation(conversation_id, user, "olist-demo")
        assert loaded is not None
        assert loaded.metadata["state_version"] == first_version
        assert await store.get_conversation(conversation_id, user, "thelook-analytics") is None

        stale = loaded.model_copy(deep=True)
        await store.update_conversation(loaded)
        assert loaded.metadata["state_version"] == first_version + 1
        with pytest.raises(ConversationStateConflict, match="stale"):
            await store.update_conversation(stale)
    finally:
        await store.delete_conversation(conversation_id, user)


@pytest.mark.asyncio
async def test_truncate_removes_last_user_turn_and_clears_memory(store) -> None:
    suffix = uuid.uuid4().hex[:12]
    user = User(id=f"truncate-owner-{suffix}", group_memberships=["analyst"])
    conversation_id = f"truncate-conversation-{suffix}"
    try:
        conversation = Conversation(
            id=conversation_id,
            user=user,
            metadata={
                "workspace_id": "olist-demo",
                "state_version": 0,
                "working_memory": {"metric_ids": ["gmv"], "previous_result_summary": "旧结果"},
            },
            messages=[
                Message(role="user", content="第一问"),
                Message(role="assistant", content="第一答"),
                Message(role="user", content="第二问"),
                Message(role="assistant", content="第二答"),
            ],
        )
        await store.update_conversation(conversation)
        removed = await store.truncate_from_last_user_message(conversation_id, user)
        assert removed == 2
        loaded = await store.get_conversation(conversation_id, user, "olist-demo")
        assert loaded is not None
        assert [message.content for message in loaded.messages] == ["第一问", "第一答"]
        assert loaded.metadata["working_memory"] == {}
    finally:
        await store.delete_conversation(conversation_id, user)


@pytest.mark.asyncio
async def test_recent_window_append_and_cursor_preserve_full_history(store):
    user = User(id=f"window-{uuid.uuid4().hex}", group_memberships=["analyst"])
    other = User(id="not-window-owner", group_memberships=["analyst"])
    conversation = Conversation(id=f"window-{uuid.uuid4().hex}", user=user)
    try:
        for n in range(10):
            await store.append_turn_messages(conversation, [Message(role="user", content=f"问{n}"), Message(role="assistant", content=f"答{n}")])
        recent = await store.get_recent_turns(conversation.id, user, limit=2)
        assert [m.content for m in recent.messages] == ["问8", "答8", "问9", "答9"]
        assert recent.metadata["message_count"] == 20
        ids = [m.metadata["message_id"] for m in recent.messages]
        assert len(set(ids)) == 4
        assert recent.messages[0].metadata["turn_id"] != recent.messages[2].metadata["turn_id"]
        await store.append_turn_messages(recent, [Message(role="user", content="问10"), Message(role="assistant", content="答10")])
        await store.update_conversation(recent)  # metadata-only update does not insert messages
        full = await store.get_conversation(conversation.id, user, recent_turns=None)
        assert len(full.messages) == 22
        assert [m.metadata["message_index"] for m in full.messages] == list(range(22))
        assert full.messages[16].metadata["message_id"] == ids[0]
        page = await store.get_history_page(conversation.id, user, limit=5)
        older = await store.get_history_page(conversation.id, user, before=page[0].metadata["message_index"], limit=5)
        assert page[0].metadata["message_index"] == 17
        assert older[-1].metadata["message_index"] == 16
        assert await store.get_history_page(conversation.id, other) == []
        batch = await store.get_turn_range(conversation.id, user, start=2, before=18, limit_turns=3)
        assert [m.metadata["message_index"] for m in batch] == list(range(2, 8))
        assert await store.get_turn_range(conversation.id, other, start=0, before=18) == []
        foreign = PostgresConversationStore(store.settings, default_workspace_id="thelook-analytics")
        assert await foreign.get_turn_range(conversation.id, user, start=0, before=18) == []
        assert await foreign.get_history_page(conversation.id, user) == []
        full.metadata["working_memory"] = {"history_start_index": 22}
        await store.update_conversation(full)
        after_clear = await store.get_conversation(conversation.id, user)
        assert after_clear.messages == []
        assert len((await store.get_conversation(conversation.id, user, recent_turns=None)).messages) == 22
    finally:
        await store.delete_conversation(conversation.id, user)


@pytest.mark.asyncio
async def test_incremental_summary_persists_across_store_and_builder_instances(store):
    from data_analysis_agent.analysis_context import AnalysisContextBuilder
    from data_analysis_agent.analysis_state import AnalysisState
    from data_analysis_agent.budget import BudgetUsage, RequestBudget
    from dataclasses import replace

    user = User(id=f"summary-{uuid.uuid4().hex}", group_memberships=["analyst"])
    state = AnalysisState(task_id="summary-task", workspace_id="olist-demo", dataset_version="data", metric_version="metric")
    conversation = Conversation(id=f"summary-{uuid.uuid4().hex}", user=user,
                                metadata={"working_memory": state.as_dict(),
                                          "dataset_version_id": "data", "metric_version": "metric"})
    calls = []
    async def summarize(turns, source):
        calls.append(source)
        return "用户希望逐步分析交易趋势，待解释口径。"
    try:
        for n in range(9):
            await store.append_turn_messages(conversation, [Message(role="user", content=f"问题{n}" + "中" * 100), Message(role="assistant", content=f"回复{n}" + "中" * 100)])
        loaded = await store.get_conversation(conversation.id, user)
        budget = replace(RequestBudget(), history_compaction_trigger_tokens=1500,
                         history_recent_target_tokens=700)
        builder = AnalysisContextBuilder(summarize, budget=budget)
        await builder.build(loaded, state, store, BudgetUsage(budget))
        assert loaded.metadata["history_summary"] is None
        builder.schedule(conversation.id, user, store)
        await asyncio.gather(*list(builder.tasks.values()))
        restarted_store = PostgresConversationStore(store.settings)
        restored = await restarted_store.get_conversation(conversation.id, user)
        await AnalysisContextBuilder(summarize, budget=budget).build(restored, state, restarted_store, BudgetUsage(budget))
        assert len(calls) == 1
        assert restored.metadata["history_summary"]["text"].startswith("用户希望")
        previous_end = restored.metadata["history_summary"]["source_message_end"]
        await restarted_store.append_turn_messages(restored, [Message(role="user", content="第十问" + "中" * 1000), Message(role="assistant", content="第十答")])
        next_turn = await restarted_store.get_conversation(conversation.id, user)
        restarted = AnalysisContextBuilder(summarize, budget=budget)
        restarted.schedule(conversation.id, user, restarted_store)
        await asyncio.gather(*list(restarted.tasks.values()))
        assert calls[-1].previous_text.startswith("用户希望")
        next_turn = await restarted_store.get_conversation(conversation.id, user)
        assert next_turn.metadata["history_summary"]["source_message_end"] > previous_end
        await store.truncate_from_last_user_message(conversation.id, user)
        edited = await store.get_conversation(conversation.id, user)
        assert edited.metadata["history_summary"] is None and edited.metadata["working_memory"] == {}
    finally:
        await store.delete_conversation(conversation.id, user)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["append", "clear", "edit", "version", "other_owner", "workspace"])
async def test_background_summary_transaction_never_overwrites_messages_or_stale_scope(store, change):
    from dataclasses import replace
    from data_analysis_agent.analysis_context import AnalysisContextBuilder
    from data_analysis_agent.analysis_state import AnalysisState

    owner = User(id=f"summary-race-{uuid.uuid4().hex}", group_memberships=["analyst"])
    state = AnalysisState(task_id="task", workspace_id="olist-demo", dataset_version="data", metric_version="metric")
    conversation = Conversation(id=f"summary-race-{uuid.uuid4().hex}", user=owner,
        metadata={"working_memory": state.as_dict(), "dataset_version_id": "data", "metric_version": "metric"},
        messages=[Message(role="user", content="问题"), Message(role="assistant", content="答复")])
    try:
        await store.update_conversation(conversation)
        snapshot = await store.get_conversation(conversation.id, owner)
        sources = snapshot.messages
        stale_foreground = snapshot.model_copy(deep=True)
        if change == "append":
            await store.append_turn_messages(snapshot, [Message(role="user", content="新问题"), Message(role="assistant", content="新答复")])
            stale_foreground = await store.get_conversation(conversation.id, owner)
        elif change == "clear":
            snapshot.metadata["working_memory"] = replace(state, task_id="new").as_dict()
            await store.update_conversation(snapshot)
        elif change == "version":
            snapshot.metadata["working_memory"] = replace(state, metric_version="new").as_dict()
            snapshot.metadata["metric_version"] = "new"
            await store.update_conversation(snapshot)
        elif change == "edit":
            await store.truncate_from_last_user_message(conversation.id, owner)
        summary = {"text": "分析目标：比较销售。", "scope": AnalysisContextBuilder.scope(state), "source_message_end": 1}
        caller = owner if change != "other_owner" else User(id="someone-else", group_memberships=["analyst"])
        scope = AnalysisContextBuilder.scope(state)
        if change == "workspace":
            scope = {**scope, "workspace_id": "other"}
        result = await store.save_history_summary(conversation.id, caller, scope=scope,
            expected_end=-1, sources=sources, summary=summary, attempt={"status": "generated"})
        assert result is (change == "append")
        if change == "append":
            # Background writes do not create a spurious optimistic state conflict.
            await store.append_turn_messages(stale_foreground, [Message(role="user", content="又一问")])
            loaded = await store.get_conversation(conversation.id, owner, recent_turns=None)
            assert len(loaded.messages) == 5 and loaded.messages[-1].content == "又一问"
            assert loaded.metadata["history_summary"] == summary
            assert await store.save_history_summary(conversation.id, owner, scope=scope,
                expected_end=-1, sources=sources, summary=summary, attempt={}) is False
        else:
            loaded = await store.get_conversation(conversation.id, owner)
            assert loaded.metadata["history_summary"] is None
    finally:
        await store.delete_conversation(conversation.id, owner)


@pytest.mark.asyncio
async def test_agent_reload_uses_context_packet_history_cursor(store):
    from data_analysis_agent.budget import BudgetUsage, RequestBudget, CURRENT_BUDGET
    owner = User(id=f"cursor-{uuid.uuid4().hex}", group_memberships=["analyst"])
    conversation = Conversation(id=f"cursor-{uuid.uuid4().hex}", user=owner,
        messages=[Message(role=role, content=f"{n}") for n in range(35) for role in ("user", "assistant")])
    try:
        await store.update_conversation(conversation)
        evidence = BudgetUsage(RequestBudget())
        evidence.context_packet = {"recent_start_index": 0}
        token = CURRENT_BUDGET.set(evidence)
        try:
            loaded = await store.get_conversation(conversation.id, owner)
        finally:
            CURRENT_BUDGET.reset(token)
        assert len(loaded.messages) == 70 and loaded.metadata["history_start_index"] == 0
        rows = await store.get_turn_range(conversation.id, owner, start=0, before=70, limit_turns=20)
        assert len(rows) == 40
    finally:
        await store.delete_conversation(conversation.id, owner)
