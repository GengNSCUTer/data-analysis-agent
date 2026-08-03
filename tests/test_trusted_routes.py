from __future__ import annotations

import asyncio
import os
import uuid

import psycopg2
import pytest
from fastapi.testclient import TestClient
from vanna.core.storage import Conversation, Message
from vanna.core.tool import ToolCall
from vanna.core.user import User

from data_analysis_agent.conversation_store import PostgresConversationStore
from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from examples.trusted_olist_web_demo import create_app


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def require_project_database() -> None:
    if os.getenv("RUN_PROJECT_DB") != "1":
        pytest.skip(
            "set RUN_PROJECT_DB=1 to run against the project-local PostgreSQL instance"
        )


@pytest.fixture
def settings() -> PostgresConnectionSettings:
    return PostgresConnectionSettings.from_environment()


def test_conversation_routes_scope_users_and_redact_tool_content(
    settings: PostgresConnectionSettings,
) -> None:
    conversation_id = f"route-test-{uuid.uuid4().hex[:12]}"
    owner = User(id="demo-analyst", group_memberships=["analyst"])
    store = PostgresConversationStore(settings)
    asyncio.run(
        store.update_conversation(
            Conversation(
                id=conversation_id,
                user=owner,
                messages=[
                    Message(role="user", content="看订单"),
                    Message(
                        role="assistant",
                        content="查询中",
                        tool_calls=[
                            ToolCall(
                                id="secret-call",
                                name="run_sql",
                                arguments={"sql": "SELECT sensitive_value"},
                            )
                        ],
                    ),
                    Message(
                        role="tool",
                        content="sensitive database output",
                        tool_call_id="secret-call",
                    ),
                ],
            )
        )
    )

    try:
        with TestClient(create_app()) as client:
            listing = client.get("/api/project/conversations")
            assert listing.status_code == 200
            assert any(
                item["conversation_id"] == conversation_id for item in listing.json()
            )

            detail = client.get(f"/api/project/conversations/{conversation_id}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["messages"][-1]["content"] == "工具结果已记录"
            assert "sensitive database output" not in detail.text
            assert "sensitive_value" not in detail.text

            malformed = client.get("/api/project/conversations/../secret")
            assert malformed.status_code in {400, 404}

            switched = client.post("/api/project/demo-session", json={"role": "admin"})
            assert switched.status_code == 200
            assert (
                client.get(f"/api/project/conversations/{conversation_id}").status_code
                == 404
            )
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
