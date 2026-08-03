from __future__ import annotations

import os
import uuid

import pytest

from data_analysis_agent.conversation_store import (
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
