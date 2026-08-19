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


@pytest.mark.parametrize(
    ("question", "intent", "requires_database"),
    [
        ("你能做什么", "help", False),
        ("GMV通常为什么下降", "general_business", False),
        ("如何提升GMV", "general_business", False),
        ("电商经营分析一般看哪些维度", "general_business", False),
        ("GMV这个指标在电商里一般怎么理解", "general_business", False),
        ("概览 GMV 并说明统计口径", "mixed_request", True),
        ("结合当前数据，为什么GMV下降", "data_analysis", False),
    ],
)
def test_router_separates_evidence_intent_from_metric_match(
    router: QuestionRouter,
    question: str,
    intent: str,
    requires_database: bool,
) -> None:
    route = router.classify(question, user=_user())

    assert route.intent == intent
    assert route.requires_database is requires_database
    assert route.evidence_mode in {
        "none",
        "general_knowledge",
        "mixed",
        "clarification",
        "database_result",
    }
    assert route.as_dict()["intent"] == intent


def test_help_route_is_deterministic_and_has_no_metric_requirement(router) -> None:
    route = router.classify("你能做什么", user=_user())

    assert route.state == "help"
    assert route.direct_answer
    assert "只读 PostgreSQL" in route.direct_answer
    assert route.should_generate_sql is False


def test_result_followup_uses_previous_result_without_new_sql(router) -> None:
    route = router.classify(
        "这个结果为什么这么高？",
        user=_user(),
        conversation_state={"previous_result_summary": "GMV 为 100，统计范围为 2017 年。"},
    )

    assert route.state == "result_followup"
    assert route.intent == "result_followup"
    assert route.evidence_mode == "previous_result"
    assert route.should_generate_sql is False


def test_result_followup_without_evidence_requires_context(router) -> None:
    route = router.classify("这个结果为什么这么高？", user=_user())

    assert route.state == "clarification_required"
    assert route.missing == ("previous_result",)
    assert route.should_generate_sql is False


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


@pytest.mark.parametrize(
    "question",
    (
        "不同支付方式的 GMV",
        "各品类平均履约天数和好评率",
    ),
)
def test_router_requires_catalog_attribution_policy_before_sql(
    router: QuestionRouter, question: str
) -> None:
    route = router.classify(question, user=_user())

    assert route.state == "clarification_required"
    assert route.intent == "clarification_required"
    assert route.requires_database is False
    assert route.should_generate_sql is False
    assert route.reason_code == "dimension_attribution_requires_clarification"
    assert route.clarification
    assert "归属口径" in route.clarification


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
    assert agent.metadata["query_plan"]["plan_type"] == "single_metric"
    assert agent.metadata["query_plan"]["required_result_columns"] == ["gmv", "time"]
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
    assert recorder.usage.query_plan["plan_type"] == "single_metric"


@pytest.mark.asyncio
async def test_budgeted_handler_general_business_does_not_expose_sql_tools(
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
            return AgentRun("run-general", kwargs["request_id"], kwargs["conversation_id"])

        async def finish(self, run, usage):
            self.usage = usage

    class Agent:
        def __init__(self):
            self.user_resolver = Resolver()
            self.conversation_store = Store()
            self.llm_requests = []

        async def _send_llm_request(self, request):
            self.llm_requests.append(request)
            from data_analysis_agent.budget import BudgetSafetyMiddleware
            from vanna.core.llm import LlmResponse

            middleware = BudgetSafetyMiddleware()
            request = await middleware.before_llm_request(request)
            response = LlmResponse(
                content="这是通用经营建议，不代表当前数据库结果。",
                usage={"total_tokens": 7},
            )
            return await middleware.after_llm_response(request, response)

        async def send_message(self, **kwargs):
            raise AssertionError("general business request must not enter the SQL Agent")
            yield  # pragma: no cover

    agent = Agent()
    recorder = Recorder()
    handler = BudgetedChatHandler(
        agent, RequestBudget(), recorder, question_router=router
    )
    request = ChatRequest(
        message="如何提升GMV",
        conversation_id="conversation-general",
        request_context=RequestContext(),
    )

    chunks = [chunk async for chunk in handler.handle_stream(request)]

    assert chunks
    assert len(agent.llm_requests) == 1
    assert agent.llm_requests[0].tools is None
    assert agent.llm_requests[0].metadata["purpose"] == "tool_free_response"
    assert agent.llm_requests[0].metadata["intent"] == "general_business"
    assert recorder.usage is not None
    assert recorder.usage.sql_calls_used == 0
    assert recorder.usage.tool_calls_used == 0
    assert recorder.usage.llm_rounds_used == 1
    assert recorder.usage.termination_reason == "completed"


@pytest.mark.asyncio
async def test_budgeted_handler_persists_only_trusted_result_summary(
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
            return AgentRun("run-summary", kwargs["request_id"], kwargs["conversation_id"])

        async def finish(self, run, usage):
            self.usage = usage

    class Agent:
        def __init__(self):
            self.user_resolver = Resolver()
            self.conversation_store = Store()

        async def send_message(self, request_context, message, conversation_id=None):
            request_context.metadata["budget_usage"].set_result_summary(
                '已通过结果合同的可信结果摘要：{"metric_ids":["gmv"],"row_count":1}'
            )
            if False:
                yield  # pragma: no cover

    agent = Agent()
    recorder = Recorder()
    handler = BudgetedChatHandler(
        agent, RequestBudget(), recorder, question_router=router
    )
    request = ChatRequest(
        message="统计 2017 年 GMV",
        conversation_id="conversation-summary",
        request_context=RequestContext(),
    )

    _ = [chunk async for chunk in handler.handle_stream(request)]

    stored = agent.conversation_store.conversation.metadata["working_memory"]
    assert "可信结果摘要" in stored["previous_result_summary"]
