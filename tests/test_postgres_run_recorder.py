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
        usage.finish()
        await recorder.finish(run, usage)

        context = ToolContext(
            user=user,
            conversation_id=conversation_id,
            request_id=request_id,
            agent_memory=DemoAgentMemory(),
            metadata={"question": "统计订单数", "run_id": run.run_id},
        )
        await SecurePostgresRunner(settings, model_name="test-model").run_sql(
            RunSqlToolArgs(
                sql="SELECT COUNT(*) AS order_count FROM fact_orders LIMIT 1"
            ),
            context,
        )

        connection = psycopg2.connect(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.writer_user,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT termination_reason, tool_calls_used, finished_at "
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
        assert run_row[2] is not None
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
