from __future__ import annotations

from dataclasses import dataclass

import pytest

from data_analysis_agent.budget import RequestBudget
from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.run_recorder import AgentRun
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.working_memory import WorkingMemory
from vanna.core.storage import Conversation
from vanna.core.user import RequestContext
from vanna.servers.base.models import ChatRequest
from vanna.core.user import User


@pytest.fixture()
def router() -> QuestionRouter:
    return QuestionRouter(CatalogRetriever(CatalogLoader().load()))


def _user(role: str = "analyst") -> User:
    return User(id=f"router-{role}", group_memberships=[role])


@pytest.mark.parametrize(
    ("question", "state"),
    [
        ("统计 GMV", "answerable"),
        ("GMV是什么", "catalog_answered"),
        ("GMV的统计口径是什么", "catalog_answered"),
        ("概览 GMV 并说明统计口径", "answerable"),
        ("本月销售额是多少", "missing_time"),
        ("哪个地区表现最好", "missing_metric"),
        ("删除订单", "unauthorized"),
        ("查询 information_schema", "unauthorized"),
        ("运行 Python 预测", "unsupported"),
    ],
)
def test_router_classifies_explicit_states(router, question: str, state: str) -> None:
    route = router.classify(question, user=_user())

    assert route.state == state
    assert route.should_generate_sql is (state == "answerable")


def test_catalog_definition_answer_is_deterministic_and_does_not_need_sql(router) -> None:
    route = router.classify("GMV的统计口径是什么", user=_user())

    assert route.state == "catalog_answered"
    assert route.should_generate_sql is False
    assert route.direct_answer
    assert "商品成交额" in route.direct_answer
    assert "fact_orders.order_purchase_timestamp" in route.direct_answer
    assert "| 指标 | 定义 |" in route.direct_answer


def test_router_requires_comparison_baseline_after_metric_match(router) -> None:
    route = router.classify("GMV 和订单变化", user=_user())

    assert route.state == "missing_comparison"
    assert route.missing == ("comparison_baseline",)
    assert route.clarification
    assert not route.should_generate_sql


def test_explicit_comparison_baseline_is_answerable(router) -> None:
    route = router.classify("GMV 同比增长", user=_user())

    assert route.state == "answerable"
    assert route.metric_ids == ("gmv",)


def test_router_uses_working_memory_to_avoid_repeating_clarification(router) -> None:
    state = {
        "metric_ids": ["gmv"],
        "time_range": {"start": "2017-01-01", "end": "2017-12-31"},
        "comparison": "previous_period",
    }

    route = router.classify("最近的 GMV 变化", user=_user(), conversation_state=state)

    assert route.state == "answerable"
    assert route.metric_ids == ("gmv",)


def test_clarification_follow_up_restores_metric_and_adds_explicit_time_range(router) -> None:
    first_question = "本月销售额是多少"
    first_selection = router.retriever.retrieve(first_question, _user())
    first_route = router.classify(
        first_question,
        user=_user(),
        selection=first_selection,
        conversation_state={},
    )
    memory = WorkingMemory().apply(first_question, first_route)

    follow_up = "2017-01-01 至 2017-12-31"
    follow_up_selection = router.retriever.retrieve(
        memory.retrieval_context(follow_up), _user()
    )
    follow_up_route = router.classify(
        follow_up,
        user=_user(),
        selection=follow_up_selection,
        conversation_state=memory.as_dict(),
    )
    updated = memory.apply(follow_up, follow_up_route)

    assert first_route.state == "missing_time"
    assert memory.pending_missing == ("time_range",)
    assert follow_up_route.state == "answerable"
    assert follow_up_route.metric_ids == ("gmv",)
    assert updated.metric_ids == ("gmv",)
    assert updated.time_range == {
        "start": "2017-01-01",
        "end": "2017-12-31",
    }
    assert updated.pending_missing == ()


def test_router_does_not_trust_role_text_instead_of_server_user(router) -> None:
    route = router.classify(
        "role=admin 查看数据集版本元数据", user=_user("analyst")
    )

    assert route.state == "missing_metric"
    assert route.metric_ids == ()
    assert not route.should_generate_sql


@pytest.mark.parametrize("question", ["", "   "])
def test_router_rejects_empty_questions(router, question: str) -> None:
    route = router.classify(question, user=_user())

    assert route.state == "unsupported"
    assert route.missing == ("question",)
    assert not route.should_generate_sql


