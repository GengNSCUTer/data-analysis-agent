"""The evaluator must reject wrong values, not merely successful SQL/HTTP."""
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/evals"))
from check_agent_business_values import compare_frames, expected_calculation, numeric_equal, read_csv, reference_sql

MODULE = importlib.util.spec_from_file_location("business_tasks_runner", ROOT / "scripts/evals/run_agent_business_tasks.py")
runner = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(runner)


def test_suite_has_balanced_independent_tasks_and_complete_conversations():
    tasks = runner.load_tasks(ROOT / "evals/cases/agent_business_tasks_v1.yaml")
    assert len(tasks) == 60
    assert all(4 <= len(task["steps"]) <= 6 for task in tasks if task["category"] == "continuity")
    assert all("reference_sql" not in step or step.get("reference") for task in tasks for step in task["steps"])
    assert len({task["goal"] for task in tasks}) == 60


def test_reference_does_not_use_payment_fact_for_gmv_or_model_scope():
    statement, params = reference_sql("olist", {"metric":"gmv", "state":"SP", "start":"2017-01-01", "end":"2018-01-01"})
    assert "SUM(i.price)" in statement and "fact_payments" not in statement
    assert "c.customer_state=%s" in statement
    assert params == ["2017-01-01", "2018-01-01", "SP"]
    assert "order_status NOT IN" in statement


def test_reference_partition_ranking_is_per_month_and_not_global_limit():
    statement, params = reference_sql("thelook", {"metric":"completed_sale_amount", "group":"customer_state", "grain":"month", "partition_top_n":5})
    assert "PARTITION BY time" in statement
    assert "WHERE position<=%s" in statement and "LIMIT" not in statement
    assert params == [5]


def test_money_swapped_entities_and_numeric_metadata_fail():
    expected = pd.DataFrame({"customer_state":["SP","RJ"], "gmv":[Decimal("20"),Decimal("10")]})
    assert compare_frames(read_csv("customer_state,gmv\nRJ,10\nSP,20\n"), expected) == []
    assert compare_frames(read_csv("customer_state,gmv\nSP,10\nRJ,20\n"), expected) == ["numeric:gmv"]
    assert compare_frames(read_csv("customer_state,gmv\nSP,2\nRJ,2\n"), expected) == ["numeric:gmv"]


def test_null_and_literal_na_are_preserved_and_extra_rows_fail():
    frame = read_csv("customer_state,gmv\nNA,10\n,20\n")
    expected = pd.DataFrame({"customer_state":["NA",None], "gmv":[Decimal("10"),Decimal("20")]})
    assert compare_frames(frame, expected) == []
    assert compare_frames(frame.iloc[:1], expected) == ["row_count"]


@pytest.mark.parametrize("metric,actual,expected,valid",[
    ("paid_order_count", "1.001", 1, False), ("paid_order_count", "1.0",1,True),
    ("gmv","10.125",Decimal("10.13"),True), ("gmv","10.12",Decimal("10.13"),False),
    ("positive_review_rate","0.401",Decimal("0.4"),False),
    ("completed_sale_amount","10.00000001",10,True),
    ("completed_sale_amount","inf",float("inf"),False),
])
def test_numeric_rules_are_explicit(metric,actual,expected,valid):
    assert numeric_equal(actual,expected,metric) is valid


def test_timestamps_compare_same_instant_and_sorting_is_optional():
    expected = pd.DataFrame({"time":pd.to_datetime(["2017-01-01","2017-02-01"]), "gmv":[1,2]})
    actual = read_csv("time,gmv\n2017-02-01T00:00:00Z,2\n2017-01-01T00:00:00Z,1\n")
    assert compare_frames(actual,expected) == []
    assert compare_frames(actual,expected,ordered=True)


def test_extract_year_csv_is_a_valid_year_bucket():
    expected = pd.DataFrame({"time":pd.to_datetime(["2017-01-01","2018-01-01"]),"paid_order_count":[3,4]})
    assert compare_frames(read_csv("time,paid_order_count\n2017.0,3\n2018.0,4\n"),expected) == []
    assert compare_frames(read_csv("time,paid_order_count\n2016.0,3\n2018.0,4\n"),expected)


def test_pagination_numbers_and_numeric_looking_dimension_keys():
    actual = pd.DataFrame({"customer_state":["01"],"gmv":[Decimal("10")]})
    expected = read_csv("customer_state,gmv\n01,10.0000\n")
    assert compare_frames(actual,expected) == []
    actual["customer_state"] = "1"
    assert compare_frames(actual,expected) == ["dimension:customer_state"]


def test_independent_analysis_and_rendered_chart_check_values():
    expected = pd.DataFrame({"customer_state":["SP","RJ"],"gmv":[20,10]})
    assert expected_calculation(expected,"gmv","compare",[1,2]) == Decimal(10)
    assert runner.check_chart([{"type":"bar","x":["RJ","SP"],"y":[10,20]}],expected) == []
    assert runner.check_chart([{"type":"bar","x":["SP","RJ"],"y":[10,20]}],expected)
    assert runner.check_analyses({"operation":"sum","metric":"gmv"},{"analyses":[]},expected) == ["analysis_missing:sum"]


def test_completed_request_without_semantic_review_is_not_task_success():
    assert runner.task_status([{"errors":[]}]) == "pending_review"
    assert runner.task_status([{"errors":[]}], "fail") == "failed"
    assert runner.task_status([{"errors":["numeric:gmv"]}],"pass") == "failed"
    assert runner.task_status([{"errors":[]}],"pass") == "passed"


def test_correct_csv_cannot_hide_a_stale_active_top_n_or_filter():
    reference = {"metric":"gmv","group":"product_category_name","top_n":5,"state":"SP"}
    state = {"metric_ids":["gmv"],"dimensions":["product_category_name"],"top_n":10,"time_grain":None,
        "filters":[{"dimension":"customer_state","value":"SP"}]}
    assert runner.check_task_scope(reference,state) == ["scope:top_n"]
    state["top_n"] = 5
    assert runner.check_task_scope(reference,state) == []
    state["filters"] = []
    assert runner.check_task_scope(reference,state) == ["scope:filters"]


def test_partitioned_top_n_has_the_same_active_bound_without_global_limiting():
    reference = {"metric":"completed_sale_amount","group":"customer_state","grain":"month","partition_top_n":5}
    state = {"metric_ids":["completed_sale_amount"],"dimensions":["customer_state"],"time_grain":"month","top_n":5,"filters":[]}
    assert runner.check_task_scope(reference,state) == []


def test_sse_failure_cannot_reuse_a_previous_persisted_answer_as_current_prose():
    answer, failures = runner.wire_answer([{"rich":{"type":"status_card","data":{
        "title":"本次分析未完成","description":"服务调用失败"}}}])
    assert answer == "服务调用失败" and failures == ["服务调用失败"]
    assert runner.wire_answer([{"rich":{"type":"text","data":{"content":"本次查询正确结果"}}}]) == ("本次查询正确结果",[])
