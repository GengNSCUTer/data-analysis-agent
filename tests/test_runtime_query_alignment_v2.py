"""Question-derived semantics across workspaces; no source Spec or held-out data."""

from dataclasses import replace
import json

import pandas as pd
import pytest
from vanna.core.user import User

from data_analysis_agent.candidate_sql_generator import (
    CandidateSqlGenerationError,
    build_runtime_candidate_request,
)
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.query_time import copy_time_range, extract_absolute_range
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.runtime_alignment_audit import replay_single_turn
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, ResultContract
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE
from data_analysis_agent.working_memory import WorkingMemory
from data_analysis_agent.workspace import DimensionBinding


@pytest.fixture(scope="module")
def thelook():
    return CatalogRetriever(CatalogLoader(THELOOK_V2_WORKSPACE).load())


@pytest.fixture(scope="module")
def olist():
    return CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load())


@pytest.mark.parametrize("metric,alias,dimension,physical", [
    ("completed_item_count", "客户州", "customer_state", ("users", "state")),
    ("completed_item_count", "客户城市", "customer_city", ("users", "city")),
    ("completed_item_count", "客户注册流量来源", "customer_traffic_source", ("users", "traffic_source")),
    ("completed_item_count", "商品品类", "product_category", ("products", "category")),
    ("completed_item_count", "商品品牌", "product_brand", ("products", "brand")),
    ("completed_item_count", "商品部门", "product_department", ("products", "department")),
    ("received_inventory_unit_count", "配送中心", "distribution_center", ("distribution_centers", "name")),
    ("event_count", "事件类型", "event_type", ("events", "event_type")),
    ("event_count", "浏览器", "event_browser", ("events", "browser")),
    ("event_count", "事件流量来源", "event_traffic_source", ("events", "traffic_source")),
    ("registered_user_count", "注册用户国家", "user_country", ("users", "country")),
    ("registered_user_count", "注册用户州", "user_state", ("users", "state")),
    ("registered_user_count", "用户注册州地区", "user_state", ("users", "state")),
    ("registered_user_count", "注册用户流量来源", "user_traffic_source", ("users", "traffic_source")),
])
def test_logical_dimension_has_real_catalog_column(thelook, metric, alias, dimension, physical):
    name = thelook.catalog.metrics_by_id[metric].name
    question = f"统计2020-01-01至2020-01-02（不含结束日）按{alias}的{name}"
    replay = replay_single_turn(question, thelook, workspace=THELOOK_V2_WORKSPACE)
    assert replay["route_state"] == "answerable", replay
    assert replay["plan"]["metric_ids"] == [metric]
    assert replay["plan"]["dimensions"] == [dimension]
    assert replay["plan"]["required_result_columns"] == [dimension, metric]
    assert not replay["plan"]["top_n"]
    selection = thelook.retrieve(question, User(id="test", group_memberships=["analyst"]))
    assert physical[1] in dict(selection.trace.selected_columns)[physical[0]]
    assert f"{physical[0]}.{physical[1]}" in replay["candidate_prompt"]
    assert "fact_date_counts" not in replay["candidate_prompt"]
    assert "max_cardinality" not in replay["candidate_prompt"]


@pytest.mark.parametrize("metric,dimension", [
    ("completed_item_count", "customer_state"), ("registered_user_count", "user_state"),
])
def test_shared_state_alias_resolves_in_current_fact_domain(thelook, metric, dimension):
    name = thelook.catalog.metrics_by_id[metric].name
    replay = replay_single_turn(f"统计各州的{name}", thelook)
    assert replay["plan"]["dimensions"] == [dimension]


def test_explicit_unsupported_dimension_is_not_silently_dropped(thelook):
    replay = replay_single_turn("按商品品类统计注册用户数", thelook)
    assert replay["candidate_prompt"] is None
    assert replay["route_reason"] == "dimension_not_supported_by_all_metrics"


