import json

import pandas as pd
import pytest

from data_analysis_agent.result_evidence import build_result_evidence
from data_analysis_agent.result_explanation import render_result_answer
from data_analysis_agent.result_explanation import result_evidence_view


@pytest.fixture
def summary():
    return build_result_evidence(
        pd.DataFrame({"state": ["SP", "RJ"], "gmv": [20, 10]}),
        required_columns=("state", "gmv"), metric_ids=("gmv",), additive_metric_ids=("gmv",),
        column_labels={"state": "客户州", "gmv": "销售额"},
    )


@pytest.mark.parametrize("content", [
    "SP销售额10，RJ销售额20。", "SP销售额为2。", "SP销售额为987654321。",
    '{"facts":[{"kind":"row","row_number":1,"value":10}]}',
    '{"facts":[{"kind":"row","row_number":1}],"text":"SP销售额10"}',
    '{"facts":[{"kind":"row","row_number":999}]}',
    '{"facts":[{"kind":"maximum","metric_id":"source_row_count"}]}',
    '{"facts":[{"kind":"sum","metric_id":"unknown"}]}',
    '{"facts":[{"kind":"row","row_number":true}]}',
    '{"facts":[]}', 'not JSON',
])
def test_model_cannot_supply_values_entities_or_use_metadata_as_metrics(summary, content):
    answer = render_result_answer(content, summary)
    assert "客户州：SP，销售额：20。" in answer
    assert "客户州：RJ，销售额：10。" in answer
    assert "客户州：SP，销售额：10。" not in answer
    assert "客户州：RJ，销售额：20。" not in answer
    assert "987654321" not in answer
    assert '"facts"' not in answer


def test_reference_binds_maximum_to_its_entity(summary):
    answer = render_result_answer('{"facts":[{"kind":"maximum","metric_id":"gmv"}]}', summary)
    assert answer.startswith("客户州：SP的销售额最高，为20。")
    assert "RJ" not in answer


def test_requested_sum_is_the_first_conclusion(summary):
    answer = render_result_answer('{"facts":[{"kind":"sum","metric_id":"gmv"},{"kind":"row","row_number":1}]}', summary)
    assert answer.splitlines() == ["这份结果的销售额合计：30。", "客户州：SP，销售额：20。"]


def test_invalid_reference_gets_conclusion_before_supporting_rows(summary):
    answer = render_result_answer('not JSON', summary)
    assert answer.startswith("客户州：SP的销售额最高，为20。")
    assert "完整结果共" not in answer


def test_top_n_scope_is_not_rendered_as_business_total(summary):
    data = json.loads(summary.split("：", 1)[1])
    data.update(top_n=2, scope="returned_top_n")
    answer = render_result_answer('{"facts":[{"kind":"sum","metric_id":"gmv"}]}',
                                  summary.split("：", 1)[0] + "：" + json.dumps(data))
    assert answer.startswith("这份结果的销售额合计：30。")
    assert "不代表全部业务总额" in answer


def test_requested_rows_preserve_source_order_and_include_all_ten():
    summary = build_result_evidence(pd.DataFrame({"category": [f"类别{i}" for i in range(10)], "gmv": range(10)}),
        required_columns=("category", "gmv"), metric_ids=("gmv",), top_n=10)
    response = json.dumps({"facts": [{"kind": "row", "row_number": n} for n in range(1, 11)]})
    answer = render_result_answer(response, summary)
    assert len(answer.splitlines()) == 11  # ten facts and the Top-N scope note
    assert [f"类别{i}" in line for i, line in enumerate(answer.splitlines()[:10])] == [True] * 10


def test_sum_uses_statistic_not_row_count(summary):
    answer = render_result_answer('{"facts":[{"kind":"sum","metric_id":"gmv"}]}', summary)
    assert "销售额合计：30" in answer
    assert "完整结果" not in answer


def test_extreme_tie_is_not_claimed_unique():
    summary = build_result_evidence(pd.DataFrame({"state": ["SP", "RJ"], "gmv": [20, 20]}),
        required_columns=("state", "gmv"), metric_ids=("gmv",))
    answer = render_result_answer('{"facts":[{"kind":"maximum","metric_id":"gmv"}]}', summary)
    assert "2组并列" in answer
    assert "唯一" not in answer


def test_only_explicitly_available_rows_can_be_referenced(summary):
    payload = json.loads(summary.split("：", 1)[1])
    payload.update(sample_rows=payload["sample_rows"][1:], row_numbers=[2], included_row_count=1)
    summary = summary.split("：", 1)[0] + "：" + json.dumps(payload)
    # Source row 1 was not supplied in sample_rows. It cannot be selected as a row.
    answer = render_result_answer('{"facts":[{"kind":"row","row_number":1}]}', summary)
    assert answer != "客户州：SP，销售额：20。"
    assert "客户州：RJ，销售额：10。" in answer


