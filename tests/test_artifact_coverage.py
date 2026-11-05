from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from data_analysis_agent.artifact_coverage import CoverageProver
from data_analysis_agent.runtime_workspace import load_runtime_workspace
from data_analysis_agent.result_analysis import ResultInspection, ResultInspectionError
from data_analysis_agent.result_artifact import ResultArtifact
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.semantic_intent import RequestedAnswerSpec, SemanticIntent, SemanticIntentError, parse_semantic_intent


@pytest.fixture
def catalog():
    return CatalogLoader(load_runtime_workspace().profile).load()


def artifact(*, metrics=("gmv",), dimensions=("customer_state",), filters=(), top_n=None,
             time_grain=None, time_range=None, **metadata):
    workspace = load_runtime_workspace().profile
    return ResultArtifact(artifact_id="history-source", summary="可信结果", metric_ids=metrics,
        result_columns=(*dimensions, *(("time",) if time_grain else ()), *metrics),
        dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version,
        workspace_id=workspace.workspace_id, chart_state="not_rendered",
        query_scope={"metric_ids": list(metrics), "dimensions": list(dimensions), "filters": list(filters),
                     "time_range": time_range, "time_grain": time_grain, "top_n": top_n, "comparison_baseline": None},
        result_metadata={"validation_status":"valid", "result_complete":True,
            "population_coverage":"top_n" if top_n else "complete", "row_count":3,
            "ordering":[{"column":metrics[0], "direction":"desc"}], **metadata})


def spec(**values):
    return RequestedAnswerSpec(metric_ids=("gmv",), dimensions=("customer_state",), **values)


def test_equivalent_snapshot_can_be_reused_and_sp_filter_is_computed_without_sql(catalog):
    prover = CoverageProver(catalog)
    original = artifact()
    assert prover.prove(spec(), original).mode == "DIRECT_REUSE"
    wanted = RequestedAnswerSpec(metric_ids=("gmv",), filters=({"dimension":"customer_state", "value":"SP"},))
    decision = prover.prove(wanted, original)
    assert decision.mode == "LOCAL_COMPUTE"
    frame = pd.DataFrame({"customer_state":["SP","RJ",None], "gmv":[300,200,50]})
    observed = prover.compute(wanted, decision, frame)
    assert observed.to_dict("records") == [{"gmv":300}]
    assert len(frame) == 3


@pytest.mark.parametrize("change,reason", [
    ({"metric_ids":("paid_order_count",)}, "missing_metrics"),
    ({"dimensions":("customer_city",)}, "finer_grain_required"),
    ({"filters":[{"dimension":"customer_city","value":"sao paulo"}]}, "filter_column_not_returned"),
    ({"time_range":{"start":"2017-01-01","end":"2018-01-01"}}, "no_filterable_time_column"),
    ({"time_grain":"month"}, "finer_time_grain_required"),
    ({"comparison_baseline":"previous_year"}, "comparison_scope_changed"),
    ({"refresh":True}, "fresh_data_requested"),
    ({"top_n":2}, None),
])
def test_scope_differences_do_not_authorize_unsupported_reuse(catalog, change, reason):
    wanted = RequestedAnswerSpec.model_validate({**spec().model_dump(), **change})
    decision = CoverageProver(catalog).prove(wanted, artifact())
    if reason:
        assert decision.mode == "SQL_REQUIRED"
        assert reason in decision.missing_requirements
    else:
        assert decision.mode == "LOCAL_COMPUTE"


@pytest.mark.parametrize("metadata", [{"validation_status":"degraded"}, {"result_complete":False}, {"result_complete":None}])
def test_unknown_or_incomplete_results_cannot_enter_reuse(catalog, metadata):
    assert CoverageProver(catalog).prove(spec(), artifact(**metadata)).mode == "SQL_REQUIRED"


def test_source_filtered_to_sp_cannot_answer_all_states(catalog):
    original = artifact(filters=({"dimension":"customer_state","value":"SP"},))
    assert "source_population_is_narrower" in CoverageProver(catalog).prove(spec(), original).missing_requirements


def test_conflicting_filters_are_not_collapsed_to_one(catalog):
    wanted = spec(filters=[{"dimension":"customer_state","value":"SP"}, {"dimension":"customer_state","value":"RJ"}])
    assert "duplicate_filter_dimension" in CoverageProver(catalog).prove(wanted, artifact()).missing_requirements


def test_top_ten_does_not_prove_total_and_cannot_answer_twenty(catalog):
    prover = CoverageProver(catalog)
    source = artifact(top_n=10)
    total = RequestedAnswerSpec(metric_ids=("gmv",), dimensions=())
    assert "top_n_does_not_cover_population" in prover.prove(total, source).missing_requirements
    assert prover.prove(spec(top_n=20), source).mode == "SQL_REQUIRED"
    wanted = spec(top_n=10, requires_full_population=False)
    comparison = ResultInspection(operation="compare", metric_id="gmv", row_numbers=(1,2))
    assert prover.prove(wanted, source, inspections=(comparison,)).mode == "LOCAL_COMPUTE"
    assert prover.prove(wanted, artifact(top_n=10, ordering=[]), inspections=(comparison,)).mode == "SQL_REQUIRED"


