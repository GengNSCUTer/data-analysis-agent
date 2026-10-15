from __future__ import annotations

import os
import uuid

import psycopg2
import pytest
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.storage import Conversation
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory

from data_analysis_agent.budget import BudgetUsage, RequestBudget
from data_analysis_agent.conversation_store import PostgresConversationStore
from data_analysis_agent.postgres_runner import (
    PostgresConnectionSettings,
    SecurePostgresRunner,
)
from data_analysis_agent.run_recorder import PostgresRunRecorder
from data_analysis_agent.sql_repair import SafeSqlExecutionError


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def require_project_database() -> None:
    if os.getenv("RUN_PROJECT_DB") != "1":
        pytest.skip(
            "set RUN_PROJECT_DB=1 to run against the project-local PostgreSQL instance"
        )


@pytest.mark.asyncio
async def test_run_recorder_links_query_audit_to_finished_run() -> None:
    suffix = uuid.uuid4().hex[:12]
    settings = PostgresConnectionSettings.from_environment()
    user = User(id=f"run-recorder-{suffix}", group_memberships=["analyst"])
    conversation_id = f"run-recorder-conversation-{suffix}"
    request_id = f"run-recorder-request-{suffix}"
    store = PostgresConversationStore(settings)
    await store.update_conversation(Conversation(id=conversation_id, user=user))

    try:
        budget = RequestBudget()
        recorder = PostgresRunRecorder(settings, model_name="test-model")
        run = await recorder.start(
            request_id=request_id,
            conversation_id=conversation_id,
            user=user,
            question="统计订单数",
            budget=budget,
            dataset_version_id="test-data",
            metric_version="test-metric",
        )
        usage = BudgetUsage(budget)
        usage.set_input("统计订单数")
        usage.record_llm_round()
        usage.consume_tool("run_sql")
        usage.record_repair(
            {
                "repair_attempted": True,
                "repair_execution_status": "succeeded",
            }
        )
        usage.mark_result_contract_satisfied()
        usage.suppress_extra_sql()
        usage.mark_deterministic_result_finalized()
        usage.record_llm_observation(
            status="completed",
            elapsed_ms=42,
            usage_status="reported",
            finish_reason="stop",
        )
        usage.finish()
        context = ToolContext(
            user=user,
            conversation_id=conversation_id,
            request_id=request_id,
            agent_memory=DemoAgentMemory(),
            metadata={"question": "统计订单数", "run_id": run.run_id, "budget_usage": usage,
                      "required_result_columns": ["order_count"], "metric_result_columns": ["order_count"]},
        )
        runner = SecurePostgresRunner(settings, model_name="test-model")
        with pytest.raises(SafeSqlExecutionError):
            await runner.run_sql(RunSqlToolArgs(sql="SELECT COUNT(*) / 0 AS order_count FROM fact_orders"), context)
        await runner.run_sql(
            RunSqlToolArgs(
                sql="SELECT COUNT(*) AS order_count FROM fact_orders LIMIT 1"
            ),
            context,
        )
        await recorder.finish(run, usage)
        attempts = await recorder.list_execution_memory(conversation_id=conversation_id, user_id=user.id)
        assert len(attempts) == 2
        assert attempts[1].execution_id == f"audit-{context.metadata['query_audit_id']}"
        assert attempts[1].execution_status == "completed"
        assert attempts[0].execution_status == "failed"
        assert attempts[0].validation_status == "not_validated"
        assert attempts[1].validation_status == "valid"
        assert not await recorder.list_execution_memory(conversation_id=conversation_id, user_id="other-user")

        connection = psycopg2.connect(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.writer_user,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT termination_reason, tool_calls_used, repair_evidence, catalog_trace, finished_at "
                    "FROM app.agent_runs WHERE run_id = %s",
                    (run.run_id,),
                )
                run_row = cursor.fetchone()
                cursor.execute(
                    "SELECT run_id, policy_status FROM app.query_audits "
                    "WHERE run_id = %s ORDER BY audit_id DESC LIMIT 1",
                    (run.run_id,),
                )
                audit_row = cursor.fetchone()
        finally:
            connection.close()

        assert run_row[0] == "completed"
        assert run_row[1] == 1
        assert run_row[2] == {
            "repair_attempted": True,
            "repair_execution_status": "succeeded",
        }
        assert run_row[3]["result_contract_satisfied"] is True
        assert run_row[3]["extra_sql_suppressed"] == 1
        assert run_row[3]["deterministic_result_finalized"] is True
        assert run_row[3]["llm_observations"] == [
            {
                "status": "completed",
                "elapsed_ms": 42,
                "usage_status": "reported",
                "finish_reason": "stop",
            }
        ]
        assert run_row[4] is not None
        assert audit_row == (run.run_id, "allowed")
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
