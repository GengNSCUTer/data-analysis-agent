"""Business counterexamples for full-result calculations and adaptive evidence budgets."""
import json

import pandas as pd
import pytest

from data_analysis_agent.budget import BudgetUsage, RequestBudget
from data_analysis_agent.result_analysis import ResultInspection, acquisition_scope, analyze_result
from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX, build_result_evidence
from data_analysis_agent.token_budget import DEFAULT_TOKEN_COUNTER


def run(frame, operation="trend", *, scope=None, **params):
    return analyze_result(frame, requests=[ResultInspection(operation=operation, metric_id="gmv", **params)],
                          metric_ids=("gmv",), additive_metric_ids=("gmv",),
                          scope=scope or {"time_grain": "month", "time_range": {"start": "2024-01-01", "end": "2024-05-01"}},
                          time_column="time")[0]


@pytest.mark.parametrize("values,mode,judgment,counts", [
    ([100,120,110,150], "strict_increase", "false", (2,1,0)),
    ([100,110,120,150], "strict_increase", "true", (3,0,0)),
    ([100,110,110,150], "strict_increase", "false", (2,0,1)),
    ([100,110,110,150], "nondecreasing", "true", (2,0,1)),
])
def test_trend_uses_every_adjacent_calendar_period(values, mode, judgment, counts):
    frame = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=4, freq="MS"), "gmv": values})
    group = run(frame, trend_mode=mode)["groups"][0]
    assert group["judgment"] == judgment
    assert (group["up_count"], group["down_count"], group["flat_count"]) == counts
    if values == [100,120,110,150]:
        assert group["first_counterexample"]["period"] == "2024-03"
        assert group["endpoints"]["relative_change_percent"] == 50


@pytest.mark.parametrize("times,values,expected", [
    (["2024-01-01", "2024-03-01", "2024-04-01"], [100,120,150], "insufficient"),
    (["2024-01-01", "2024-03-01", "2024-04-01"], [100,150,120], "false"),
    (["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"], [100,None,120,150], "insufficient"),
    (["2024-01-01", "2024-01-01", "2024-03-01", "2024-04-01"], [100,110,120,150], "insufficient"),
    (["2024-01-01"], [100], "insufficient"),
    ([], [], "insufficient"),
])
def test_missing_null_duplicate_and_single_period_are_not_filled_or_summed(times, values, expected):
    group = run(pd.DataFrame({"time": times, "gmv": values}), trend_mode="strict_increase")["groups"][0]
    assert group["judgment"] == expected


@pytest.mark.parametrize("grain,start,end,periods,freq", [
    ("day", "2024-02-28", "2024-03-02", 3, "D"),
    ("week", "2023-12-25", "2024-01-15", 3, "W-MON"),
    ("quarter", "2023-10-01", "2024-07-01", 3, "QS"),
    ("year", "2022-01-01", "2025-01-01", 3, "YS"),
])
def test_calendar_grains_and_reordering(grain, start, end, periods, freq):
    frame = pd.DataFrame({"time": pd.date_range(start, periods=periods, freq=freq), "gmv": range(periods)})
    scope = {"time_grain": grain, "time_range": {"start": start, "end": end}}
    expected = run(frame, scope=scope)
    assert expected["groups"][0]["complete"]
    assert run(frame.sample(frac=1, random_state=7), scope=scope) == expected


def test_state_series_are_not_concatenated_and_top_n_membership_is_not_zero():
    frame = pd.DataFrame({"time": list(pd.date_range("2024-01-01", periods=4, freq="MS")) * 2,
                          "state": ["SP"]*4+["RJ"]*4, "gmv": [1,2,3,4,9,8,7,6]})
    scope = {"dimensions": ["state"], "time_grain": "month", "time_range": {"start":"2024-01-01", "end":"2024-05-01"}}
    groups = run(frame, scope=scope)["groups"]
    assert [g["judgment"] for g in groups] == ["true", "false"]
    limited = run(frame.iloc[:4], scope={**scope, "top_n": 1})["groups"][0]
    assert limited["judgment"] == "insufficient" and limited["top_n_limited"]


def test_comparisons_handle_zero_negative_and_percentage_points():
    for baseline, status in [(0,"zero"), (-10,"negative"), (10,"positive")]:
        result = run(pd.DataFrame({"gmv":[15,baseline]}), "compare", row_numbers=(1,2))
        assert result["difference"] == 15-baseline and result["baseline_status"] == status
        assert (result["relative_change_percent"] is None) == (baseline <= 0)
    result = analyze_result(pd.DataFrame({"rate":[.15,.10]}),
                            requests=[ResultInspection(operation="compare", metric_id="rate", row_numbers=(1,2))],
                            metric_ids=("rate",), metric_semantics={"rate":{"kind":"rate","unit":"比例0至1"}})[0]
    assert result["percentage_points"] == 5 and result["relative_change_percent"] == 50


