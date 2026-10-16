from __future__ import annotations

import json
from dataclasses import replace

import pytest
from vanna.core.storage import Conversation, Message
from vanna.core.user import User

from data_analysis_agent.analysis_state import AnalysisState, StatePatch
from data_analysis_agent.analysis_lifecycle import clear_active_analysis_state, prepare_analysis_state
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE as WORKSPACE
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.semantic_intent import parse_semantic_intent, resolve_state_intent, route_semantic_intent
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.result_artifact import ResultArtifact


USER = User(id="state-test", group_memberships=["analyst"])


def apply(state, operations):
    return state.apply_patch(StatePatch.model_validate({"operations": operations}), message_count=10)


def test_scope_changes_preserve_untouched_fields_and_new_task_resets():
    state = replace(AnalysisState.new(WORKSPACE), metric_ids=("gmv",), dimensions=("customer_state",),
                    time_range={"start": "2017-01-01", "end": "2018-01-01", "end_semantics": "exclusive"})
    state = apply(state, [{"op": "add", "field": "filters", "value": ["customer_state=SP"]}])
    assert state.filters == ("customer_state=SP",)
    state = apply(state, [{"op": "set", "field": "filters", "value": ["customer_state=RJ"]}])
    assert state.filters == ("customer_state=RJ",)
    state = apply(state, [{"op": "remove", "field": "filters", "value": ["customer_state=RJ"]}])
    assert state.filters == ()
    assert state.metric_ids == ("gmv",) and state.time_range["start"] == "2017-01-01"
    state = apply(state, [{"op": "clear", "field": "dimensions"}])
    assert state.dimensions == ()
    newer = apply(state, [{"op": "replace_task"}])
    assert newer.task_id != state.task_id and newer.metric_ids == ()
    assert newer.history_start_index == 10


def test_last_successful_plan_remains_linked_when_current_query_fails():
    state = replace(AnalysisState.new(WORKSPACE), active_plan={"metric_ids": ["gmv"]}, run_id="run-1")
    artifact = ResultArtifact("artifact", "有效结果", ("gmv",), ("gmv",), WORKSPACE.dataset_version_id, WORKSPACE.metric_version, "not_rendered")
    state = state.with_result_artifact(artifact)
    state = apply(state, [{"op": "set", "field": "metric_ids", "value": ["paid_order_count"]}])
    failed = replace(state, active_plan={"metric_ids": ["paid_order_count"]}, status="failed", run_id="run-2")
    restored = AnalysisState.from_mapping(failed.as_dict())
    assert restored.last_successful_plan == {"metric_ids": ["gmv"]}
    assert restored.last_successful_run_id == "run-1"
    assert restored.active_artifact_id == "artifact"
    assert restored.metric_ids == ("paid_order_count",)


def test_clear_and_version_change_keep_history_and_start_new_task():
    state = AnalysisState.new(WORKSPACE)
    conversation = Conversation(id="state-clear", user=USER, messages=[Message(role="user", content="旧问题")],
                                metadata={"working_memory": state.as_dict(), "message_count": 30})
    clear_active_analysis_state(conversation, WORKSPACE)
    restored, notice = prepare_analysis_state(conversation, WORKSPACE)
    assert not notice and restored.task_id != state.task_id
    assert restored.history_start_index == 30 and len(conversation.messages) == 1
    conversation.metadata["metric_version"] = "old"
    newer, notice = prepare_analysis_state(conversation, WORKSPACE)
    assert notice and newer.task_id != restored.task_id and newer.metric_ids == ()


def test_incremental_intent_is_validated_after_merge():
    catalog = CatalogLoader(WORKSPACE).load()
    state = replace(AnalysisState.new(WORKSPACE), metric_ids=("gmv",), dimensions=("customer_state",))
    intent = parse_semantic_intent(json.dumps({"request_kind": "data_query", "action": "update_task", "state_patch": {
        "operations": [{"op": "set", "field": "filters", "value": ["customer_state=SP"]}]
    }}), catalog, user=USER)
    resolved, proposed = resolve_state_intent(intent, state, catalog, USER, message_count=8)
    assert resolved.metric_ids == ("gmv",) and resolved.filters[0].value == "SP"
    assert proposed.task_id == state.task_id
    router = QuestionRouter(CatalogRetriever(catalog), full_results=True)
    selection = router.retriever.retrieve("gmv", USER, metric_ids=resolved.metric_ids)
    assert route_semantic_intent(resolved, router, selection, proposed.as_dict()).should_generate_sql