def test_snapshot_bound_allows_small_city_window_without_inventing_top_n(thelook):
    metric = thelook.catalog.metrics_by_id["completed_item_count"]
    question = "2020-01-01至2020-01-02（不含结束日）按客户城市统计完成商品件数"
    decision = QueryPlan.preflight((metric,), question, workspace=THELOOK_V2_WORKSPACE, max_result_rows=1000)
    assert decision.allowed
    assert decision.estimated_max_rows == THELOOK_V2_WORKSPACE.fact_date_counts["orders.created_at"]["2020-01-01"]
    assert 0 < decision.estimated_max_rows < 1000
    broad = QueryPlan.preflight((metric,), "按客户城市统计完成商品件数", workspace=THELOOK_V2_WORKSPACE)
    assert not broad.allowed
    assert broad.estimated_max_rows == 7884


def test_workspace_time_bound_does_not_inherit_three_year_olist_assumption(thelook):
    metric = thelook.catalog.metrics_by_id["registered_user_count"]
    decision = QueryPlan.preflight((metric,), "按年统计各州注册用户数", workspace=THELOOK_V2_WORKSPACE)
    assert decision.estimated_max_rows == 6 * 229
    assert decision.reason_code == "result_row_budget_exceeded"


@pytest.mark.parametrize("text,start,end", [
    ("2017-01-01 至 2017-02-01（不含结束日）", "2017-01-01", "2017-02-01"),
    ("2017-01-01 至 2017-02-01", "2017-01-01", "2017-02-02"),
    ("2020-02-29 至 2020-02-29", "2020-02-29", "2020-03-01"),
    ("2018年", "2018-01-01", "2019-01-01"),
])
def test_absolute_dates_have_explicit_exclusive_end(text, start, end):
    bounds = {"start": start, "end": end, "end_semantics": "exclusive"}
    assert extract_absolute_range(text) == bounds
    memory = WorkingMemory(time_range=bounds)
    assert WorkingMemory.from_mapping(memory.as_dict()).time_range == bounds


@pytest.mark.parametrize("text", [
    "2019-02-29至2019-03-01", "2020-04-01至2020-03-01",
    "2020-01-01至2020-01-01（不含结束日）", "2020-01-01",
])
def test_invalid_or_unsupported_dates_do_not_become_whole_years(olist, text):
    assert extract_absolute_range(text) is None
    replay = replay_single_turn(f"统计{text}的有效订单数", olist)
    assert replay["candidate_prompt"] is None
    assert replay["route_reason"] == "invalid_absolute_range"


def test_legacy_time_state_needs_reconfirmation_not_silent_reinterpretation(olist):
    legacy = {"start": "2017-01-01", "end": "2017-12-31"}
    assert copy_time_range(legacy) == legacy
    user = User(id="test", group_memberships=["analyst"])
    router = QuestionRouter(olist)
    route = router.admit_sql_request("统计有效订单数", user=user, conversation_state={"time_range": legacy})
    assert route.reason_code == "legacy_time_endpoint_unconfirmed"
    route = router.admit_sql_request("统计2018年有效订单数", user=user, conversation_state={"time_range": legacy})
    assert route.should_generate_sql
    assert copy_time_range({**legacy, "end_semantics": "exclusive", "end": "invalid"}) is None


@pytest.mark.parametrize("question", ["看有效订单数的时间变化", "看有效订单数趋势"])
def test_series_without_a_grain_requires_clarification(olist, question):
    replay = replay_single_turn(question, olist)
    assert replay["candidate_prompt"] is None
    assert replay["route_reason"] == "time_grain_unspecified"


def test_week_bound_counts_calendar_buckets_not_seven_day_chunks():
    # PostgreSQL date_trunc('week') puts Sunday and Monday into separate buckets.
    assert QueryPlan._series_bucket_bound("week", "2020-01-05至2020-01-06", {}) == 2
    assert QueryPlan._series_bucket_bound("week", "2020-01-05至2020-01-06（不含结束日）", {}) == 1


