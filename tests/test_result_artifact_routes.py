"""Real authenticated routes, conversation ownership and complete CSV downloads."""
import asyncio
import csv
import io
import os
import uuid

from fastapi.testclient import TestClient
import pytest

from data_analysis_agent.conversation_store import PostgresConversationStore
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.result_artifact import ResultArtifact
from data_analysis_agent.result_artifact_store import ResultArtifactStore
from vanna.core.storage import Conversation
from vanna.core.user import User


@pytest.mark.postgres
@pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="needs project PostgreSQL")
def test_pages_download_and_scope_use_the_same_verified_artifact(monkeypatch, tmp_path):
    from data_analysis_agent import server as demo

    username = "budgetroutes"
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_USERNAME", username)
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD", "budget-route-test-password")
    monkeypatch.setattr(demo, "RESULT_ARTIFACT_DIRECTORY", tmp_path)
    store = ResultArtifactStore(tmp_path)
    conversations = PostgresConversationStore()
    workspace = OLIST_RUNTIME_WORKSPACE
    artifact = ResultArtifact(
        artifact_id="budget-route-test", summary="已验证统计", metric_ids=("paid_order_count",),
        result_columns=("customer_city", "paid_order_count"),
        dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version,
        workspace_id=workspace.workspace_id, chart_state="not_rendered",
    )
    with TestClient(demo.create_app()) as client:
        login = client.post("/api/auth/login", json={"username": username, "password": "budget-route-test-password"})
        assert login.status_code == 200
        user = User(id="local:" + username, group_memberships=["analyst"])
        conversation = Conversation(
            id="budget-routes-" + uuid.uuid4().hex, user=user, messages=[],
            metadata={"workspace_id": workspace.workspace_id, "dataset_version_id": workspace.dataset_version_id,
                      "metric_version": workspace.metric_version, "working_memory": {"previous_result_artifact": artifact.as_dict()}},
        )
        scope = dict(artifact_id=artifact.artifact_id, user_id=user.id, workspace_id=workspace.workspace_id)
        rows = [{"customer_city": "城市" + str(n), "paid_order_count": n} for n in range(1205)]
        store.save_result_csv(**scope, columns=artifact.result_columns, rows=rows, row_count=len(rows), dataset_version=artifact.dataset_version, metric_version=artifact.metric_version)
        asyncio.run(conversations.update_conversation(conversation))
        route = f"/api/project/conversations/{conversation.id}/artifacts/{artifact.artifact_id}/rows"
        try:
            first = client.get(route)
            assert first.status_code == 200, first.text
            assert first.json()["page_rows"] == 200 and first.json()["total_rows"] == 1205
            last = client.get(route, params={"cursor": 1200})
            assert last.status_code == 200 and last.json()["page_rows"] == 5
            assert not last.json()["has_next_page"]
            assert client.get(route, params={"page_size": 501}).status_code == 422
            downloaded = client.get(f"/api/project/conversations/{conversation.id}/result.csv")
            assert downloaded.status_code == 200 and downloaded.headers["x-row-count"] == "1205"
            assert len(list(csv.DictReader(io.StringIO(downloaded.content.decode("utf-8"))))) == 1205
            # Own login cannot retrieve another owner's stored conversation.
            alien = conversation.model_copy(deep=True, update={"id": "alien-" + uuid.uuid4().hex, "user": User(id="other-owner", group_memberships=["analyst"])})
            alien.metadata.pop("state_version", None)
            asyncio.run(conversations.update_conversation(alien))
            try:
                assert client.get(route.replace(conversation.id, alien.id)).status_code == 404
            finally:
                asyncio.run(conversations.delete_conversation(alien.id, alien.user))
            conversation.metadata["metric_version"] = "stale-version"
            asyncio.run(conversations.update_conversation(conversation))
            assert client.get(route).status_code == 404
            conversation.metadata["metric_version"] = workspace.metric_version
            asyncio.run(conversations.update_conversation(conversation))
            (store._scope(user.id, workspace.workspace_id, artifact.artifact_id) / "result.csv").write_text("tampered", encoding="utf-8")
            assert client.get(route).status_code == 409
            assert client.get(f"/api/project/conversations/{conversation.id}/result.csv").status_code == 409
            client.cookies.clear()
            assert client.get(route).status_code == 401
        finally:
            asyncio.run(conversations.delete_conversation(conversation.id, user))
