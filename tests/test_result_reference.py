from __future__ import annotations

import pytest
from dataclasses import replace

from data_analysis_agent.analysis_state import AnalysisState
from data_analysis_agent.result_artifact import ResultArtifact
from data_analysis_agent.result_reference import (
    ResultReferenceError,
    apply_reference_filter,
    resolve_ranked_entity,
)
from data_analysis_agent.semantic_intent import ReferenceRequest


def _artifact(*, second_value=80, dimensions=("customer_state",)):
    return ResultArtifact(
        artifact_id="rta_current",
        summary="SP 100; RJ 80",
        metric_ids=("gmv",),
        result_columns=("customer_state", "gmv"),
        dataset_version="olist-dataset-v3",
        metric_version="olist-metrics-v3",
        chart_state="not_rendered",
        workspace_id="olist-demo",
        query_scope={"dimensions": list(dimensions)},
        result_metadata={
            "validation_status": "valid",
            "ordering": [{"column": "gmv", "direction": "desc"}],
            "ranked_entities": [
                {"rank": 1, "dimension": "customer_state", "value": "SP", "order_value": 100},
                {"rank": 2, "dimension": "customer_state", "value": "RJ", "order_value": second_value},
            ],
        },
    )


def _request(**kwargs):
    return ReferenceRequest(
        kind="previous_ranked_entity",
        dimension="customer_state",
        rank=1,
        **kwargs,
    )


def test_resolves_unique_highest_from_current_artifact():
    binding = resolve_ranked_entity(
        _request(expected_source_artifact_id="rta_current"),
        _artifact(),
        workspace_id="olist-demo",
        dataset_version="olist-dataset-v3",
        metric_version="olist-metrics-v3",
    )
    assert binding.value == "SP"
    assert binding.match_mode == "unique_highest"


def test_rejects_tied_highest_and_stale_scope():
    with pytest.raises(ResultReferenceError, match="并列"):
        resolve_ranked_entity(
            _request(), _artifact(second_value=100), workspace_id="olist-demo",
            dataset_version="olist-dataset-v3", metric_version="olist-metrics-v3",
        )
    with pytest.raises(ResultReferenceError, match="版本"):
        resolve_ranked_entity(
            _request(), _artifact(), workspace_id="olist-demo",
            dataset_version="other", metric_version="olist-metrics-v3",
        )


def test_first_row_does_not_claim_unique_highest():
    request = _request(match_mode="first_row")
    binding = resolve_ranked_entity(
        request, _artifact(second_value=100), workspace_id="olist-demo",
        dataset_version="olist-dataset-v3", metric_version="olist-metrics-v3",
    )
    assert binding.value == "SP"


def test_server_owned_binding_replaces_same_dimension_filter():
    state = AnalysisState(filters=("customer_state=RJ", "payment_type=credit_card"))
    updated = apply_reference_filter(
        state,
        resolve_ranked_entity(
            _request(), _artifact(), workspace_id="olist-demo",
            dataset_version="olist-dataset-v3", metric_version="olist-metrics-v3",
        ),
    )
    assert updated.filters == ("payment_type=credit_card", "customer_state=SP")


def test_second_row_resolves_without_claiming_unique_highest():
    binding = resolve_ranked_entity(
        ReferenceRequest(kind="previous_ranked_entity", dimension="customer_state",
                         rank=2, match_mode="first_row"),
        _artifact(), workspace_id="olist-demo", dataset_version="olist-dataset-v3",
        metric_version="olist-metrics-v3",
    )
    assert binding.value == "RJ" and binding.rank == 2


@pytest.mark.parametrize("change,reference_options,reason", [
    (None, {}, "reference_artifact_missing"),
    ({"workspace_id": "other"}, {}, "reference_workspace_mismatch"),
    ({"metric_version": "old"}, {}, "reference_version_stale"),
    ({}, {"expected_source_artifact_id": "old"}, "reference_artifact_mismatch"),
    ({"result_metadata": {"validation_status": "degraded"}}, {}, "reference_result_unvalidated"),
    ({"query_scope": {"dimensions": []}}, {}, "reference_dimension_missing"),
    ({"result_metadata": {"validation_status": "valid", "ordering": []}}, {}, "reference_ordering_missing"),
    ({"result_metadata": {"validation_status": "valid", "ordering": [{"column": "gmv", "direction": "desc"}], "ranked_entities": []}}, {}, "reference_rank_missing"),
])
def test_reference_failures_do_not_create_filters(change, reference_options, reason):
    state = AnalysisState(filters=("customer_state=MG",))
    artifact = replace(_artifact(), **change) if change is not None else None
    with pytest.raises(ResultReferenceError) as failure:
        resolve_ranked_entity(_request(**reference_options), artifact, workspace_id="olist-demo",
                              dataset_version="olist-dataset-v3", metric_version="olist-metrics-v3")
    assert failure.value.reason_code == reason
    assert state.filters == ("customer_state=MG",)
