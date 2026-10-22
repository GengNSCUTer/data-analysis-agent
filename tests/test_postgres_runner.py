"""Opt-in integration coverage for the project-local PostgreSQL trust boundary."""

from __future__ import annotations

import os
import asyncio
import time
import threading
from dataclasses import replace
import uuid

import psycopg2
import pytest

from data_analysis_agent.postgres_runner import (
    PostgresConnectionSettings,
    SecurePostgresRunner,
    _cancellable_query,
)
from data_analysis_agent.budget import BudgetedToolRegistry, BudgetUsage, RequestBudget
from data_analysis_agent.budget import BudgetSafetyMiddleware
from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.conversation_store import PostgresConversationStore
from data_analysis_agent.result_validator import ResultValidationError, ResultValidator
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.run_recorder import PostgresRunRecorder
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy
from data_analysis_agent.sql_repair import SafeSqlExecutionError
from data_analysis_agent.query_filters import QueryFilter
from data_analysis_agent.trusted_sql_tool import TrustedRunSqlTool
from vanna import Agent
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.agent.config import AgentConfig
from vanna.core.llm import LlmRequest, LlmResponse, LlmService, LlmStreamChunk
from vanna.core.tool import ToolCall, ToolContext
from vanna.core.user import RequestContext
from vanna.core.user.resolver import UserResolver
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.servers.base.models import ChatRequest


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def require_project_database() -> None:
    if os.getenv("RUN_PROJECT_DB") != "1":
        pytest.skip("set RUN_PROJECT_DB=1 to run against the project-local PostgreSQL instance")


