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
    build_result_evidence, constrain_followup_answer, followup_adds_numbers,
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
    with pytest.raises(ResultInspectionError, match="不可跨组求和"):
        build_result_evidence(frame, required_columns=frame.columns, metric_ids=("positive_review_rate",),
                              inspection=ResultInspection(operation="sum", metric_id="positive_review_rate"))


def test_additivity_depends_on_catalog_dimension_not_just_metric_name():
    catalog = CatalogLoader(load_runtime_workspace().profile).load()
    assert "paid_order_count" in catalog.additive_metrics_for(("paid_order_count",), ("customer_state",))
    assert "paid_order_count" not in catalog.additive_metrics_for(("paid_order_count",), ("product_category_name",))
    assert "item_count" not in catalog.additive_metrics_for(("item_count",), ("payment_type",))
    assert "gmv" in catalog.additive_metrics_for(("gmv",), ("product_category_name",))


def test_server_sum_is_decimal_based_and_comparison_handles_zero_denominator():
    frame = pd.DataFrame({"state": ["SP", "RJ", "MG"], "gmv": [0.1, 0.2, 0.0]})
    result = payload(evidence(frame, inspection=ResultInspection(operation="sum", metric_id="gmv")))
    assert result["calculation"]["value"] == 0.3
    result = payload(evidence(frame, inspection=ResultInspection(operation="compare", metric_id="gmv", row_numbers=(1, 3))))
    assert result["calculation"]["difference_first_minus_second"] == 0.1
    assert result["calculation"]["relative_change_percent"] is None
    assert result["calculation"]["zero_denominator"] is True


def test_targeted_read_reaches_rows_outside_the_old_sample_and_matches_exact_values():
    frame = pd.DataFrame({"state": ["01", "NA", *[f"S{i}" for i in range(20)]], "gmv": range(22)})
    result = payload(evidence(frame, inspection=ResultInspection(operation="rows", row_numbers=(10, 20))))
    assert result["row_numbers"] == [10, 20]
    assert [row["gmv"] for row in result["sample_rows"]] == [9, 19]
    result = payload(evidence(frame, inspection=ResultInspection(operation="sum", metric_id="gmv", filters={"state": "NA"})))
    assert result["calculation"]["value"] == 1
    with pytest.raises(ResultInspectionError, match="超出"):
        evidence(frame, inspection=ResultInspection(row_numbers=(300,)))
    with pytest.raises(ResultInspectionError, match="列"):
        evidence(frame, inspection=ResultInspection(filters={"order_id": "secret"}))


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
        WorkingMemory().with_result_summary("长" * 8_000)


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
                            result_inspection=ResultInspection(operation="sum", metric_id="gmv"))
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        result = payload(await handler._inspect_previous_result(memory, User(id="owner"), intent))
    finally:
        CURRENT_BUDGET.reset(token)
    assert result["calculation"]["value"] == sum(range(22))
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
            return LlmResponse(content="合计成交额为30。", finish_reason="stop")

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
    assert '"sum_of_returned_rows":30' in captured[0].messages[0].content
    assert usage.catalog_trace["prompt_budgets"][-1]["purpose"] == "result_explanation"


def test_numeric_check_accepts_server_formatting_but_not_new_arithmetic():
    summary = evidence(pd.DataFrame({"state": ["SP", "RJ"], "gmv": [20.0, 10.0]}))
    assert followup_adds_numbers("SP成交额20.00，合计30。", summary) is False
    assert followup_adds_numbers("另一个未知指标为987654321。", summary) is True
    answer = constrain_followup_answer("未知指标987654321。", summary + '\n已验证查询范围：{"year":2017}')
    assert "987654321" not in answer
    assert "合计：30" in answer and "sample_rows" not in answer
