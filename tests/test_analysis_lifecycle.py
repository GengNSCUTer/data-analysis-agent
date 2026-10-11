"""M5 analysis-context lifecycle: clear, version invalidation, and conflicts."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from vanna.core.storage import Conversation, Message
from vanna.core.user import RequestContext, User
from vanna.servers.base.models import ChatRequest

from data_analysis_agent.analysis_lifecycle import (
    analysis_scope_is_current,
    clear_active_analysis_state,
    current_result_artifact,
    prepare_turn_memory,
)
from data_analysis_agent.budget import RequestBudget
from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.conversation_store import ConversationStateConflict
from data_analysis_agent.metric_context import OLIST_WORKSPACE
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.run_recorder import AgentRun
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever


def _user() -> User:
    return User(id="lifecycle-analyst", group_memberships=["analyst"])


def _artifact() -> dict[str, object]:
    return {
        "artifact_id": "artifact-old",
        "summary": "旧结果：2016 年 GMV 为 10",
        "metric_ids": ["gmv"],
        "chart_state": "not_rendered",
        "workspace_id": "olist-demo",
    }


def _seeded_conversation(*, metric_version: str) -> Conversation:
    return Conversation(
        id="conversation-lifecycle",
        user=_user(),
        messages=[Message(role="user", content="统计 2016 年 GMV")],
        metadata={
            "workspace_id": OLIST_WORKSPACE.workspace_id,
            "dataset_version_id": OLIST_WORKSPACE.dataset_version_id,
            "metric_version": metric_version,
            "state_version": 3,
            "history_summary": {"text": "旧摘要提到 2016 年 GMV", "source_sha256": "abc"},
            "working_memory": {
                "metric_ids": ["gmv"],
                "time_range": {"start": "2016-01-01", "end": "2016-12-31"},
                "comparison_baseline": "previous_period",
                "dimensions": ["customer_state"],
                "previous_result_summary": "旧结果：2016 年 GMV 为 10",
                "previous_result_artifact": _artifact(),
            },
        },
    )


def test_unpinned_conversation_is_current_and_matching_scope_keeps_artifact() -> None:
    assert analysis_scope_is_current({}, OLIST_WORKSPACE)
    conversation = _seeded_conversation(metric_version=OLIST_WORKSPACE.metric_version)
    assert analysis_scope_is_current(conversation.metadata, OLIST_WORKSPACE)
    artifact = current_result_artifact(conversation, OLIST_WORKSPACE)
    assert artifact is not None
    assert artifact.artifact_id == "artifact-old"


def test_version_mismatch_hides_artifact_and_prepare_drops_analysis_state() -> None:
    conversation = _seeded_conversation(metric_version="0.1-draft")
    assert current_result_artifact(conversation, OLIST_WORKSPACE) is None

    memory, notice = prepare_turn_memory(conversation, OLIST_WORKSPACE)

    assert notice
    assert "版本已变化" in notice
    assert memory.metric_ids == ()
    assert memory.previous_result_artifact is None
    assert "history_summary" not in conversation.metadata
    assert conversation.metadata["metric_version"] == OLIST_WORKSPACE.metric_version
    assert conversation.messages[0].content == "统计 2016 年 GMV"


def test_clear_keeps_messages_and_history_summary_but_drops_active_result() -> None:
    conversation = _seeded_conversation(metric_version="0.1-draft")

    clear_active_analysis_state(conversation, OLIST_WORKSPACE)

    assert conversation.messages[0].content == "统计 2016 年 GMV"
    assert conversation.metadata["history_summary"]["source_sha256"] == "abc"
    assert conversation.metadata["working_memory"]["metric_ids"] == []
    assert conversation.metadata["working_memory"]["previous_result_artifact"] is None
    assert conversation.metadata["metric_version"] == OLIST_WORKSPACE.metric_version
    assert current_result_artifact(conversation, OLIST_WORKSPACE) is None


@dataclass
class _Recorder:
    usage: object | None = None

    async def start(self, **kwargs):
        return AgentRun("run-lifecycle", kwargs["request_id"], kwargs["conversation_id"])

    async def finish(self, run, usage):
        self.usage = usage


def _handler(store, router: QuestionRouter) -> BudgetedChatHandler:
    user = _user()

    class Resolver:
        async def resolve_user(self, context):
            return user

    class Agent:
        def __init__(self):
            self.user_resolver = Resolver()
            self.conversation_store = store
            self.called = False

        async def send_message(self, request_context, message, conversation_id=None):
            self.called = True
            if False:
                yield  # pragma: no cover

    agent = Agent()
    handler = BudgetedChatHandler(agent, RequestBudget(), _Recorder(), question_router=router)
    return handler


@pytest.fixture()
def router() -> QuestionRouter:
    return QuestionRouter(CatalogRetriever(CatalogLoader().load()))


@pytest.mark.asyncio
async def test_matching_version_still_reuses_analysis_state(router: QuestionRouter) -> None:
    class Store:
        def __init__(self):
            self.conversation = _seeded_conversation(
                metric_version=OLIST_WORKSPACE.metric_version
            )

        async def get_conversation(self, conversation_id, resolved_user, workspace_id=None):
            return self.conversation

        async def update_conversation(self, conversation):
            self.conversation = conversation

    handler = _handler(Store(), router)
    # Reuse a confirmed new-format range. Unmarked legacy ranges intentionally
    # require endpoint reconfirmation and must not reach candidate generation.
    handler.agent.conversation_store.conversation.metadata["working_memory"]["time_range"] = {
        "start": "2016-01-01", "end": "2017-01-01", "end_semantics": "exclusive",
    }
    request = ChatRequest(
        message="最近的 GMV 变化",
        conversation_id="conversation-lifecycle",
        request_context=RequestContext(),
    )

    chunks = [chunk async for chunk in handler.handle_stream(request)]

    assert handler.agent.called is True
    titles = [chunk.rich["data"].get("title") for chunk in chunks if chunk.rich]
    assert "分析上下文已失效" not in titles
    assert handler.agent.conversation_store.conversation.metadata["working_memory"][
        "time_range"
    ] == {"start": "2016-01-01", "end": "2017-01-01", "end_semantics": "exclusive"}


@pytest.mark.asyncio
async def test_version_mismatch_does_not_reuse_old_metric_time_or_result(
    router: QuestionRouter,
) -> None:
    class Store:
        def __init__(self):
            self.conversation = _seeded_conversation(metric_version="0.1-draft")

        async def get_conversation(self, conversation_id, resolved_user, workspace_id=None):
            return self.conversation

        async def update_conversation(self, conversation):
            self.conversation = conversation

    handler = _handler(Store(), router)
    request = ChatRequest(
        message="最近的 GMV 变化",
        conversation_id="conversation-lifecycle",
        request_context=RequestContext(),
    )

    chunks = [chunk async for chunk in handler.handle_stream(request)]

    stored = handler.agent.conversation_store.conversation.metadata
    assert handler.agent.called is False
    assert chunks[0].rich["data"]["title"] == "分析上下文已失效"
    assert stored["working_memory"]["time_range"] is None
    assert stored["working_memory"]["previous_result_summary"] is None
    assert stored["working_memory"]["comparison_baseline"] is None
    assert "history_summary" not in stored
    assert stored["metric_version"] == OLIST_WORKSPACE.metric_version
    assert handler.agent.conversation_store.conversation.messages[0].content == "统计 2016 年 GMV"


@pytest.mark.asyncio
async def test_stale_state_version_returns_recoverable_conflict(
    router: QuestionRouter,
) -> None:
    class Store:
        def __init__(self):
            self.conversation = _seeded_conversation(
                metric_version=OLIST_WORKSPACE.metric_version
            )
            self.persisted = None

        async def get_conversation(self, conversation_id, resolved_user, workspace_id=None):
            return self.conversation

        async def update_conversation(self, conversation):
            raise ConversationStateConflict("conversation state_version is stale")

    store = Store()
    handler = _handler(store, router)
    request = ChatRequest(
        message="最近的 GMV 变化",
        conversation_id="conversation-lifecycle",
        request_context=RequestContext(),
    )

    chunks = [chunk async for chunk in handler.handle_stream(request)]

    assert store.persisted is None
    assert chunks[-1].rich["data"]["title"] == "会话状态已更新"
    assert "未覆盖" in chunks[-1].rich["data"]["description"]
