"""User resource settings remain separate from permissions and model evidence."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from pydantic import ValidationError

from data_analysis_agent.result_budget import ResultBudget, ResultSettings
from data_analysis_agent.user_memory import UserMemoryStore


def test_defaults_and_operator_ceiling_do_not_expand_model_or_execution_resources():
    assert ResultBudget().execution_max_rows == 5_000
    assert ResultBudget.aggregate().execution_max_rows == 5_000
    ceiling = ResultBudget.aggregate(execution_max_rows=100_000)
    default = ResultSettings().to_budget(ceiling)
    assert default.execution_max_rows == 5_000 and default.preview_rows == 200
    chosen = ResultSettings(execution_max_rows=6_000, preview_rows=500).to_budget(ceiling)
    assert chosen.execution_max_rows == chosen.aggregate_result_rows == 6_000
    assert chosen.preview_rows == 500
    assert (chosen.llm_context_rows, chosen.llm_context_chars) == (8, 1200)
    assert chosen.result_max_bytes == ceiling.result_max_bytes
    assert chosen.statement_timeout_ms == ceiling.statement_timeout_ms
    reduced = ResultSettings(execution_max_rows=6_000, preview_rows=500).to_budget(
        ResultBudget.aggregate(execution_max_rows=100))
    assert reduced.execution_max_rows == reduced.preview_rows == 100


@pytest.mark.parametrize("values", [
    {"execution_max_rows": 0}, {"execution_max_rows": True},
    {"execution_max_rows": "6000"}, {"execution_max_rows": 5.5},
    {"preview_rows": 0}, {"preview_rows": 5_001}, {"preview_rows": False},
    {"execution_max_rows": 10, "preview_rows": 11}, {"sql": "SELECT 1"},
])
def test_invalid_user_settings_are_rejected(values):
    with pytest.raises(ValidationError):
        ResultSettings(**values)


def test_large_result_evidence_fits_history_without_expanding_it_with_ui_settings():
    from data_analysis_agent.budget import BudgetUsage, RequestBudget
    usage = BudgetUsage(RequestBudget())
    limit = usage.result_evidence_limit()
    assert limit < usage.budget.max_history_tokens
    assert usage.catalog_trace["result_evidence_budget"]["history_limit"] == 48_000
    usage.context_packet = {"recent_dialogue": "最近讨论" * 1000}
    assert usage.result_evidence_limit() < limit


def test_user_ceiling_is_used_by_semantic_preflight_and_plan_without_changing_shared_router():
    from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
    from data_analysis_agent.query_plan import QueryPlan
    from data_analysis_agent.question_router import QuestionRouter
    from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
    from data_analysis_agent.semantic_intent import SemanticIntent, route_semantic_intent, select_intent_catalog
    from vanna.core.user import User

    retriever = CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load())
    router = QuestionRouter(retriever, max_result_rows=100_000, full_results=True)
    intent = SemanticIntent(request_kind="data_query", metric_ids=("paid_order_count",),
                            dimensions=("customer_city",), confidence=0.99)
    selection = select_intent_catalog(intent, retriever, User(id="settings", group_memberships=["analyst"]))
    blocked = route_semantic_intent(intent, router, selection, max_result_rows=3_000)
    assert blocked.needs_clarification and not blocked.should_generate_sql
    assert "3,000" in blocked.clarification and "查询与展示设置" in blocked.clarification
    route = route_semantic_intent(intent, router, selection, max_result_rows=5_000)
    assert route.should_generate_sql
    plan = QueryPlan.from_selection(selection, "按城市统计有效订单数", route,
        {"semantic_intent": intent.as_dict()},
        result_budget=ResultSettings(preview_rows=500).to_budget(ResultBudget.aggregate(execution_max_rows=100_000)))
    assert plan.result_budget.execution_max_rows == 5_000
    assert plan.result_budget.preview_rows == 500
    assert plan.result_budget.aggregate_result_rows == 4119
    assert router.max_result_rows == 100_000


@pytest.mark.postgres
@pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="needs project PostgreSQL")
@pytest.mark.parametrize("row_count,ceiling,accepted", [(5000, 5000, True), (5001, 5000, False),
                                                       (5200, 5000, False), (5200, 5200, True)])
async def test_real_reader_enforces_chosen_ceiling_without_partial_success(row_count, ceiling, accepted):
    from data_analysis_agent.postgres_runner import SecurePostgresRunner
    from data_analysis_agent.result_validator import ResultValidationError
    from data_analysis_agent.result_validator import ResultValidator
    from vanna.capabilities.sql_runner import RunSqlToolArgs
    from vanna.core.tool import ToolContext
    from vanna.core.user import User
    from vanna.integrations.local.agent_memory import DemoAgentMemory

    budget = ResultBudget.aggregate(execution_max_rows=ceiling, preview_rows=500)
    context = ToolContext(user=User(id="settings-reader-test", group_memberships=["analyst"]),
        conversation_id="settings-reader-test", request_id=uuid4().hex, agent_memory=DemoAgentMemory(),
        metadata={**budget.as_metadata(), "query_plan": budget.as_metadata(),
                  "required_result_columns": ["gmv"], "metric_result_columns": ["gmv"]})
    # Read a bounded existing fixture: no new business rows or schema changes.
    sql = (f"WITH rows AS (SELECT price AS gmv FROM analytics.fact_order_items LIMIT {row_count}) "
           "SELECT gmv FROM rows")
    runner = SecurePostgresRunner(result_validator=ResultValidator())
    if not accepted:
        with pytest.raises(ResultValidationError):
            await runner.run_sql(RunSqlToolArgs(sql=sql), context)
        assert context.metadata["result_validation"]["state"] == "needs_clarification"
        return
    frame = await runner.run_sql(RunSqlToolArgs(sql=sql), context)
    assert len(frame) == row_count
    assert context.metadata["result_validation"]["state"] == "valid"
    assert context.metadata["display_result_row_budget"] == 500


@pytest.mark.postgres
@pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="needs project PostgreSQL")
def test_result_settings_are_atomic_and_scoped_by_subject_workspace_and_service_ceiling(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from data_analysis_agent import server

    subject = "settings-" + uuid4().hex
    store = UserMemoryStore()
    assert store.get_result_settings(subject, "olist-demo") == ResultSettings()
    chosen = ResultSettings(execution_max_rows=6000, preview_rows=500)
    store.put_result_settings(subject, "olist-demo", chosen)
    assert store.get_result_settings(subject, "olist-demo") == chosen
    assert store.get_result_settings(subject, "thelook-demo") == ResultSettings()
    assert store.get_result_settings(subject + "-other", "olist-demo") == ResultSettings()
    username = "settings_" + uuid4().hex[:10]
    monkeypatch.setenv("DATA_ANALYSIS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_USERNAME", username)
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD", "settings-test-password")
    with TestClient(server.create_app()) as client:
        assert client.get("/api/project/result-settings").status_code == 401
        assert client.put("/api/project/result-settings", json=chosen.model_dump()).status_code == 401
        assert client.post("/api/auth/login", json={"username": username, "password": "settings-test-password"}).is_success
        assert client.get("/api/project/result-settings").json()["execution_max_rows"] == 5000
        assert client.put("/api/project/result-settings", json=chosen.model_dump()).is_success
        assert client.get("/api/project/result-settings").json()["preview_rows"] == 500
        for values in ({"execution_max_rows": 100001, "preview_rows": 200},
                       {"execution_max_rows": 10, "preview_rows": 20},
                       {"execution_max_rows": True, "preview_rows": 1},
                       {**chosen.model_dump(), "subject_id": "other"}):
            assert client.put("/api/project/result-settings", json=values).status_code == 422
        assert client.get("/api/project/result-settings").json()["execution_max_rows"] == 6000
    # A later operational ceiling reduction applies to already saved values.
    monkeypatch.setenv("DATA_ANALYSIS_MAX_AGGREGATE_ROWS", "1000")
    with TestClient(server.create_app()) as client:
        client.post("/api/auth/login", json={"username": username, "password": "settings-test-password"})
        assert client.get("/api/project/result-settings").json()["execution_max_rows"] == 1000
        assert client.put("/api/project/result-settings", json=chosen.model_dump()).status_code == 422
