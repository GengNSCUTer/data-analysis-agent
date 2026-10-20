from datetime import datetime, timezone

import pytest

from data_analysis_agent.execution_memory import ExecutionMemory, reuse_decision


def _scope(**overrides):
    value = {
        "workspace_id": "olist-demo",
        "dataset_version": "olist-v1",
        "metric_version": "metric-v1",
        "metric_ids": ["gmv"],
        "dimensions": ["customer_state"],
        "filters": [],
        "time_range": {"start": "2017-01-01", "end": "2018-01-01"},
        "time_grain": None,
        "comparison_baseline": None,
        "top_n": None,
        "result_shape": "grouped",
    }
    value.update(overrides)
    return value


def test_execution_memory_hides_sql_and_exposes_validation_and_artifact():
    item = ExecutionMemory.from_row(
        {
            "audit_id": 7,
            "run_id": "run-7",
            "conversation_id": "conversation-7",
            "policy_status": "allowed",
            "policy_reason": None,
            "model_name": "test-model",
            "dataset_version_id": "olist-v1",
            "metric_version": "metric-v1",
            "elapsed_ms": 22,
            "row_count": 27,
            "error_message": None,
            "termination_reason": "completed",
            "catalog_trace": {
                "execution_attempts": {"7": {"validation_status": "valid", "artifact_id": "rta-7"}},
                "result_contract_satisfied": True,
                "analysis_state": {"task_id": "task-1", "turn_id": "turn-1", "state_version": 3},
                "query_plan": {"query_plan_version": "query-plan-v2", "execution_strategy": "aggregate"},
                "result_artifact": {"artifact_id": "rta-7"},
            },
            "started_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    )
    evidence = item.as_dict()
    assert evidence["execution_id"] == "audit-7"
    assert evidence["validation_status"] == "valid"
    assert evidence["artifact_id"] == "rta-7"
    assert "sql" not in str(evidence).lower()


def test_execution_memory_marks_rejected_candidates_unvalidated():
    item = ExecutionMemory.from_row(
        {
            "audit_id": 8,
            "conversation_id": "conversation-8",
            "policy_status": "rejected",
            "policy_reason": "对象不在白名单",
            "termination_reason": "execution_error",
            "error_type": "sql_policy",
            "catalog_trace": {},
        }
    )
    assert item.validation_status == "not_validated"
    assert item.failure_reason == "policy_rejected"


def test_reuse_requires_identical_governed_scope():
    scope = _scope()
    assert reuse_decision(artifact_scope=scope, requested_scope=scope, artifact_id="rta-1", validation_status="valid") == "validated_result"
    assert reuse_decision(artifact_scope=scope, requested_scope=scope, artifact_id="rta-1", validation_status="valid", presentation_only=True) == "display_only"
    assert reuse_decision(
        artifact_scope=scope,
        requested_scope=_scope(dimensions=["product_category_name"]),
        artifact_id="rta-1",
        validation_status="valid",
    ) == "query_required"
    assert reuse_decision(artifact_scope=scope, requested_scope=scope, artifact_id=None, validation_status="valid") == "query_required"


@pytest.mark.parametrize("change", [
    {"workspace_id": "other"}, {"dataset_version": "new-data"}, {"metric_version": "new-metric"},
    {"metric_ids": ["paid_order_count"]}, {"filters": [{"dimension": "customer_state", "value": "SP"}]},
    {"time_range": None}, {"time_grain": "month"}, {"comparison_baseline": "previous_period"},
    {"top_n": 5}, {"result_shape": "scalar"},
])
def test_changed_scope_requires_query(change):
    assert reuse_decision(artifact_scope=_scope(), requested_scope=_scope(**change), artifact_id="rta", validation_status="valid") == "query_required"


def test_missing_or_unvalidated_scope_cannot_reuse():
    assert reuse_decision(artifact_scope={}, requested_scope={}, artifact_id="rta", validation_status="valid") == "query_required"
    assert reuse_decision(artifact_scope=_scope(), requested_scope=_scope(), artifact_id="rta", validation_status="degraded") == "query_required"


def test_successful_run_does_not_validate_other_attempts_or_expose_driver_error():
    trace = {"result_contract_satisfied": True, "result_artifact": {"artifact_id": "rta-success"},
             "execution_attempts": {"2": {"validation_status": "valid", "artifact_id": "rta-success"}}}
    failed = ExecutionMemory.from_row({"audit_id": 1, "policy_status": "execution_error", "catalog_trace": trace,
                                       "error_message": "SELECT secret FROM private_table", "termination_reason": "completed"})
    success = ExecutionMemory.from_row({"audit_id": 2, "policy_status": "allowed", "catalog_trace": trace})
    assert failed.execution_status == "failed"
    assert failed.validation_status == "not_validated" and failed.artifact_id is None
    assert "secret" not in str(failed.as_dict())
    assert success.validation_status == "valid" and success.artifact_id == "rta-success"


@pytest.mark.asyncio
async def test_runtime_reuse_checks_scope_owner_versions_and_payload(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace, MethodType

    from vanna.core.user import User
    from data_analysis_agent.analysis_state import AnalysisState
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.metric_context import OLIST_WORKSPACE
    from data_analysis_agent.result_artifact import ResultArtifact
    from data_analysis_agent.result_artifact_store import ResultArtifactStore

    store = ResultArtifactStore(tmp_path)
    state = replace(AnalysisState.new(OLIST_WORKSPACE), metric_ids=("gmv",), dimensions=("customer_state",), result_shape="grouped")
    scope = {**state.scope(), "workspace_id": state.workspace_id, "dataset_version": state.dataset_version,
             "metric_version": state.metric_version, "result_shape": state.result_shape}
    manifest = store.save_result_csv(
        artifact_id="rta-reuse", user_id="owner", workspace_id=state.workspace_id,
        columns=["customer_state", "gmv"], rows=[{"customer_state": "SP", "gmv": 10}], row_count=1,
        dataset_version=state.dataset_version, metric_version=state.metric_version,
        result_metadata={"validation_status": "valid"},
    )
    artifact = ResultArtifact(
        artifact_id=manifest["artifact_id"], summary="已验证结果", metric_ids=("gmv",),
        result_columns=("customer_state", "gmv"), dataset_version=state.dataset_version,
        metric_version=state.metric_version, chart_state="not_rendered", workspace_id=state.workspace_id,
        csv_sha256=manifest["csv_sha256"], query_scope=scope, result_metadata=manifest["result_metadata"],
    )
    state = state.with_result_artifact(artifact)
    handler = SimpleNamespace(workspace=OLIST_WORKSPACE, result_artifact_store=store)
    handler._verified_previous_artifact = MethodType(BudgetedChatHandler._verified_previous_artifact, handler)
    owner = User(id="owner")

    async def decide(value=state, user=owner):
        return await BudgetedChatHandler._result_reuse_decision(handler, value, user)

    assert await decide() == "validated_result"
    assert await decide(user=User(id="other")) == "query_required"
    # A failed query leaves the last successful artifact on its original scope.
    assert await decide(replace(state, dimensions=("product_category_name",))) == "query_required"
    failed = replace(state, dimensions=("product_category_name",), status="failed")
    assert await BudgetedChatHandler._result_reuse_decision(
        handler, failed, owner, target="last_successful_result",
    ) == "validated_result"
    assert failed.dimensions == ("product_category_name",) and failed.status == "failed"
    assert await BudgetedChatHandler._result_reuse_decision(
        handler, failed, User(id="other"), target="last_successful_result",
    ) == "query_required"
    assert await decide(replace(state, previous_result_artifact=replace(artifact, dataset_version="old"))) == "query_required"
    csv_path = next(tmp_path.rglob("result.csv"))
    csv_path.write_text("tampered", encoding="utf-8")
    assert await decide() == "query_required"
    csv_path.unlink()
    assert await decide() == "query_required"


@pytest.mark.asyncio
async def test_reference_reads_only_requested_rows_from_owned_full_artifact(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace, MethodType
    from unittest.mock import Mock
    from vanna.core.user import User
    from data_analysis_agent.analysis_state import AnalysisState
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE as RUNTIME_WORKSPACE
    from data_analysis_agent.result_artifact import ResultArtifact
    from data_analysis_agent.result_artifact_store import ResultArtifactStore
    from data_analysis_agent.result_reference import ResultReferenceError
    from data_analysis_agent.semantic_intent import ReferenceRequest

    store = ResultArtifactStore(tmp_path)
    manifest = store.save_result_csv(
        artifact_id="full-ranking", user_id="owner", workspace_id=RUNTIME_WORKSPACE.workspace_id,
        dataset_version=RUNTIME_WORKSPACE.dataset_version_id, metric_version=RUNTIME_WORKSPACE.metric_version,
        columns=["product_category_name", "gmv"], row_count=10,
        rows=[{"product_category_name": f"category-{i}", "gmv": 100-i} for i in range(1, 11)],
        result_metadata={"validation_status": "valid", "ordering": [{"column": "gmv", "direction": "desc"}]},
    )
    artifact = ResultArtifact("full-ranking", "摘要只有八行", ("gmv",), ("product_category_name", "gmv"),
        RUNTIME_WORKSPACE.dataset_version_id, RUNTIME_WORKSPACE.metric_version, "not_rendered", workspace_id=RUNTIME_WORKSPACE.workspace_id,
        query_scope={"dimensions": ["product_category_name"]}, csv_sha256=manifest["csv_sha256"],
        result_metadata=manifest["result_metadata"])
    state = replace(AnalysisState.new(RUNTIME_WORKSPACE), top_n=5, status="failed", previous_result_artifact=artifact)
    handler = SimpleNamespace(workspace=RUNTIME_WORKSPACE, result_artifact_store=store)
    handler._verified_previous_artifact = MethodType(BudgetedChatHandler._verified_previous_artifact, handler)
    store.read_csv_page = Mock(wraps=store.read_csv_page)
    owner = User(id="owner")
    request = ReferenceRequest(kind="previous_ranked_entity", dimension="product_category_name",
                               rank=10, match_mode="first_row")
    binding = await BudgetedChatHandler._resolve_result_reference(handler, state, request, owner)
    assert binding.value == "category-10" and state.top_n == 5 and state.status == "failed"
    assert store.read_csv_page.call_args.kwargs["cursor"] == 9
    assert store.read_csv_page.call_args.kwargs["page_size"] == 1
    highest = request.model_copy(update={"rank": 1, "match_mode": "unique_highest"})
    assert (await BudgetedChatHandler._resolve_result_reference(handler, state, highest, owner)).value == "category-1"
    assert store.read_csv_page.call_args.kwargs["page_size"] == 2
    with pytest.raises(ResultReferenceError, match="排名行"):
        await BudgetedChatHandler._resolve_result_reference(handler, state, request.model_copy(update={"rank": 11}), owner)
    store.read_csv_page.reset_mock()
    with pytest.raises(ResultReferenceError, match="失效"):
        await BudgetedChatHandler._resolve_result_reference(handler, state, request, User(id="other"))
    store.read_csv_page.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("chart_type,rows,accepted", [
    ("donut", [{"customer_state": "SP", "gmv": 10}, {"customer_state": "RJ", "gmv": 5}], True),
    ("horizontal_bar", [{"customer_state": "SP", "gmv": 10}], True),
    ("line", [{"customer_state": "SP", "gmv": 10}], False),
    ("donut", [], False),
    ("bar", [{"customer_state": "SP", "gmv": 1}] * 201, False),
])
async def test_chart_only_followup_uses_bounded_verified_csv(tmp_path, chart_type, rows, accepted):
    from dataclasses import replace
    from types import SimpleNamespace, MethodType
    from vanna.core.user import User
    from data_analysis_agent.analysis_state import AnalysisState
    from data_analysis_agent.budget import BudgetUsage, RequestBudget
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.runtime_workspace import load_runtime_workspace
    from data_analysis_agent.question_router import QuestionRouter
    from data_analysis_agent.result_artifact import ResultArtifact
    from data_analysis_agent.result_artifact_store import ResultArtifactStore
    from data_analysis_agent.result_budget import ResultBudget
    from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
    from data_analysis_agent.semantic_intent import SemanticIntent

    workspace = load_runtime_workspace().profile
    store = ResultArtifactStore(tmp_path)
    manifest = store.save_result_csv(
        artifact_id="rta-chart", user_id="owner", workspace_id=workspace.workspace_id,
        columns=["customer_state", "gmv"], rows=rows, row_count=len(rows),
        dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version,
        result_metadata={"validation_status": "valid"},
    )
    state = replace(AnalysisState.new(workspace), metric_ids=("gmv",), dimensions=("customer_state",),
                    filters=("customer_city=sao paulo",), result_shape="grouped")
    artifact = ResultArtifact(
        artifact_id="rta-chart", summary="可信摘要", metric_ids=("gmv",),
        result_columns=("customer_state", "gmv"), workspace_id=workspace.workspace_id,
        dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version,
        chart_state="not_rendered", csv_sha256=manifest["csv_sha256"],
        query_scope={**state.scope(), "workspace_id": workspace.workspace_id,
                     "dataset_version": workspace.dataset_version_id,
                     "metric_version": workspace.metric_version, "result_shape": "grouped"},
        result_metadata=manifest["result_metadata"],
    )
    state = state.with_result_artifact(artifact)
    handler = SimpleNamespace(
        workspace=workspace, result_artifact_store=store, result_budget=ResultBudget.aggregate(),
        question_router=QuestionRouter(CatalogRetriever(CatalogLoader(workspace).load()), full_results=True),
    )
    handler._verified_previous_artifact = MethodType(BudgetedChatHandler._verified_previous_artifact, handler)
    user = User(id="owner", group_memberships=["analyst"])
    assert await BudgetedChatHandler._result_reuse_decision(handler, state, user) == "validated_result"
    evidence = BudgetUsage(RequestBudget())
    pending = replace(state, metric_ids=("item_count",), top_n=1, status="failed")
    component, notice = await BudgetedChatHandler._chart_from_previous_result(
        handler, SimpleNamespace(message="只换图"), pending,
        SemanticIntent(request_kind="result_followup", action="keep", chart_request=chart_type), user, evidence,
    )
    assert bool(component) is accepted and notice
    assert evidence.deterministic_chart_rendered is accepted
    assert evidence.sql_calls_used == 0 and evidence.llm_rounds_used == 0
    if accepted:
        assert component.rich_component.data["data"]
        assert "gmv" in str(component.rich_component.data) and pending.metric_ids == ("item_count",)
        assert store.verify_manifest(artifact_id="rta-chart", user_id="owner", workspace_id=workspace.workspace_id)["plotly_ref"]