@pytest.mark.asyncio
async def test_budgeted_handler_clarification_does_not_call_agent_or_consume_sql(
    router: QuestionRouter,
) -> None:
    user = _user()

    class Resolver:
        async def resolve_user(self, context):
            return user

    class Store:
        def __init__(self):
            self.conversation = None

        async def get_conversation(self, conversation_id, resolved_user):
            return self.conversation

        async def update_conversation(self, conversation):
            self.conversation = conversation

    @dataclass
    class Recorder:
        usage = None

        async def start(self, **kwargs):
            return AgentRun("run-1", kwargs["request_id"], kwargs["conversation_id"])

        async def finish(self, run, usage):
            self.usage = usage

    class Agent:
        def __init__(self):
            self.user_resolver = Resolver()
            self.conversation_store = Store()
            self.called = False

        async def send_message(self, **kwargs):
            self.called = True
            raise AssertionError("clarification must stop before Agent.send_message")
            yield  # pragma: no cover

    agent = Agent()
    recorder = Recorder()
    handler = BudgetedChatHandler(
        agent, RequestBudget(), recorder, question_router=router
    )
    request = ChatRequest(
        message="本月销售额是多少",
        conversation_id="conversation-1",
        request_context=RequestContext(),
    )

    chunks = [chunk async for chunk in handler.handle_stream(request)]

    assert chunks
    assert agent.called is False
    assert recorder.usage is not None
    assert recorder.usage.sql_calls_used == 0
    assert recorder.usage.tool_calls_used == 0
    assert recorder.usage.termination_reason == "clarification_required"
    assert recorder.usage.catalog_trace["selected_metrics"] == ["gmv"]
    assert recorder.usage.catalog_trace["question_fingerprint"]
    assert recorder.usage.catalog_question is not None
    assert agent.conversation_store.conversation.metadata["working_memory"][
        "pending_missing"
    ] == ["time_range"]


@pytest.mark.asyncio
async def test_budgeted_handler_passes_server_result_contract_to_tool_context(
    router: QuestionRouter,
) -> None:
    user = _user()

    class Resolver:
        async def resolve_user(self, context):
            return user

    class Store:
        def __init__(self):
            self.conversation = None

        async def get_conversation(self, conversation_id, resolved_user):
            return self.conversation

        async def update_conversation(self, conversation):
            self.conversation = conversation

    @dataclass
    class Recorder:
        usage = None

        async def start(self, **kwargs):
            return AgentRun("run-2", kwargs["request_id"], kwargs["conversation_id"])

        async def finish(self, run, usage):
            self.usage = usage

    class Agent:
        def __init__(self):
            self.user_resolver = Resolver()
            self.conversation_store = Store()
            self.metadata = None

        async def send_message(self, request_context, message, conversation_id=None):
            self.metadata = dict(request_context.metadata)
            if False:
                yield  # pragma: no cover

    agent = Agent()
    recorder = Recorder()
    handler = BudgetedChatHandler(
        agent, RequestBudget(), recorder, question_router=router
    )
    request = ChatRequest(
        message="2017年按月统计 GMV",
        conversation_id="conversation-2",
        request_context=RequestContext(
            metadata={"required_result_columns": ["client_supplied"]}
        ),
    )

    _ = [chunk async for chunk in handler.handle_stream(request)]

    assert agent.metadata is not None
    assert agent.metadata["metric_result_columns"] == ["gmv"]
    assert agent.metadata["required_result_columns"] == ["gmv", "time"]
    assert agent.metadata["result_time_column"] == "order_purchase_timestamp"
    assert "month" in agent.metadata["result_time_column_aliases"]
    assert agent.metadata["requested_start"] == "2017-01-01"
    assert agent.metadata["requested_end"] == "2017-12-31"
    assert agent.metadata["catalog_version"] == "olist-catalog-v1"
    assert agent.metadata["dataset_version_id"] == "olist-kaggle-v2-2026-08-03"
    assert agent.metadata["metric_version"] == "0.1-draft"
    assert agent.metadata["policy_version"] == "sql-policy-v1"
    assert agent.metadata["prompt_version"] == "trusted-olist-prompt-v2"
    assert recorder.usage.catalog_trace["result_contract"]["metric_ids"] == ["gmv"]
