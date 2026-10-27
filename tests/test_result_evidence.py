from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from data_analysis_agent.analysis_state import AnalysisState
from data_analysis_agent.budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.llm_observability import ObservedLlmService
from data_analysis_agent.result_artifact import ResultArtifact
from data_analysis_agent.result_artifact_store import ResultArtifactIntegrityError, ResultArtifactStore
from data_analysis_agent.result_evidence import (
    RESULT_EVIDENCE_PREFIX, ResultInspection, ResultInspectionError,
    build_result_evidence, resize_result_evidence,
)
from data_analysis_agent.runtime_workspace import load_runtime_workspace
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.semantic_intent import SemanticIntent
from data_analysis_agent.token_budget import DEFAULT_TOKEN_COUNTER, ContextCapacityError
from data_analysis_agent.working_memory import WorkingMemory
from vanna.core.llm import LlmMessage, LlmRequest, LlmResponse, LlmService
from vanna.core.user import User


def payload(summary):
    return json.loads(summary.removeprefix(RESULT_EVIDENCE_PREFIX))


def evidence(frame, **kwargs):
    return build_result_evidence(frame, required_columns=tuple(frame.columns), metric_ids=("gmv",),
                                 additive_metric_ids=("gmv",), **kwargs)


@pytest.mark.parametrize("count", [10, 12, 27])
def test_small_results_are_complete_and_last_row_is_not_lost(count):
    frame = pd.DataFrame({"category": [f"类别{i}" for i in range(count)], "gmv": range(count)})
    summary = evidence(frame)
    result = payload(summary)
    assert result["coverage"] == "complete"
    assert result["included_row_count"] == count
    assert result["sample_rows"][-1]["category"] == f"类别{count - 1}"
    assert DEFAULT_TOKEN_COUNTER.count_text(summary) <= 4_000


def test_large_result_has_full_statistics_and_year_end_peak_not_just_a_prefix():
    frame = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=366),
                          "gmv": [1] * 365 + [9_876]})
    result = payload(evidence(frame, max_tokens=1_500, time_column="time"))
    assert result["coverage"] == "selected_rows"
    assert result["included_row_count"] < len(frame)
    assert result["statistics"]["gmv"]["sum_of_returned_rows"] == 10_241
    assert result["statistics"]["gmv"]["maximum_row"]["time"].startswith("2024-12-31")
    assert result["time_endpoints"]["last"]["gmv"] == 9_876


def test_actual_request_resizes_rows_but_keeps_full_population_statistics():
    frame = pd.DataFrame({"category":[f"类别{i}" for i in range(250)],"gmv":range(250)})
    summary = evidence(frame)
    assert DEFAULT_TOKEN_COUNTER.count_text(summary) > 4000
    usage = BudgetUsage(RequestBudget(max_prompt_tokens=6000, max_history_tokens=4000, max_output_tokens=1000))
    token = CURRENT_BUDGET.set(usage)
    try:
        request = LlmRequest(messages=[LlmMessage(role="user",content="历史背景"*300 + summary)],
                             user=User(id="budget"),max_tokens=1000,metadata={"purpose":"tool_free_response"})
        service = ObservedLlmService(None)
        fitted = service._fit_result_request(request)
        service._check_context(fitted)
    finally:
        CURRENT_BUDGET.reset(token)
    result = json.loads(fitted.messages[0].content.split(RESULT_EVIDENCE_PREFIX)[1])
    assert result["source_row_count"] == 250 and result["included_row_count"] < 250
    assert result["statistics"]["gmv"]["sum_of_returned_rows"] == sum(range(250))
    assert request.messages[0].content.endswith(summary)
    assert usage.catalog_trace["result_evidence_view"]["included_row_count"] == result["included_row_count"]


def test_large_calculation_detail_is_paged_without_changing_computed_count():
    frame = pd.DataFrame({"category":[f"类别{i}" for i in range(250)],"gmv":range(250)})
    result = payload(evidence(frame, inspections=[ResultInspection(operation="threshold", metric_id="gmv", threshold_value=1000, limit=250)],max_tokens=2000))
    assert result["analyses"][0]["match_count"] == 250
    assert result["analyses"][0]["detail_items_omitted"] > 0
    assert result["source_row_count"] == 250
    resized = resize_result_evidence(evidence(frame), 2000)
    assert DEFAULT_TOKEN_COUNTER.count_text(resized) <= 2000