def test_top_five_from_ten_and_rank_direction_are_proven(catalog):
    prover = CoverageProver(catalog)
    wanted = spec(top_n=2)
    source = artifact(top_n=10)
    decision = prover.prove(wanted, source)
    frame = pd.DataFrame({"customer_state":["SP","RJ","MG"],"gmv":[300,200,100]})
    assert len(prover.compute(wanted, decision, frame)) == 2
    assert prover.prove(wanted, artifact(ordering=[{"column":"gmv","direction":"asc"}])).mode == "SQL_REQUIRED"


def test_derived_limited_population_cannot_be_relabelled_as_a_complete_result(catalog):
    source = artifact(top_n=None, population_coverage="top_n")
    total = RequestedAnswerSpec(metric_ids=("gmv",))
    decision = CoverageProver(catalog).prove(total, source)
    assert decision.mode == "SQL_REQUIRED" and "population_scope_unproven" in decision.missing_requirements


@pytest.mark.parametrize("metric", ["average_order_value","positive_review_rate","unique_customer_count"])
def test_non_additive_measures_cannot_be_rolled_up(catalog, metric):
    source = artifact(metrics=(metric,))
    wanted = RequestedAnswerSpec(metric_ids=(metric,))
    assert "non_additive_rollup" in CoverageProver(catalog).prove(wanted, source).missing_requirements


def test_complete_months_can_be_filtered_and_rolled_up_but_missing_month_is_not_zero(catalog):
    prover = CoverageProver(catalog)
    year = {"start":"2017-01-01", "end":"2018-01-01", "end_semantics":"exclusive"}
    q1 = {"start":"2017-01-01", "end":"2017-04-01", "end_semantics":"exclusive"}
    source = artifact(dimensions=(), time_grain="month", time_range=year)
    wanted = RequestedAnswerSpec(metric_ids=("gmv",), time_range=q1)
    decision = prover.prove(wanted, source)
    assert decision.mode == "LOCAL_COMPUTE"
    frame = pd.DataFrame({"time":pd.date_range("2017-01-01", periods=12, freq="MS"), "gmv":range(1,13)})
    assert prover.compute(wanted, decision, frame).iloc[0]["gmv"] == 6
    with pytest.raises(ResultInspectionError, match="缺少完整时间桶"):
        prover.compute(wanted, decision, frame.drop(index=1))
    partial = wanted.model_copy(update={"time_range":wanted.time_range.model_copy(update={"start":"2017-01-15"})})
    assert prover.prove(partial, source).mode == "SQL_REQUIRED"
    later = RequestedAnswerSpec(metric_ids=("gmv",), time_range={"start":"2018-01-01","end":"2019-01-01"})
    assert prover.prove(later, source).mode == "SQL_REQUIRED"


def test_month_to_quarter_does_not_change_numbers_or_recover_finer_days(catalog):
    prover = CoverageProver(catalog)
    year = {"start":"2017-01-01", "end":"2018-01-01", "end_semantics":"exclusive"}
    source = artifact(dimensions=(), time_grain="month", time_range=year)
    wanted = RequestedAnswerSpec(metric_ids=("gmv",), time_grain="quarter", time_range=year)
    decision = prover.prove(wanted, source)
    frame = pd.DataFrame({"time":pd.date_range("2017-01-01", periods=12, freq="MS"),"gmv":range(1,13)})
    result = prover.compute(wanted, decision, frame)
    assert result["gmv"].tolist() == [6,15,24,33]
    assert prover.prove(wanted.model_copy(update={"time_grain":"day"}), source).mode == "SQL_REQUIRED"


def test_missing_state_never_becomes_zero(catalog):
    prover = CoverageProver(catalog)
    wanted = RequestedAnswerSpec(metric_ids=("gmv",), filters=[{"dimension":"customer_state","value":"ZZ"}])
    decision = prover.prove(wanted, artifact())
    with pytest.raises(ResultInspectionError, match="缺失当作零"):
        prover.compute(wanted, decision, pd.DataFrame({"customer_state":["SP"],"gmv":[300]}))


def test_literal_nan_dimension_does_not_match_null_and_duplicate_grain_is_rejected(catalog):
    prover = CoverageProver(catalog)
    wanted = RequestedAnswerSpec(metric_ids=("gmv",),filters=[{"dimension":"customer_state","value":"nan"}])
    decision = prover.prove(wanted,artifact())
    frame = pd.DataFrame({"customer_state":["nan",float("nan")],"gmv":[10,999]})
    assert prover.compute(wanted,decision,frame).iloc[0]["gmv"] == 10
    with pytest.raises(ResultInspectionError,match="唯一聚合粒度"):
        prover.compute(wanted,decision,pd.DataFrame({"customer_state":["SP","SP"],"gmv":[10,20]}))


def test_requested_answer_is_catalog_validated_and_not_an_execution_permission(catalog):
    text = SemanticIntent(request_kind="data_query", metric_ids=("gmv",),
        requested_answer=RequestedAnswerSpec(metric_ids=("secret_metric",))).model_dump_json()
    with pytest.raises(SemanticIntentError, match="unknown_or_invisible_metric"):
        parse_semantic_intent(text, catalog)
    text = '{"request_kind":"result_followup","requested_answer":{"metric_ids":[]}}'
    with pytest.raises(SemanticIntentError, match="invalid_intent_output"):
        parse_semantic_intent(text, catalog)