@pytest.mark.parametrize("metric,value,spec,wanted", [
    ("new_amount", 1.005, {"kind":"amount", "unit":"BRL"}, "1.01 BRL"),
    ("thelook_rate", 0.12345, {"kind":"rate", "unit":"比例0至1"}, "12.35%"),
    ("days_to_sale", 2.345, {"kind":"duration", "unit":"天"}, "2.35 天"),
    ("orders", 20, {"kind":"count", "unit":"单"}, "20 单"),
    ("amount", 20000, {"kind":"amount", "unit":"来源金额单位未声明"}, "20000.00"),
])
def test_catalog_formats_values_without_model_units(metric, value, spec, wanted):
    summary = build_result_evidence(pd.DataFrame({"state":["01"], metric:[value]}),
        required_columns=("state",metric), metric_ids=(metric,), metric_semantics={metric:spec})
    answer = render_result_answer('{"facts":[{"kind":"row","row_number":1}]}', summary)
    assert f"state：01，{metric}：{wanted}。" == answer
    assert "人民币" not in answer


def test_evidence_card_counts_are_separate_from_business_numbers(summary):
    data = json.loads(summary.split("：", 1)[1])
    data.update(source_row_count=250, row_count=250, sample_rows=data["sample_rows"][:1],
                row_numbers=[1], included_row_count=1, coverage="selected_rows")
    summary = summary.split("：", 1)[0] + "：" + json.dumps(data)
    assert result_evidence_view(summary)["source_row_count"] == 250
    assert result_evidence_view(summary)["included_row_count"] == 1
    answer = render_result_answer('{"facts":[{"kind":"row","row_number":1}]}', summary)
    assert "250" not in answer and "完整结果" not in answer


def test_unknown_currency_or_model_unit_conversion_is_not_adopted(summary):
    answer = render_result_answer('{"facts":[{"kind":"row","row_number":1,"unit":"万元"}]}', summary)
    assert "万元" not in answer
    assert "客户州：SP，销售额：20" in answer


def test_provenance_card_shows_source_and_selected_counts_without_result_values(summary):
    from data_analysis_agent.budget import BudgetUsage, RequestBudget
    from data_analysis_agent.chat_runtime import BudgetedChatHandler

    usage = BudgetUsage(RequestBudget())
    usage.set_result_summary(summary)
    view = result_evidence_view(summary)
    view.update(source_row_count=250, row_count=250, included_row_count=20, coverage="selected_rows")
    usage.record_catalog({"result_evidence_view":view, "result_inspection":{
        "metric_ids":["gmv"], "columns":["state","gmv"], "dataset_version":"v1", "metric_version":"m1",
    }})
    content = BudgetedChatHandler._trusted_result_evidence_chunk("c", "r", usage).rich["data"]["description"]
    assert "完整查询结果250行" in content and "解释依据含20行明细" in content
    assert "重新核验" in content and "不能仅从样本外推整体" in content
    assert "SP" not in content and "RJ" not in content
    assert "客户州" in content and "销售额" in content


def test_reused_evidence_card_keeps_original_query_time_and_filter(summary):
    from data_analysis_agent.budget import BudgetUsage, RequestBudget
    from data_analysis_agent.chat_runtime import BudgetedChatHandler

    usage = BudgetUsage(RequestBudget())
    usage.set_result_summary(summary)
    usage.record_catalog({"result_inspection": {"query_scope":{
        "time_range":{"start":"2017-01-01", "end":"2018-01-01"},
        "filters":[{"dimension":"state", "value":"SP"}],
    }}, "query_plan":{"time_range":{"start":"2020-01-01", "end":"2021-01-01"}}})
    content = BudgetedChatHandler._trusted_result_evidence_chunk("c", "r", usage).rich["data"]["description"]
    assert "2017-01-01（含）至2018-01-01（不含）" in content
    assert "客户州=SP" in content and "2020-01-01" not in content


def test_non_additive_metric_cannot_be_summed_by_reference():
    summary = build_result_evidence(pd.DataFrame({"state":["SP","RJ"], "average_order_value":[20,10]}),
        required_columns=("state","average_order_value"), metric_ids=("average_order_value",))
    answer = render_result_answer('{"facts":[{"kind":"sum","metric_id":"average_order_value"}]}', summary)
    assert "合计" not in answer and "30" not in answer


def test_maximum_reference_can_use_full_result_extreme_outside_samples():
    frame = pd.DataFrame({"state":[f"州{i}" for i in range(250)], "gmv":range(250)})
    summary = build_result_evidence(frame, required_columns=("state","gmv"), metric_ids=("gmv",), max_tokens=2000)
    payload = json.loads(summary.split("：", 1)[1])
    assert payload["included_row_count"] < 250
    assert not any(row["state"] == "州249" for row in payload["sample_rows"])
    answer = render_result_answer('{"facts":[{"kind":"maximum","metric_id":"gmv"}]}', summary)
    assert "州249" in answer and "249" in answer


def test_empty_top_n_mapping_is_not_a_top_n_query():
    summary = build_result_evidence(pd.DataFrame({"gmv":[20]}), required_columns=("gmv",),
        metric_ids=("gmv",), top_n={})
    data = json.loads(summary.split("：", 1)[1])
    assert data["top_n"] is None and data["scope"] == "returned_result"
    answer = render_result_answer('{"facts":[{"kind":"row","row_number":1}]}', summary)
    assert "前N项" not in answer