def test_restoration_uses_current_budget_without_breaking_a_saved_result(monkeypatch):
    summary = evidence(pd.DataFrame({"category":[f"类别{i}" for i in range(250)], "gmv":range(250)}))
    monkeypatch.setenv("DATA_ANALYSIS_MAX_RESULT_EVIDENCE_TOKENS", "2000")
    restored = WorkingMemory.from_mapping({"previous_result_summary":summary})
    data = payload(restored.previous_result_summary)
    assert DEFAULT_TOKEN_COUNTER.count_text(restored.previous_result_summary) <= 2000
    assert data["source_row_count"] == 250
    assert data["statistics"]["gmv"]["sum_of_returned_rows"] == sum(range(250))


def test_accepted_time_alias_still_has_full_sequence_endpoints():
    frame = pd.DataFrame({"month": pd.date_range("2017-01-01", periods=12, freq="MS"), "gmv": range(12)})
    result = payload(build_result_evidence(frame, required_columns=("time", "gmv"), metric_ids=("gmv",),
                                          column_aliases={"time": ("month",)}, time_column="time"))
    assert result["time_endpoints"]["last"]["month"].startswith("2017-12")
    assert result["coverage"] == "complete"


def test_only_allowed_columns_and_top_n_scope_are_exposed():
    summary = build_result_evidence(
        pd.DataFrame({"state": ["SP", "RJ"], "gmv": [20, 10], "order_id": ["secret-a", "secret-b"]}),
        required_columns=("state", "gmv"), metric_ids=("gmv",), additive_metric_ids=("gmv",), top_n=2,
    )
    result = payload(summary)
    assert "secret" not in summary and "order_id" not in summary
    assert result["scope"] == "returned_top_n"
    assert result["statistics"]["gmv"]["sum_of_returned_rows"] == 30


def test_non_additive_metrics_never_get_group_sum_or_average():
    frame = pd.DataFrame({"state": ["SP", "RJ"], "positive_review_rate": [0.9, 0.5]})
    result = payload(build_result_evidence(frame, required_columns=frame.columns,
                                          metric_ids=("positive_review_rate",)))
    assert result["statistics"]["positive_review_rate"]["sum_allowed"] is False
    assert "sum_of_returned_rows" not in result["statistics"]["positive_review_rate"]
    calculated = payload(build_result_evidence(frame, required_columns=frame.columns,
                         metric_ids=("positive_review_rate",), inspections=[
                         ResultInspection(operation="sum", metric_id="positive_review_rate")]))
    assert calculated["analyses"][0]["status"] == "query_required"
    assert "不可跨组求和" in calculated["analyses"][0]["reason"]


def test_additivity_depends_on_catalog_dimension_not_just_metric_name():
    catalog = CatalogLoader(load_runtime_workspace().profile).load()
    assert "paid_order_count" in catalog.additive_metrics_for(("paid_order_count",), ("customer_state",))
    assert "paid_order_count" not in catalog.additive_metrics_for(("paid_order_count",), ("product_category_name",))
    assert "item_count" not in catalog.additive_metrics_for(("item_count",), ("payment_type",))
    assert "gmv" in catalog.additive_metrics_for(("gmv",), ("product_category_name",))


def test_server_sum_is_decimal_based_and_comparison_handles_zero_denominator():
    frame = pd.DataFrame({"state": ["SP", "RJ", "MG"], "gmv": [0.1, 0.2, 0.0]})
    result = payload(evidence(frame, inspections=[ResultInspection(operation="sum", metric_id="gmv")]))
    assert result["analyses"][0]["value"] == 0.3
    result = payload(evidence(frame, inspections=[ResultInspection(operation="compare", metric_id="gmv", row_numbers=(1, 3))]))
    assert result["analyses"][0]["difference"] == 0.1
    assert result["analyses"][0]["relative_change_percent"] is None
    assert result["analyses"][0]["baseline_status"] == "zero"


