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


def test_authored_final_variants_have_twelve_balanced_tasks_and_new_questions():
    original = runner.load_tasks(ROOT / 'evals/cases/agent_business_tasks_v1.yaml')
    variants = runner.load_tasks(ROOT / 'evals/cases/agent_business_variants_v1.yaml')
    assert len(variants) == 12
    previous = {step['question'] for task in original for step in task['steps']}
    questions = [step['question'] for task in variants for step in task['steps']]
    assert len(questions) == len(set(questions)) and not previous.intersection(questions)
    assert {task['category'] for task in variants} == set(runner.CATEGORIES)


def test_resume_retains_failed_turns_and_the_last_independent_reference(tmp_path):
    import json
    task = {'steps': [{'question':'first'}, {'question':'followup'}, {'question':'next'}]}
    first = {'turn':1, 'question':'first', 'errors':['explanation_unverified']}
    second = {'turn':2, 'question':'followup', 'errors':[]}
    (tmp_path/'turn-1.result.json').write_text(json.dumps(first))
    (tmp_path/'turn-2.result.json').write_text(json.dumps(second))
    (tmp_path/'turn-1.expected.csv').write_text('gmv\n300\n')
    turns, expected = runner.restored_turns(tmp_path, task)
    assert turns == [first, second] and expected.iloc[0]['gmv'] == '300'
    task['steps'][1]['question'] = 'changed'
    with pytest.raises(ValueError, match='Saved question'):
        runner.restored_turns(tmp_path, task)


def test_fresh_twelve_task_suite_uses_declared_balanced_quota(tmp_path):
    import yaml
    suite = yaml.safe_load((ROOT / 'evals/cases/agent_business_tasks_v1.yaml').read_text())
    selected = {}
    for task in suite['tasks']:
        selected.setdefault((task['dataset'], task['category']), task)
    suite.update(tasks=list(selected.values()), tasks_per_category=1, tasks_per_workspace=6)
    path = tmp_path / 'fresh.yaml'
    path.write_text(yaml.safe_dump(suite, allow_unicode=True))
    assert len(runner.load_tasks(path)) == 12
    suite['tasks'].pop()
    path.write_text(yaml.safe_dump(suite, allow_unicode=True))
    with pytest.raises(ValueError):
        runner.load_tasks(path)


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


@pytest.mark.parametrize('field,wrong', [
    ('time_range', {'start':'2018-01-01','end':'2019-01-01'}),
    ('dimensions', ['customer_state']),
    ('filters', []),
    ('metric_ids', ['gmv']),
])
def test_completed_state_and_csv_artifact_must_have_matching_scope(field, wrong):
    scope = {'metric_ids':['freight_amount'], 'dimensions':[],
             'filters':[{'dimension':'customer_state','value':'SP'}],
             'time_range':{'start':'2018-01-01','end':'2018-04-01'}, 'time_grain':'month',
             'comparison_baseline':None, 'top_n':None}
    state = {**scope, 'status':'completed', 'filters':['customer_state=SP'],
             'active_plan':{'required_result_columns':['time','freight_amount']}}
    artifact = {'query_scope':scope,'result_columns':['time','freight_amount']}
    assert runner.check_active_result_scope(state, artifact) == []
    mismatch = {**artifact,'query_scope':{**scope,field:wrong}}
    assert runner.check_active_result_scope(state, mismatch) == ['artifact_scope:'+field]
    assert runner.check_active_result_scope({**state,'status':'failed'}, mismatch) == []


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


def test_payment_type_is_a_dimension_and_money_aligns_by_payment_type():
    actual = read_csv('payment_type,payment_amount\ncredit_card,20\nboleto,10\n')
    expected = pd.DataFrame({'payment_type':['boleto','credit_card'],'payment_amount':[Decimal('10'),Decimal('20')]})
    assert compare_frames(actual,expected) == []
    actual['payment_amount'] = [10,20]
    assert compare_frames(actual,expected) == ['numeric:payment_amount']


def test_independent_analysis_and_rendered_chart_check_values():
    expected = pd.DataFrame({"customer_state":["SP","RJ"],"gmv":[20,10]})
    assert expected_calculation(expected,"gmv","compare",[1,2]) == Decimal(10)
    assert runner.check_chart([{"type":"bar","x":["RJ","SP"],"y":[10,20]}],expected) == []
    assert runner.check_chart([{"type":"bar","x":["SP","RJ"],"y":[10,20]}],expected)
    assert runner.check_analyses({"operation":"sum","metric":"gmv"},{"analyses":[]},expected) == ["analysis_missing:sum"]