@pytest.mark.parametrize("wording", ["季度汇总", "季度度变化", "季度度序列", "季度时间序列"])
def test_explicit_quarter_series_is_recognized(olist, wording):
    replay = replay_single_turn(f"看2017年有效订单数的{wording}", olist)
    assert replay["plan"]["time_grain"] == "quarter"
    assert replay["plan"]["required_result_columns"] == ["time", "paid_order_count"]


def test_series_wording_does_not_bypass_a_real_comparison_baseline(olist):
    trend = replay_single_turn("统计2017年有效订单数的季度趋势", olist)
    assert trend["plan"]["time_grain"] == "quarter"
    growth = replay_single_turn("统计2017年有效订单数的季度增长", olist)
    assert growth["candidate_prompt"] is None
    assert growth["route_reason"] == "comparison_without_baseline"


def test_required_columns_are_canonical_independent_of_metric_question_order(olist):
    left = replay_single_turn("2017年按月统计各州 GMV 和有效订单数", olist, workspace=OLIST_RUNTIME_WORKSPACE)
    right = replay_single_turn("2017年按月统计各州有效订单数和 GMV", olist, workspace=OLIST_RUNTIME_WORKSPACE)
    assert left["plan"]["required_result_columns"] == right["plan"]["required_result_columns"] == [
        "customer_state", "time", "gmv", "paid_order_count",
    ]
    contract = json.loads(left["candidate_prompt"].split("### Server-provided ResultContract\n")[1].split("\n### Question")[0])
    assert contract["requested_end"] == "2018-01-01"
    assert contract["requested_end_exclusive"] is True


def test_candidate_builder_rejects_unconfirmed_or_disagreeing_endpoints(olist):
    question = "统计2018年有效订单数"
    user = User(id="test", group_memberships=["analyst"])
    selection = olist.retrieve(question, user)
    route = QuestionRouter(olist).admit_sql_request(question, user=user, selection=selection)
    plan = QueryPlan.from_selection(selection, question, route)
    contract = ResultContract.from_selection(selection, question, plan.time_range, catalog=olist.catalog,
                                            required_result_columns=plan.required_result_columns)
    facts = dict(question=question, selection=selection, route=route, query_plan=plan,
                 result_contract=contract, workspace=OLIST_RUNTIME_WORKSPACE)
    assert build_runtime_candidate_request(**facts)
    with pytest.raises(CandidateSqlGenerationError, match="endpoints mismatch"):
        build_runtime_candidate_request(**{**facts, "result_contract": replace(contract, requested_end_exclusive=False)})
    legacy_plan = replace(plan, time_range={"start": "2018-01-01", "end": "2018-12-31"})
    with pytest.raises(CandidateSqlGenerationError, match="confirmed exclusive"):
        build_runtime_candidate_request(**{**facts, "query_plan": legacy_plan})


def test_validator_distinguishes_new_exclusive_and_legacy_inclusive_upper_bound():
    frame = pd.DataFrame({"time": ["2018-01-01"], "gmv": [10.0]})
    args = dict(required_columns=("time", "gmv"), metric_columns=("gmv",),
                time_column="time", requested_start="2017-01-01", requested_end="2018-01-01")
    assert ResultValidator().validate(frame, **args).state == "valid"
    result = ResultValidator().validate(frame, **args, requested_end_exclusive=True)
    assert result.state == "degraded"
    assert not result.safe_to_answer


@pytest.mark.parametrize("changes", [
    {"dimension_bindings": {"unsafe": DimensionBinding(("编号",), (("users", "id"),), 1)}},
    {"fact_date_counts": {"users.created_at": {"2020-02-30": 1}}},
    {"fact_date_counts": {"users.created_at": {"2020-01-01": -1}}},
])
def test_workspace_rejects_unsafe_shape_configuration(changes):
    with pytest.raises(ValueError):
        replace(THELOOK_V2_WORKSPACE, **changes)
