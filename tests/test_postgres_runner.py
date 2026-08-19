"""Opt-in integration coverage for the project-local PostgreSQL trust boundary."""

from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

from data_analysis_agent.postgres_runner import (
    PostgresConnectionSettings,
    SecurePostgresRunner,
)
from data_analysis_agent.budget import BudgetedToolRegistry, BudgetUsage, RequestBudget
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.sql_policy import PolicyViolation
from data_analysis_agent.trusted_sql_tool import TrustedRunSqlTool
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.tool import ToolCall, ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem


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
async def test_runner_executes_allowed_query_and_audits_policy_rejection() -> None:
    user_id = f"postgres-test-{uuid.uuid4().hex[:12]}"
    runner = SecurePostgresRunner()
    context = make_context(user_id)

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

    assert dataframe.iloc[0]["customer_state"] == "SP"
    assert len(dataframe) == 5

    with pytest.raises(PolicyViolation, match="only SELECT"):
        await runner.run_sql(RunSqlToolArgs(sql="DELETE FROM fact_orders"), context)

    audits = runner.audit.list_recent(user_id, "analyst")
    assert [audit["policy_status"] for audit in audits[:2]] == ["rejected", "allowed"]
    assert audits[1]["question"] == "integration test"
    assert "analytics.fact_orders" in audits[1]["final_sql"]


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
