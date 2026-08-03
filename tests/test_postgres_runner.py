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
from data_analysis_agent.sql_policy import PolicyViolation
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory


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