def test_targeted_read_reaches_rows_outside_the_old_sample_and_matches_exact_values():
    frame = pd.DataFrame({"state": ["01", "NA", *[f"S{i}" for i in range(20)]], "gmv": range(22)})
    result = payload(evidence(frame, inspections=[ResultInspection(operation="rows", row_numbers=(10, 20))]))
    assert result["row_numbers"] == [10, 20]
    assert [row["gmv"] for row in result["sample_rows"]] == [9, 19]
    result = payload(evidence(frame, inspections=[ResultInspection(operation="sum", metric_id="gmv", filters={"state": "NA"})]))
    assert result["analyses"][0]["value"] == 1
    with pytest.raises(ResultInspectionError, match="超出"):
        evidence(frame, inspections=[ResultInspection(row_numbers=(300,))])
    with pytest.raises(ResultInspectionError, match="列"):
        evidence(frame, inspections=[ResultInspection(filters={"order_id": "secret"})])


def test_refresh_preserves_structured_evidence_larger_than_old_character_limit():
    frame = pd.DataFrame({"category": [f"类别{i}" for i in range(27)], "gmv": range(27)})
    summary = evidence(frame)
    assert len(summary) > 1_200
    usage = BudgetUsage(RequestBudget())
    usage.set_result_summary(summary)
    artifact = ResultArtifact.from_trusted_usage(request_id="evidence", summary=usage.result_summary,
                                                catalog_trace={}, chart_rendered=False)
    restored = WorkingMemory.from_mapping(WorkingMemory().with_result_artifact(artifact).as_dict())
    assert restored.previous_result_artifact.summary == summary
    assert restored.previous_result_summary == summary
    assert payload(restored.previous_result_summary)["sample_rows"][-1]["category"] == "类别26"
    with pytest.raises(ContextCapacityError):
        WorkingMemory().with_result_summary("长" * 80_000)


@pytest.fixture
def saved_result(tmp_path):
    workspace = load_runtime_workspace().profile
    frame = pd.DataFrame({"customer_state": ["01", "NA", *[f"S{i}" for i in range(20)]], "gmv": range(22)})
    store = ResultArtifactStore(tmp_path)
    manifest = store.save_result_csv(
        artifact_id="rta_evidence", user_id="owner", workspace_id=workspace.workspace_id,
        columns=frame.columns, rows=frame.to_dict("records"), row_count=len(frame),
        dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version,
        result_metadata={"validation_status": "valid"},
    )
    artifact = ResultArtifact(
        artifact_id="rta_evidence", summary=evidence(frame), metric_ids=("gmv",),
        result_columns=tuple(frame.columns), dataset_version=workspace.dataset_version_id,
        metric_version=workspace.metric_version, chart_state="not_rendered",
        workspace_id=workspace.workspace_id, csv_sha256=manifest["csv_sha256"],
        csv_ref="result.csv", query_scope={"dimensions": ["customer_state"], "time_grain": None, "top_n": None},
        result_metadata={"validation_status": "valid"},
    )
    return workspace, store, artifact


@pytest.mark.parametrize("mutation", ["user", "workspace", "version", "tamper"])
def test_result_calculation_preserves_ownership_versions_and_integrity(saved_result, mutation):
    workspace, store, artifact = saved_result
    args = dict(artifact_id=artifact.artifact_id, user_id="owner", workspace_id=workspace.workspace_id,
                dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version,
                dimensions=("customer_state",))
    frame = store.read_result_frame(**args)
    assert frame["customer_state"].tolist()[:2] == ["01", "NA"]
    if mutation == "user":
        args["user_id"] = "other"
    elif mutation == "workspace":
        args["workspace_id"] = "other"
    elif mutation == "version":
        args["dataset_version"] = "old"
    else:
        (store._scope("owner", workspace.workspace_id, artifact.artifact_id) / "result.csv").write_text("tampered")
    with pytest.raises((FileNotFoundError, ResultArtifactIntegrityError)):
        store.read_result_frame(**args)