def test_structured_filter_patch_matches_semantic_intent_shape():
    catalog = CatalogLoader(WORKSPACE).load()
    state = replace(AnalysisState.new(WORKSPACE), metric_ids=("gmv",), dimensions=("customer_state",))
    intent = parse_semantic_intent(json.dumps({"request_kind": "data_query", "action": "update_task",
        "state_patch": {"operations": [{"op": "set", "field": "filters",
        "value": [{"dimension": "customer_state", "value": "SP"}]}]}}), catalog, user=USER)
    resolved, proposed = resolve_state_intent(intent, state, catalog, USER, message_count=8)
    assert resolved.filters[0].value == "SP"
    assert proposed.filters == ("customer_state=SP",)


def test_patch_is_the_only_authority_for_incremental_scope_changes():
    catalog = CatalogLoader(WORKSPACE).load()
    state = replace(AnalysisState.new(WORKSPACE), metric_ids=("gmv",),
                    dimensions=("customer_state",), time_grain=None, top_n=5)
    intent = parse_semantic_intent(json.dumps({
        "request_kind": "data_query", "action": "update_task",
        "time_grain": "year", "metric_ids": ["paid_order_count"],
        "state_patch": {"operations": [{"op": "set", "field": "filters", "value": ["customer_state=SP"]}]},
    }), catalog, user=USER)
    resolved, proposed = resolve_state_intent(intent, state, catalog, USER, message_count=8)
    assert proposed.metric_ids == ("gmv",) and proposed.time_grain is None and proposed.top_n == 5
    assert resolved.filters[0].value == "SP"


def test_patch_cannot_introduce_hidden_metric_or_multiple_state_filters():
    catalog = CatalogLoader(WORKSPACE).load()
    state = replace(AnalysisState.new(WORKSPACE), metric_ids=("gmv",))
    intent = parse_semantic_intent(json.dumps({"request_kind": "data_query", "action": "update_task", "state_patch": {
        "operations": [{"op": "set", "field": "metric_ids", "value": ["invented_metric"]}]
    }}), catalog, user=USER)
    with pytest.raises(ValueError, match="invalid_state_patch"):
        resolve_state_intent(intent, state, catalog, USER, message_count=8)
    intent = parse_semantic_intent(json.dumps({"request_kind": "data_query", "action": "update_task", "state_patch": {
        "operations": [{"op": "add", "field": "filters", "value": ["customer_state=SP", "customer_state=RJ"]}]
    }}), catalog, user=USER)
    resolved, proposed = resolve_state_intent(intent, state, catalog, USER, message_count=8)
    router = QuestionRouter(CatalogRetriever(catalog), full_results=True)
    selection = router.retriever.retrieve("gmv", USER, metric_ids=resolved.metric_ids)
    assert not route_semantic_intent(resolved, router, selection, proposed.as_dict()).should_generate_sql


@pytest.mark.parametrize("dimensions,explicit_limit,expected", [
    (["product_category_name"], None, None),
    ([], None, None),
    (["customer_state"], None, 1),
    (["product_category_name"], 10, 10),
])
@pytest.mark.parametrize("limit_first", [True, False])
def test_rank_limit_belongs_to_grouping_population(dimensions, explicit_limit, expected, limit_first):
    state = replace(AnalysisState.new(WORKSPACE), metric_ids=("gmv",),
                    dimensions=("customer_state",), filters=("customer_state=SP",), top_n=1)
    operations = [{"op": "set", "field": "dimensions", "value": dimensions}]
    if explicit_limit is not None:
        operations.insert(0 if limit_first else 1, {"op": "set", "field": "top_n", "value": explicit_limit})
    proposed = apply(state, operations)
    assert proposed.top_n == expected
    assert proposed.metric_ids == ("gmv",) and proposed.filters == ("customer_state=SP",)