def make_context(user_id: str, role: str = "analyst") -> ToolContext:
    return ToolContext(
        user=User(id=user_id, group_memberships=[role]),
        conversation_id=f"postgres-integration-{uuid.uuid4().hex}",
        request_id=f"postgres-integration-{uuid.uuid4().hex}",
        agent_memory=DemoAgentMemory(),
        metadata={"question": "integration test"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_filter", [True, False])
async def test_grouping_error_repair_rechecks_plan_before_real_reader(keep_filter, tmp_path):
    """Use a controlled candidate, real PostgreSQL error, policy and validation."""
    context = make_context(f"repair-chain-{uuid.uuid4().hex}")
    context.metadata.update({
        "required_result_columns": ["customer_state", "paid_order_count"],
        "metric_result_columns": ["paid_order_count"],
        "query_plan": {"metric_ids": ["paid_order_count"], "filters": [
            {"dimension": "customer_state", "value": "SP",
             "sources": [{"table": "dim_customers", "column": "customer_state"}]},
        ]},
    })
    original = (
        "SELECT c.customer_state, COUNT(DISTINCT o.order_id) AS paid_order_count "
        "FROM analytics.fact_orders o JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
        "WHERE o.order_status NOT IN ('canceled', 'unavailable') AND c.customer_state='SP'"
    )
    prompts = []

    async def candidate(prompt, _):
        prompts.append(prompt)
        sql = original if keep_filter else original.replace(" AND c.customer_state='SP'", "")
        return sql + " GROUP BY c.customer_state"

    runner = SecurePostgresRunner(result_validator=ResultValidator())
    tool = TrustedRunSqlTool(
        runner, repair_provider=candidate, repair_policy=SqlPolicy(),
        file_system=LocalFileSystem(str(tmp_path)),
    )
    result = await tool.execute(context, RunSqlToolArgs(sql=original))
    assert len(prompts) == 1 and "syntax" in prompts[0] and "current_query_plan" in prompts[0]
    assert result.success is keep_filter
    audits = runner.audit.list_recent(context.user.id, "analyst")
    assert len(audits) == 2 and audits[1]["policy_status"] == "execution_error"
    if not keep_filter:
        assert audits[0]["policy_status"] == "rejected"
        assert result.metadata["terminal_reason"] == "plan_sql_mismatch"
        return
    assert audits[0]["policy_status"] == "allowed"
    assert context.metadata["result_validation"]["state"] == "valid"
    with psycopg2.connect(
        host=runner.settings.host, port=runner.settings.port,
        database=runner.settings.database, user=runner.settings.reader_user,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM analytics.fact_orders WHERE order_status NOT IN ('canceled','unavailable') "
                "AND customer_id IN (SELECT customer_id FROM analytics.dim_customers WHERE customer_state='SP')"
            )
            expected = cursor.fetchone()[0]
    assert result.metadata["results"] == [{"customer_state": "SP", "paid_order_count": expected}]


@pytest.mark.asyncio
async def test_cancel_stops_actual_postgres_worker_and_closes_connection():
    started = threading.Event()
    connections = []
    settings = PostgresConnectionSettings.from_environment()

    def slow_query(cancellation):
        connection = psycopg2.connect(host=settings.host, port=settings.port, database=settings.database,
                                      user=settings.reader_user, application_name='stage5-cancel-test')
        connections.append(connection)
        try:
            cancellation.attach(connection)
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '10s'")
                started.set()
                # Test the cancellation transport, not an online allowed function.
                cursor.execute('SELECT pg_sleep(8)')
        finally:
            cancellation.close(connection)

    worker = asyncio.create_task(_cancellable_query(slow_query))
    assert await asyncio.to_thread(started.wait, 2)
    await asyncio.sleep(.05)
    begin = time.monotonic()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert time.monotonic() - begin < 2
    assert connections[0].closed
    connection = psycopg2.connect(host=settings.host, port=settings.port, database=settings.database, user=settings.reader_user)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name='stage5-cancel-test'")
            assert cursor.fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_actual_reader_statement_timeout_is_audited_without_partial_result():
    settings = replace(PostgresConnectionSettings.from_environment(), statement_timeout_ms=1)
    runner = SecurePostgresRunner(settings=settings)
    context = make_context('stage5-timeout-' + uuid.uuid4().hex[:8])
    with pytest.raises(SafeSqlExecutionError):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT SUM(i.price) AS gmv FROM fact_order_items i JOIN fact_orders o ON o.order_id=i.order_id WHERE o.order_status NOT IN ('canceled','unavailable')"), context)
    assert context.metadata['sql_error']['category'] == 'timeout'
    assert not context.metadata.get('result_contract_satisfied')
    assert runner.audit.list_recent(context.user.id, 'analyst')[0]['policy_status'] == 'execution_error'


@pytest.mark.asyncio
async def test_display_filter_resolves_to_its_actual_reader_column():
    runner = SecurePostgresRunner()
    sources = (("dim_products", "product_category_name"),
               ("dim_category_translation", "product_category_name"),
               ("dim_category_translation", "product_category_name_english"))
    filters = (QueryFilter("customer_state", "SP", (("dim_customers", "customer_state"),)),
               QueryFilter("product_category_name", "bed_bath_table", sources))
    resolved = await runner.resolve_filter_sources(filters)
    assert resolved[0] == filters[0]
    assert resolved[1].sources == sources
    assert resolved[1].value_sources == (("dim_category_translation", "product_category_name_english"),)
    raw = await runner.resolve_filter_sources((QueryFilter("product_category_name", "cama_mesa_banho", sources),))
    assert raw[0].value_sources == sources[:2]
    # Values are parameters. A quote/SQL-looking value is neither executed
    # nor turned into a mandatory clarification when it has no match.
    unknown = QueryFilter("product_category_name", "missing' OR TRUE --", sources)
    assert await runner.resolve_filter_sources((unknown,)) == (unknown,)


@pytest.mark.asyncio
async def test_runner_executes_allowed_query_and_audits_policy_rejection() -> None:
    user_id = f"postgres-test-{uuid.uuid4().hex[:12]}"
    runner = SecurePostgresRunner()
    context = make_context(user_id)
    envelope = ResultBudget.aggregate().as_metadata()
    context.metadata.update({
        **envelope,
        "required_result_columns": ["customer_state", "paid_order_count"],
        "metric_result_columns": ["paid_order_count"],
        "additive_metric_ids": ["paid_order_count"],
        "query_plan": {**envelope, "top_n": {"customer_state": 5}},
    })

    dataframe = await runner.run_sql(
        RunSqlToolArgs(
            sql=(
                "SELECT customer_state, COUNT(DISTINCT order_id) AS paid_order_count "
                "FROM fact_orders JOIN dim_customers USING (customer_id) "
                "WHERE order_status NOT IN ('canceled', 'unavailable') "
                "GROUP BY customer_state ORDER BY paid_order_count DESC LIMIT 5"
            )
        ),
        context,
    )

    # Check against this fixture's facts, not the real Olist snapshot's top state.
    # The bundled synthetic database has three states; LIMIT 5 need not yield five.
    with psycopg2.connect(
        host=runner.settings.host, port=runner.settings.port,
        database=runner.settings.database, user=runner.settings.reader_user,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.customer_state, COUNT(DISTINCT o.order_id) "
                "FROM analytics.fact_orders o JOIN analytics.dim_customers c "
                "ON c.customer_id=o.customer_id "
                "WHERE o.order_status NOT IN ('canceled', 'unavailable') "
                "GROUP BY c.customer_state"
            )
            expected = dict(cursor.fetchall())
    assert len(dataframe) == min(5, len(expected))
    assert dataframe['paid_order_count'].tolist() == sorted(expected.values(), reverse=True)[:5]
    assert all(expected[row.customer_state] == row.paid_order_count for row in dataframe.itertuples())
    assert context.metadata["result_budget_check"]["state"] == "validated"
    from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX
    import json
    evidence = json.loads(context.metadata["validated_result_summary"].removeprefix(RESULT_EVIDENCE_PREFIX))
    assert evidence["coverage"] == "complete"
    assert evidence["statistics"]["paid_order_count"]["sum_of_returned_rows"] == sum(expected.values())

    with pytest.raises(PolicyViolation, match="only SELECT"):
        await runner.run_sql(RunSqlToolArgs(sql="DELETE FROM fact_orders"), context)

    audits = runner.audit.list_recent(user_id, "analyst")
    assert [audit["policy_status"] for audit in audits[:2]] == ["rejected", "allowed"]
    assert audits[1]["question"] == "integration test"
    assert "analytics.fact_orders" in audits[1]["final_sql"]


@pytest.mark.asyncio
async def test_runner_audits_unimplemented_modes_without_a_reader_query() -> None:
    runner = SecurePostgresRunner()
    context = make_context(f"budget-mode-{uuid.uuid4().hex[:12]}")
    envelope = ResultBudget(query_mode="export", aggregate_result_rows=None).as_metadata()
    context.metadata.update({**envelope, "query_plan": envelope})
    with pytest.raises(PolicyViolation, match="export is not enabled"):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT 1 AS gmv"), context)
    audit = runner.audit.list_recent(context.user.id, "analyst")[0]
    assert audit["policy_status"] == "rejected"
    assert audit["final_sql"] is None
    assert audit["policy_reason"] == "result_budget:query_mode export is not enabled"


@pytest.mark.asyncio
async def test_runner_rejects_columns_outside_the_server_result_contract() -> None:
    """A Policy-allowed projection must still satisfy the server contract."""
    runner = SecurePostgresRunner()
    context = make_context(f"exact-columns-{uuid.uuid4().hex[:12]}")
    context.metadata.update(
        {
            "required_result_columns": ["paid_order_count"],
            "metric_result_columns": ["paid_order_count"],
            "exact_result_columns": True,
            "metric_value_constraints": {
                "paid_order_count": {"minimum": 0, "integer_like": True}
            },
        }
    )

    frame = await runner.run_sql(
        RunSqlToolArgs(
            sql=(
                "SELECT COUNT(DISTINCT order_id) AS paid_order_count, "
                "'extra' AS extra FROM fact_orders"
            )
        ),
        context,
    )

    validation = context.metadata["result_validation"]
    assert validation["state"] == "degraded"
    assert list(frame.columns) == ["paid_order_count"]
    assert context.metadata.get("result_contract_satisfied") is None
    assert "extra" in context.metadata["result_display_notice"]


@pytest.mark.asyncio
async def test_empty_query_preserves_database_column_metadata():
    runner = SecurePostgresRunner()
    context = make_context(f"empty-columns-{uuid.uuid4().hex[:12]}")
    context.metadata.update({"required_result_columns": ["customer_state", "gmv"],
                             "metric_result_columns": ["gmv"], "exact_result_columns": True})
    frame = await runner.run_sql(RunSqlToolArgs(sql=(
        "SELECT customer_state, SUM(price) AS gmv FROM fact_order_items "
        "JOIN fact_orders USING(order_id) JOIN dim_customers USING(customer_id) "
        "WHERE order_purchase_timestamp >= TIMESTAMP '2020-01-01' GROUP BY customer_state"
    )), context)
    assert frame.empty and list(frame.columns) == ["customer_state", "gmv"]
    validation = context.metadata["result_validation"]
    assert validation["state"] == "degraded" and not validation["missing_columns"]
    assert "没有返回数据" in validation["reason"]
    assert not context.metadata.get("result_contract_satisfied")


@pytest.mark.asyncio
async def test_runner_treats_resource_cap_as_possible_truncation() -> None:
    """An unplanned query's resource cap cannot prove result completeness."""
    runner = SecurePostgresRunner(replace(PostgresConnectionSettings.from_environment(), max_rows=2))
    context = make_context(f"policy-limit-{uuid.uuid4().hex[:12]}")

    frame = await runner.run_sql(
        RunSqlToolArgs(sql="SELECT customer_state FROM dim_customers"), context
    )

    validation = context.metadata["result_validation"]
    assert validation["state"] == "degraded"
    assert validation["truncated"] is True
    assert validation["row_count"] == 2
    assert len(frame) == 2
    assert context.metadata.get("result_contract_satisfied") is None


@pytest.mark.asyncio
async def test_result_contract_state_flows_through_real_runner_and_budget_registry(tmp_path) -> None:
    """Exercise the production runner/tool/registry boundary against PostgreSQL."""
    suffix = uuid.uuid4().hex[:12]
    user_id = f"contract-chain-{suffix}"
    settings = PostgresConnectionSettings.from_environment()
    usage = BudgetUsage(RequestBudget(max_tool_calls=3, max_sql_calls=2))
    runner = SecurePostgresRunner(
        settings=settings,
        result_validator=ResultValidator(settings.max_rows),
        model_name="integration-test-model",
    )
    registry = BudgetedToolRegistry()
    registry.register_local_tool(
        TrustedRunSqlTool(
            sql_runner=runner,
            file_system=LocalFileSystem(str(tmp_path)),
        ),
        access_groups=["analyst"],
    )
    context = ToolContext(
        user=User(id=user_id, group_memberships=["analyst"]),
        conversation_id=f"contract-conversation-{suffix}",
        request_id=f"contract-request-{suffix}",
        agent_memory=DemoAgentMemory(),
        metadata={
            "question": "统计有效订单数",
            "budget_usage": usage,
            "required_result_columns": ["paid_order_count"],
            "metric_result_columns": ["paid_order_count"],
        },
    )

    first = await registry.execute(
        ToolCall(
            id="first-sql",
            name="run_sql",
            arguments={
                "sql": (
                    "SELECT COUNT(DISTINCT order_id) AS paid_order_count "
                    "FROM fact_orders "
                    "WHERE order_status NOT IN ('canceled', 'unavailable')"
                )
            },
        ),
        context,
    )

    assert first.success is True
    assert context.metadata["result_validation"]["state"] == "valid"
    assert context.metadata["result_contract_satisfied"] is True
    assert usage.result_contract_satisfied is True
    assert usage.result_summary
    assert usage.sql_calls_used == 1
    assert usage.tool_calls_used == 1

    second = await registry.execute(
        ToolCall(
            id="redundant-sql",
            name="run_sql",
            arguments={"sql": "SELECT 1 AS paid_order_count"},
        ),
        context,
    )

    assert second.success is True
    assert second.metadata["suppressed_after_result_contract"] is True
    assert "不要再次执行 SQL" in second.result_for_llm
    assert usage.sql_calls_used == 1
    assert usage.tool_calls_used == 1
    assert usage.extra_sql_suppressed == 1
    assert usage.termination_reason == "completed"

    audits = runner.audit.list_recent(user_id, "analyst")
    assert len(audits) == 1
    assert audits[0]["policy_status"] == "allowed"


@pytest.mark.asyncio
async def test_sse_agent_middleware_removes_redundant_sql_after_real_contract(tmp_path) -> None:
    """Verify the LLM-response guard, not only the registry fallback, against PostgreSQL."""
    suffix = uuid.uuid4().hex[:12]
    user = User(id=f"contract-sse-{suffix}", group_memberships=["analyst"])
    conversation_id = f"contract-sse-conversation-{suffix}"
    settings = PostgresConnectionSettings.from_environment()

    class Resolver(UserResolver):
        async def resolve_user(self, request_context: RequestContext) -> User:
            return user

    class SequencedLlm(LlmService):
        def __init__(self) -> None:
            self.requests: list[LlmRequest] = []

        async def send_request(self, request: LlmRequest) -> LlmResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                return LlmResponse(
                    tool_calls=[
                        ToolCall(
                            id="validated-query",
                            name="run_sql",
                            arguments={
                                "sql": (
                                    "SELECT COUNT(DISTINCT order_id) AS paid_order_count "
                                    "FROM fact_orders "
                                    "WHERE order_status NOT IN ('canceled', 'unavailable')"
                                )
                            },
                        )
                    ]
                )
            return LlmResponse(
                content="这条调用会由合同守卫抑制。",
                tool_calls=[
                    ToolCall(
                        id="redundant-query",
                        name="run_sql",
                        arguments={"sql": "SELECT 1 AS paid_order_count"},
                    )
                ],
            )

        async def stream_request(self, request: LlmRequest):
            yield LlmStreamChunk(content="")

        async def validate_tools(self, tools) -> list[str]:
            return []

    store = PostgresConversationStore(settings)
    recorder = PostgresRunRecorder(settings, model_name="fixed-sequence-test")
    runner = SecurePostgresRunner(
        settings=settings,
        result_validator=ResultValidator(settings.max_rows),
        model_name="fixed-sequence-test",
    )
    registry = BudgetedToolRegistry()
    registry.register_local_tool(
        TrustedRunSqlTool(
            sql_runner=runner,
            file_system=LocalFileSystem(str(tmp_path)),
        ),
        access_groups=["analyst"],
    )
    llm = SequencedLlm()
    agent = Agent(
        llm_service=llm,
        tool_registry=registry,
        user_resolver=Resolver(),
        agent_memory=DemoAgentMemory(),
        conversation_store=store,
        llm_middlewares=[BudgetSafetyMiddleware()],
        config=AgentConfig(max_tool_iterations=4, stream_responses=False, temperature=0.0),
    )
    handler = BudgetedChatHandler(
        agent,
        RequestBudget(max_tool_iterations=4, max_tool_calls=4, max_sql_calls=2),
        recorder,
        question_router=QuestionRouter(CatalogRetriever(CatalogLoader().load())),
    )
    request = ChatRequest(
        message="统计有效订单数",
        conversation_id=conversation_id,
        request_context=RequestContext(),
    )

    try:
        chunks = [chunk async for chunk in handler.handle_stream(request)]
        assert chunks
        assert len(llm.requests) == 2

        connection = psycopg2.connect(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.writer_user,
        )
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT sql_calls_used, tool_calls_used, termination_reason, catalog_trace "
                    "FROM app.agent_runs WHERE request_id = %s",
                    (request.request_id,),
                )
                run = cursor.fetchone()
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM app.query_audits WHERE request_id = %s",
                    (request.request_id,),
                )
                audit_count = cursor.fetchone()["count"]
        finally:
            connection.close()

        assert run["sql_calls_used"] == 1
        assert run["tool_calls_used"] == 1
        assert run["termination_reason"] == "completed"
        assert run["catalog_trace"]["result_contract_satisfied"] is True
        assert run["catalog_trace"]["extra_sql_suppressed"] == 1
        assert audit_count == 1
    finally:
        connection = psycopg2.connect(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.writer_user,
        )
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM app.conversations WHERE conversation_id = %s",
                        (conversation_id,),
                    )
        finally:
            connection.close()


@pytest.mark.parametrize(
    ("role", "query"),
    [
        ("daa_analytics_reader", "SELECT * FROM app.query_audits LIMIT 1"),
        ("daa_app_writer", "SELECT * FROM analytics.fact_orders LIMIT 1"),
    ],
)
def test_database_roles_cannot_cross_schema_boundaries(role: str, query: str) -> None:
    settings = PostgresConnectionSettings.from_environment()
    connection = psycopg2.connect(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=role,
    )
    try:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cursor.execute(query)
    finally:
        connection.close()