def test_period_compare_and_growth_ranking_use_the_requested_baseline():
    frame = pd.DataFrame({"time":["2024-03-01","2024-04-01"]*2, "state":["SP","SP","RJ","RJ"], "gmv":[100,110,10,20]})
    scope = {"dimensions":["state"], "time_grain":"month", "time_range":{"start":"2024-03-01","end":"2024-05-01"}}
    compared = run(frame, "period_compare", scope=scope)
    assert [c["relative_change_percent"] for c in compared["comparisons"]] == [10,100]
    ranked = run(frame, "rank", scope=scope, rank_by="relative_change", limit=1)
    assert ranked["rows"][0]["state"] == "RJ"
    missing = run(frame, "period_compare", scope=scope, baseline="previous_year")
    assert missing["status"] == "query_required"
    assert missing["query_requirements"]["time_range"]["start"] == "2023-04-01"


def test_acquisition_keeps_comparison_operands_and_expands_only_required_scope():
    scope = {"time_grain":"year", "time_range":{"start":"2024-01-01","end":"2025-01-01"}, "top_n":10}
    updates, requests = acquisition_scope([
        ResultInspection(operation="period_compare", metric_id="gmv", baseline="previous_year"),
        ResultInspection(operation="share", metric_id="gmv", denominator="business_total"),
    ], scope)
    assert updates["time_range"]["start"] == "2023-01-01"
    assert requests[0].periods == ("2024-01-01","2023-01-01")
    assert updates["top_n"] is None and requests[1].numerator_top_n == 10


def test_ranking_respects_partitions_and_explicit_ties():
    frame = pd.DataFrame({"month":["Jan"]*3+["Feb"]*3, "state":["A","B","C"]*2, "gmv":[20,20,10,5,8,9]})
    result = run(frame, "rank", group_by=("month",), limit=1)
    assert [(r["month"],r["state"]) for r in result["rows"]] == [("Jan","A"),("Feb","C")]
    ties = run(frame, "rank", group_by=("month",), limit=1, include_ties=True)
    assert len(ties["rows"]) == 3


def test_shares_distinguish_top_n_and_full_denominators_and_nonadditivity():
    frame = pd.DataFrame({"category":["A","B","C"], "gmv":[60,30,10]})
    assert run(frame, "share", row_numbers=(1,2))["share_percent"] == 90
    assert run(frame.iloc[:2], "share", row_numbers=(1,), scope={"top_n":2})["share_percent"] == pytest.approx(100*60/90)
    needed = run(frame.iloc[:2], "share", denominator="business_total", scope={"top_n":2})
    assert needed["status"] == "query_required" and needed["query_requirements"] == {"top_n":None}
    rate = analyze_result(pd.DataFrame({"rate":[.9,.5]}), requests=[ResultInspection(operation="sum", metric_id="rate")], metric_ids=("rate",))[0]
    assert rate["status"] == "query_required" and "value" not in rate


def test_distribution_threshold_and_null_population():
    frame = pd.DataFrame({"state":["A","B","C","D"], "gmv":[10,20,30,None]})
    distribution = run(frame, "distribution")
    assert (distribution["count"],distribution["null_count"],distribution["median"],distribution["p25"],distribution["p75"]) == (3,1,20,15,25)
    threshold = run(frame, "threshold", comparison="ge", threshold_value=20)
    assert threshold["match_count"] == 2
    assert [r["state"] for r in threshold["rows"]] == ["B","C"]


def test_cumulative_and_rolling_use_calendar_windows():
    frame = pd.DataFrame({"time":pd.date_range("2024-01-01", periods=4, freq="MS"), "gmv":[1,2,3,4]})
    cumulative = run(frame, trend_mode="cumulative")["groups"][0]
    assert [r["value"] for r in cumulative["derived_rows"]] == [1,3,6,10]
    rolling = run(frame, trend_mode="rolling", window=3)["groups"][0]
    assert [r["value"] for r in rolling["derived_rows"]] == [None,None,2,3]
    assert "derived_rows" not in run(frame.drop(index=1), trend_mode="rolling")["groups"][0]


def test_partial_calendar_period_is_not_silently_expanded_to_make_growth_claims():
    scope = {"time_grain":"month", "time_range":{"start":"2024-03-01", "end":"2024-04-15"}}
    frame = pd.DataFrame({"time":["2024-03-01","2024-04-01"], "gmv":[100,50]})
    result = run(frame, "period_compare", scope=scope)
    assert result["status"] == "unsupported" and "部分期间" in result["reason"]
    assert acquisition_scope([ResultInspection(operation="period_compare", metric_id="gmv")], scope)[0] == {}


def test_growth_ranking_requires_complete_membership_and_reports_zero_baselines():
    frame = pd.DataFrame({"state":["A","A","B","B"], "time":["2024-03-01","2024-04-01"]*2, "gmv":[0,100,10,20]})
    scope = {"dimensions":["state"], "time_grain":"month", "time_range":{"start":"2024-03-01","end":"2024-05-01"}}
    result = run(frame,"rank",scope=scope,rank_by="relative_change")
    assert result["rows"][0]["state"] == "B" and result["unrankable_count"] == 1
    limited = run(frame,"rank",scope={**scope,"top_n":1},rank_by="relative_change")
    assert limited["status"] == "query_required" and limited["query_requirements"]["top_n"] is None