@pytest.mark.asyncio
async def test_followup_reads_complete_artifact_without_sql_or_task_mutation(saved_result):
    workspace, store, artifact = saved_result
    catalog = CatalogLoader(workspace).load()
    handler = BudgetedChatHandler(SimpleNamespace(), RequestBudget(), None,
                                 question_router=QuestionRouter(CatalogRetriever(catalog)),
                                 workspace=workspace, result_artifact_store=store)
    memory = AnalysisState(previous_result_artifact=artifact, previous_result_summary=artifact.summary,
                           filters=("customer_state=SP",))
    intent = SemanticIntent(request_kind="result_followup", action="keep",
                            result_inspections=(ResultInspection(operation="sum", metric_id="gmv"),))
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        result = payload(await handler._inspect_previous_result(memory, User(id="owner"), intent))
    finally:
        CURRENT_BUDGET.reset(token)
    assert result["analyses"][0]["value"] == sum(range(22))
    assert usage.sql_calls_used == 0
    assert memory.filters == ("customer_state=SP",)
    assert usage.catalog_trace["result_inspection"]["source_row_count"] == 22
    with pytest.raises(ResultInspectionError, match="失效"):
        await handler._inspect_previous_result(memory, User(id="other"), intent)
    with pytest.raises(ResultInspectionError, match="失效"):
        await handler._inspect_previous_result(replace(memory, previous_result_artifact=replace(artifact, metric_version="old")), User(id="owner"), intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_first_explanation_calls_model_with_only_server_evidence_and_no_tools(stream):
    captured = []

    class Explainer(LlmService):
        async def send_request(self, request):
            captured.append(request)
            return LlmResponse(content='{"paragraphs":[{"text":"合计成交额为30。","kind":"fact","evidence_refs":["/statistics/gmv/sum_of_returned_rows"]}]}', finish_reason="stop")

        async def stream_request(self, request):
            raise AssertionError("explanation is buffered until its numbers are checked")
            yield

        async def validate_tools(self, tools):
            return []

    usage = BudgetUsage(RequestBudget())
    usage.mark_result_contract_satisfied()
    usage.set_result_summary(evidence(pd.DataFrame({"state": ["SP", "RJ"], "gmv": [20, 10]})))
    usage.catalog_question = "说明各州销售额和合计"
    service = ObservedLlmService(Explainer())
    request = LlmRequest(user=User(id="owner"), messages=[LlmMessage(role="user", content="untrusted history")])
    token = CURRENT_BUDGET.set(usage)
    try:
        if stream:
            chunks = [chunk async for chunk in service.stream_request(request)]
            answer = chunks[-1].content
        else:
            answer = (await service.send_request(request)).content
    finally:
        CURRENT_BUDGET.reset(token)
    assert answer == "合计成交额为30。"
    assert len(captured) == 1 and captured[0].tools is None
    assert "untrusted history" not in captured[0].messages[0].content
    assert '"sum_of_returned_rows":30' in captured[0].messages[-1].content
    assert usage.catalog_trace["prompt_budgets"][-1]["purpose"] == "result_answer_draft"


@pytest.mark.asyncio
async def test_computed_trend_uses_the_same_model_answer_and_optional_review():
    captured = []
    text = "四月比三月下降，不能说每个月都在增长。建议核对当月商品结构。"
    class Explainer(LlmService):
        async def send_request(self, request):
            captured.append(request)
            if request.metadata["purpose"] == "result_answer_review":
                return LlmResponse(content='{"verdict":"supported","issues":[]}',finish_reason="stop")
            return LlmResponse(content=json.dumps({"paragraphs":[{"text":text,"kind":"fact","evidence_refs":["/analyses/0"]}]}),finish_reason="stop")
        async def stream_request(self, request):
            raise AssertionError("unchecked draft must not stream")
            yield
        async def validate_tools(self, tools):
            return []
    usage = BudgetUsage(RequestBudget())
    usage.mark_result_contract_satisfied()
    usage.set_result_summary(evidence(pd.DataFrame({"time":pd.date_range("2017-03-01",periods=2,freq="MS"),"gmv":[120,110]}),
        time_column="time",query_scope={"time_grain":"month"}))
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(Explainer()).send_request(LlmRequest(user=User(id="owner"),messages=[]))
    finally:
        CURRENT_BUDGET.reset(token)
    assert response.content == text
    assert [r.metadata["purpose"] for r in captured] == ["result_answer_draft","result_answer_review"]
    assert '"operation":"trend"' in captured[0].messages[-1].content
    assert usage.llm_rounds_used == 2 and usage.catalog_trace["result_explanation"]["revision_count"] == 0