def test_two_window_task_accepts_independent_monthly_source_but_checks_both_half_totals_and_growth():
    expected=read_csv('time,gmv\n2024-01-01,60\n2024-07-01,120\n')
    monthly=pd.DataFrame({'time':pd.date_range('2024-01-01',periods=12,freq='MS'),'gmv':[10]*6+[20]*6})
    assert runner.check_acquisition_frame(monthly,expected,monthly.copy())==[]
    wrong=monthly.copy()
    wrong.loc[1,'gmv']=100
    assert runner.check_acquisition_frame(wrong,expected,monthly)
    target={'operation':'period_compare','metric':'gmv','current_window':['2024-07-01','2025-01-01'],
            'baseline_window':['2024-01-01','2024-07-01']}
    comparison={**{k:target[k] for k in ('current_window','baseline_window')},
                'current':120,'baseline':60,'difference':60,'relative_change_percent':100}
    payload={'analyses':[{'operation':'period_compare','status':'ready','metric_id':'gmv','comparisons':[comparison]}]}
    assert runner.check_analyses(target,payload,expected)==[]
    comparison['relative_change_percent']=50
    assert runner.check_analyses(target,payload,expected)==['analysis_numeric:relative_change_percent']
    assert runner.check_analyses(target,{'analyses':[]},expected)==['analysis_missing:period_compare']


def test_completed_request_without_semantic_review_is_not_task_success():
    assert runner.task_status([{"errors":[]}]) == "pending_review"
    assert runner.task_status([{"errors":[]}], "fail") == "failed"
    assert runner.task_status([{"errors":["numeric:gmv"]}],"pass") == "failed"
    assert runner.task_status([{"errors":[]}],"pass") == "passed"


def test_sse_answer_replaces_cumulative_component_updates_and_keeps_distinct_paragraphs():
    events = [{'rich':{'id':key,'type':'text','lifecycle':kind,'data':{'content':content}}}
              for key,kind,content in [('a','create','主'),('a','update','主要结论'),
                                       ('b','create','建'),('b','update','建议核查')]]
    answer, failures = runner.wire_answer(events)
    assert answer == '主要结论\n\n建议核查'
    assert failures == []


def test_saved_chart_reassessment_replaces_old_chart_error_without_erasing_product_failure(tmp_path):
    import json
    directory = tmp_path / 'o-basic-1-attempt-1'
    directory.mkdir()
    task = {'id':'o-basic-1','attempt':1,'turns':[{'turn':1,'answer':'',
        'errors':['chart_values','explanation_unverified'],
        'analysis_state':{'metric_ids':['gmv'],'dimensions':[],'time_grain':None,'top_n':None}}]}
    runner.write_json(directory / 'task.json',task)
    runner.write_json(tmp_path / 'tasks.json', [{'id': 'o-basic-1', 'steps': [
        {'question': '独立测试问题', 'reference': {'metric': 'gmv'}}]}])
    (directory / 'turn-1.expected.csv').write_text('gmv\n1\n')
    (directory / 'turn-1.csv').write_text('gmv\n1\n')
    runner.write_json(directory / 'turn-1.chart-rechecked.json',[{'type':'pie','values':[1]}])
    reviewed = runner.review_saved(tmp_path)
    assert reviewed['records'][0]['turns'][0]['errors'] == ['explanation_unverified']
    assert json.loads((directory / 'task.json').read_text()) == task


def test_saved_review_uses_actual_suite_snapshot_instead_of_default_sixty_tasks(tmp_path):
    import json
    task_id = 'fresh-independent-question'
    runner.write_json(tmp_path / 'tasks.json', [{'id': task_id, 'steps': [
        {'question': '新的问题', 'reference': {'metric': 'gmv'}}]}])
    runner.write_json(tmp_path / 'semantic-reviews.json', [{'id': task_id, 'attempt': 1, 'verdict': 'pass'}])
    directory = tmp_path / (task_id + '-attempt-1')
    directory.mkdir()
    record = {'id': task_id, 'attempt': 1, 'turns': [{'turn': 1, 'answer': '独立复核正文', 'errors': [],
        'analysis_state': {'metric_ids': ['gmv'], 'dimensions': [], 'time_grain': None, 'top_n': None}}]}
    runner.write_json(directory / 'task.json', record)
    for name in ['turn-1.expected.csv', 'turn-1.csv']:
        (directory / name).write_text('gmv\n1\n')
    assert runner.review_saved(tmp_path)['status_counts'] == {'passed': 1}
    assert json.loads((directory / 'task.json').read_text()) == record


def test_correct_csv_cannot_hide_a_stale_active_top_n_or_filter():
    reference = {"metric":"gmv","group":"product_category_name","top_n":5,"state":"SP"}
    state = {"metric_ids":["gmv"],"dimensions":["product_category_name"],"top_n":10,"time_grain":None,
        "filters":["customer_state=SP"]}
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