def test_nonfinite_values_are_missing_and_decimal_money_is_not_float_summed():
    from decimal import Decimal
    frame = pd.DataFrame({"gmv":[Decimal("0.1"),Decimal("0.2"),None]})
    assert run(frame,"sum")["value"] == .3
    distribution = run(pd.DataFrame({"gmv":[1,float("inf"),float("nan")]}),"distribution")
    assert distribution["count"] == 1 and distribution["null_count"] == 2


def test_same_requested_fact_is_not_repeated_in_a_combination_answer():
    frame = pd.DataFrame({"gmv":[1,2,3]})
    request = ResultInspection(operation="sum", metric_id="gmv")
    assert len(analyze_result(frame, requests=[request, request], metric_ids=("gmv",), additive_metric_ids=("gmv",))) == 1


def test_increment_contribution_uses_entity_differences_and_zero_net_has_no_ratio():
    frame = pd.DataFrame({"time":["2024-03-01","2024-04-01"]*2, "state":["A","A","B","B"],"gmv":[100,110,10,20]})
    scope = {"dimensions":["state"],"time_grain":"month","time_range":{"start":"2024-03-01","end":"2024-05-01"}}
    result = run(frame,"share",scope=scope,share_basis="change")
    assert result["denominator"] == 20
    assert [r["contribution_percent"] for r in result["contributions"]] == [50,50]
    frame.loc[3,"gmv"] = 0
    result = run(frame,"share",scope=scope,share_basis="change")
    assert result["denominator"] == 0 and result["share_percent"] is None
    assert [r["difference"] for r in result["contributions"]] == [10,-10]


def test_baseline_acquisition_does_not_change_the_other_operations_period():
    scope = {"time_grain":"year", "time_range":{"start":"2024-01-01", "end":"2025-01-01"}}
    updates, requests = acquisition_scope([
        ResultInspection(operation="period_compare", metric_id="gmv", baseline="previous_year"),
        ResultInspection(operation="sum", metric_id="gmv"),
    ], scope)
    frame = pd.DataFrame({"time":[pd.Timestamp("2023-01-01",tz="Asia/Shanghai"),pd.Timestamp("2024-01-01",tz="Asia/Shanghai")], "gmv":[1000,10]})
    result = analyze_result(frame, requests=requests, metric_ids=("gmv",), scope={**scope,**updates}, additive_metric_ids=("gmv",), time_column="time")
    assert result[0]["comparisons"][0]["difference"] == -990
    assert result[1]["value"] == 10


def test_new_denominator_order_cannot_rebind_the_old_selected_numerator():
    request = ResultInspection(operation="share", metric_id="gmv", row_numbers=(1,), denominator="business_total")
    old = run(pd.DataFrame({"category":["A","B"],"gmv":[70,20]}),"share",row_numbers=(1,),denominator="business_total",scope={"top_n":2})
    assert old["numerator"] == 70
    frame = pd.DataFrame({"category":["C","B","A"],"gmv":[10,20,70]})
    result = analyze_result(frame,requests=[request],metric_ids=("gmv",),additive_metric_ids=("gmv",),
                             numerator_overrides={0:{"value":old["numerator"],"row_count":1}})[0]
    assert result["numerator"] == 70 and result["share_percent"] == 70


def test_requested_calculations_produce_evidence_for_the_model_to_explain():
    frame = pd.DataFrame({"time":pd.date_range("2024-01-01", periods=4, freq="MS"), "gmv":[100,120,110,150]})
    summary = build_result_evidence(frame, required_columns=("time","gmv"), metric_ids=("gmv",),
                                    additive_metric_ids=("gmv",), time_column="time", column_labels={"gmv":"销售额"},
                                    query_scope={"time_grain":"month", "time_range":{"start":"2024-01-01","end":"2024-05-01"}},
                                    inspections=[ResultInspection(operation="trend", metric_id="gmv", trend_mode="strict_increase")])
    payload = json.loads(summary.removeprefix(RESULT_EVIDENCE_PREFIX))
    group = payload["analyses"][0]["groups"][0]
    assert group["judgment"] == "false"
    assert group["first_counterexample"]["current"] == 110
    assert group["first_counterexample"]["baseline"] == 120
    assert payload["column_labels"]["gmv"] == "销售额"


def test_result_budget_grows_with_available_context_and_packs_more_than_four_k():
    usage = BudgetUsage(RequestBudget())
    short = usage.result_evidence_limit()
    usage.context_packet = {"recent_dialogue":"较长的对话内容"*1500}
    assert 4000 < usage.result_evidence_limit() < short
    constrained = BudgetUsage(RequestBudget(max_result_evidence_tokens=2000))
    assert constrained.result_evidence_limit() == 2000
    frame = pd.DataFrame({"state":[f"州{i}" for i in range(250)], "gmv":range(250)})
    summary = build_result_evidence(frame, required_columns=("state","gmv"), metric_ids=("gmv",), max_tokens=short)
    payload = json.loads(summary.removeprefix(RESULT_EVIDENCE_PREFIX))
    assert payload["coverage"] == "complete"
    assert 4000 < DEFAULT_TOKEN_COUNTER.count_text(summary) <= short
