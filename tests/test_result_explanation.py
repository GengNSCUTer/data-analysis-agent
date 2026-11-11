import asyncio
import json

import pandas as pd
import pytest

from data_analysis_agent.result_evidence import build_result_evidence
from data_analysis_agent.result_explanation import (
    ANSWER_UNVERIFIED,
    AnswerDraft,
    check_answer,
    generate_result_answer,
    parse_result_evidence,
    resolve_evidence_ref,
    result_evidence_view,
)
from vanna.core.llm import LlmMessage, LlmRequest, LlmResponse
from vanna.core.user import User


@pytest.fixture
def summary():
    return build_result_evidence(
        pd.DataFrame({"state": ["SP", "RJ"], "gmv": [20, 10]}),
        required_columns=("state", "gmv"),
        metric_ids=("gmv",),
        additive_metric_ids=("gmv",),
        column_labels={"state": "客户州", "gmv": "销售额"},
        metric_semantics={"gmv": {"kind": "amount", "unit": "BRL"}},
    )


def answer(text, refs=("/sample_rows/0",), kind="fact"):
    return json.dumps(
        {"paragraphs": [{"text": text, "kind": kind, "evidence_refs": list(refs)}]},
        ensure_ascii=False,
    )


@pytest.mark.parametrize('text,invalid', [
    ('查询2023-01-01至2023-12-31（不含结束日）。', True),
    ('查询2023-01-01至2024-01-01（不含结束日）。', False),
    ('查询2023-01-01至2023-12-31（含结束日）。', False),
])
def test_explicit_interval_checks_inclusive_last_day_against_actual_scope(text, invalid):
    summary = build_result_evidence(pd.DataFrame({'n':[1]}), required_columns=('n',), metric_ids=('n',),
        query_scope={'time_range':{'start':'2023-01-01','end':'2024-01-01','end_semantics':'exclusive'}})
    checked = check_answer(AnswerDraft.model_validate_json(answer(text, ['/query_scope'])), parse_result_evidence(summary))
    assert any('time_boundary' in e for e in checked.errors) is invalid


@pytest.mark.asyncio
async def test_two_row_difference_is_available_before_authoring_without_a_revision():
    summary = build_result_evidence(pd.DataFrame({'department':['Men','Women'], 'inventory_item_count':[83958,84652]}),
        required_columns=('department','inventory_item_count'),metric_ids=('inventory_item_count',))
    text = 'Women比Men多694件。'
    draft = answer(text, ['/analyses/0'])
    result, requests = await run_answer(summary, [draft, '{"verdict":"supported","issues":[]}'])
    evidence = parse_result_evidence(requests[0].messages[-1].content)
    assert evidence['analyses'][0]['difference'] == -694
    assert evidence['analyses'][0]['reverse_comparison']['difference'] == 694
    assert result.status == 'accepted' and result.revision_count == 0


def test_mixed_observed_rate_and_cited_formula_threshold_needs_review_not_false_amount_error():
    summary = build_result_evidence(pd.DataFrame({'repeat_rate':[0.0304032]}), required_columns=('repeat_rate',),
        metric_ids=('repeat_rate',), metric_semantics={'repeat_rate':{'kind':'rate','unit':'比例0至1'}},
        metric_definitions={'repeat_rate':'同期至少2笔有效订单的客户数 / 至少1笔有效订单的客户数'})
    checked = check_answer(AnswerDraft.model_validate_json(answer('复购率为3.04%，指同期至少2笔有效订单的客户占比。',
        ['/sample_rows/0','/metric_definitions/repeat_rate'])), parse_result_evidence(summary))
    assert not checked.errors and any('formula_threshold' in d for d in checked.doubts)
    wrong = check_answer(AnswerDraft.model_validate_json(answer('复购率为2%。',
        ['/sample_rows/0','/metric_definitions/repeat_rate'])), parse_result_evidence(summary))
    assert wrong.errors


def test_zero_in_another_row_does_not_authorize_zero_rate_for_ceuta():
    summary = build_result_evidence(pd.DataFrame({'state':['Ceuta','A'], 'cancellation_rate':[0.5,0]}),
        required_columns=('state','cancellation_rate'),metric_ids=('cancellation_rate',),
        metric_semantics={'cancellation_rate':{'kind':'rate','unit':'比例0至1'}})
    bad = check_answer(AnswerDraft.model_validate_json(answer('Ceuta取消率为0%。', ['/sample_rows/0'])), parse_result_evidence(summary))
    assert any('numeric_mismatch' in e for e in bad.errors)


@pytest.mark.asyncio
async def test_catalog_metric_definition_is_evidence_for_formula_but_not_an_observed_value():
    summary = build_result_evidence(pd.DataFrame({"period_repeat_customer_rate": [0.2]}),
        required_columns=("period_repeat_customer_rate",), metric_ids=("period_repeat_customer_rate",),
        metric_definitions={"period_repeat_customer_rate": "同期至少2笔有效订单的客户数/至少1笔有效订单的客户数，不是终身老客率。"},
        metric_semantics={"period_repeat_customer_rate": {"kind": "rate", "unit": "比例0至1"}})
    draft = answer("期间复购客户率按同期至少2单的客户占至少1单客户的比例计算，不是终身老客率。",
                   refs=("/metric_definitions/period_repeat_customer_rate",))
    checked = check_answer(AnswerDraft.model_validate_json(draft), parse_result_evidence(summary))
    assert not checked.errors and checked.doubts
    outcome, requests = await run_answer(summary, [draft, '{"verdict":"supported","issues":[]}'])
    assert outcome.status == "accepted" and outcome.review_count == 1
    assert "metric_definitions" in requests[1].messages[-1].content
    # A definition's threshold must never become an unreviewed observed rate.
    wrong = answer("期间复购客户率是2。", refs=("/metric_definitions/period_repeat_customer_rate",))
    checked = check_answer(AnswerDraft.model_validate_json(wrong), parse_result_evidence(summary))
    assert checked.errors or checked.doubts


def test_unselected_metric_definitions_are_not_sent_to_result_explanation():
    summary = build_result_evidence(pd.DataFrame({"gmv": [20]}), required_columns=("gmv",), metric_ids=("gmv",),
                                   metric_definitions={"gmv": "有效商品行价格合计", "secret_metric": "不要泄漏"})
    assert parse_result_evidence(summary)["metric_definitions"] == {"gmv": "有效商品行价格合计"}
    wrong = answer("GMV为999。", refs=("/sample_rows/0", "/metric_definitions/gmv"))
    assert check_answer(AnswerDraft.model_validate_json(wrong), parse_result_evidence(summary)).errors


def check(text, summary, refs=("/sample_rows/0",), kind="fact"):
    return check_answer(
        AnswerDraft.model_validate_json(answer(text, refs, kind)),
        parse_result_evidence(summary),
    )


def test_calculated_growth_percent_uses_its_own_unit_even_when_metric_is_a_count():
    from data_analysis_agent.result_analysis import ResultInspection
    summary = build_result_evidence(pd.DataFrame({'time':['2017-01-01','2018-01-01'],
        'paid_order_count':[44379,53532]}),required_columns=('time','paid_order_count'),
        metric_ids=('paid_order_count',),time_column='time',
        query_scope={'time_grain':'year','time_range':{'start':'2017-01-01','end':'2019-01-01'}},
        metric_semantics={'paid_order_count':{'kind':'count','unit':'单'}},
        inspections=(ResultInspection(operation='period_compare',metric_id='paid_order_count'),))
    path='/analyses/0/comparisons/0/relative_change_percent'
    correct=check('增长约20.6%。',summary,[path])
    assert not correct.errors
    assert not any('numeric_calculation' in issue for issue in correct.doubts)
    wrong=check('增长约17.1%。',summary,[path])
    assert wrong.errors


@pytest.mark.asyncio
async def test_reverse_year_growth_cannot_use_absolute_forward_percentage_even_if_reviewer_agrees():
    from data_analysis_agent.result_analysis import ResultInspection
    summary=build_result_evidence(pd.DataFrame({'time':['2017','2018'],'paid_order_count':[44379,53532]}),
        required_columns=('time','paid_order_count'),metric_ids=('paid_order_count',),
        metric_semantics={'paid_order_count':{'kind':'count','unit':'单'}},
        inspections=(ResultInspection(operation='compare',metric_id='paid_order_count',row_numbers=(1,2)),))
    wrong=answer('2018年比2017年增长约17.1%。',refs=('/analyses/0',))
    right=answer('2018年比2017年增长约20.6%。',refs=('/analyses/0',))
    checked=check_answer(AnswerDraft.model_validate_json(wrong),parse_result_evidence(summary))
    assert checked.errors
    assert not check_answer(AnswerDraft.model_validate_json(right),parse_result_evidence(summary)).errors
    outcome,requests=await run_answer(summary,[wrong,right,'{"verdict":"supported","issues":[]}'])
    assert outcome.text=='2018年比2017年增长约20.6%。'
    assert outcome.revision_count==1 and len(requests)==3
    assert outcome.calls[0]['check_errors']
    assert outcome.calls[-1]['review_verdict']=='supported'


@pytest.mark.parametrize('decline',['下降约20%。','同比下降20%。','减少了20%。'])
def test_comparison_decline_can_use_positive_magnitude_without_becoming_positive_growth(decline):
    from data_analysis_agent.result_analysis import ResultInspection
    summary=build_result_evidence(pd.DataFrame({'gmv':[80,100]}),required_columns=('gmv',),metric_ids=('gmv',),
        inspections=(ResultInspection(operation='compare',metric_id='gmv',row_numbers=(1,2)),))
    assert not check(decline,summary,refs=('/analyses/0',)).errors
    assert check('增长约20%。',summary,refs=('/analyses/0',)).errors


async def run_answer(summary, responses):
    requests = []

    async def send(request):
        requests.append(request)
        response = responses[len(requests) - 1]
        if isinstance(response, BaseException):
            raise response
        return LlmResponse(content=response, finish_reason="stop")

    request = LlmRequest(
        user=User(id="owner"),
        messages=[
            LlmMessage(
                role="user",
                content="用户问题：请说明各州销售额，并给建议。范围：2017年",
            ),
            LlmMessage(role="user", content=summary),
        ],
        max_tokens=4000,
    )
    outcome = await generate_result_answer(
        request, send, lambda r: r, lambda: None, lambda: None
    )
    return outcome, requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["SP州销售额为20 BRL。", "**SP州**这次贡献了20雷亚尔。", "- SP：20 BRL"]
)
async def test_free_wording_is_returned_unchanged_without_review(summary, text):
    outcome, requests = await run_answer(summary, [answer(text)])
    assert outcome.text == text
    assert outcome.status == "accepted"
    assert len(requests) == 1 and outcome.review_count == outcome.revision_count == 0
    assert requests[0].tools is None and requests[0].tool_choice == "none"


@pytest.mark.parametrize(
    "text,refs",
    [
        ("SP销售额为10。", ["/sample_rows/0"]),
        ("RJ销售额为20。", ["/sample_rows/0"]),
        ("SP销售额为2。", ["/source_row_count"]),
        ("SP销售额为20。", ["/sample_rows/99"]),
        ("SP销售额为20。", ["/csv_sha256"]),
        ("SP销售额为20美元。", ["/sample_rows/0"]),
    ],
)
def test_definite_value_entity_reference_or_currency_errors(summary, text, refs):
    assert check(text, summary, refs).errors


@pytest.mark.parametrize(
    "metric,value,spec,text",
    [
        (
            "amount",
            2190452.45,
            {"kind": "amount", "unit": "BRL"},
            "SP销售额约219.05万BRL。",
        ),
        ("rate", 0.12345, {"kind": "rate", "unit": "比例0至1"}, "SP好评率为12.35%。"),
        ("days", 2.345, {"kind": "duration", "unit": "天"}, "SP履约时长为2.35天。"),
        ("orders", 20, {"kind": "count", "unit": "单"}, "SP有20单。"),
        ("amount", 1.005, {"kind": "amount", "unit": "CNY"}, "SP金额为1.01元。"),
    ],
)
def test_rounding_scales_rates_and_catalog_units_are_accepted(
    metric, value, spec, text
):
    summary = build_result_evidence(
        pd.DataFrame({"state": ["SP"], metric: [value]}),
        required_columns=("state", metric),
        metric_ids=(metric,),
        metric_semantics={metric: spec},
    )
    assert not check(text, summary).errors


def test_multiple_entities_and_unsupported_causes_are_doubts_not_language_gates(
    summary,
):
    assert check(
        "SP为10、RJ为20。", summary, ["/sample_rows/0", "/sample_rows/1"]
    ).doubts
    assert check(
        "SP销售额最高是因为促销。", summary, ["/statistics/gmv/maximum_row"]
    ).doubts


def test_a_matching_number_does_not_prove_a_negated_statement(summary):
    checked = check("SP销售额不是20 BRL。", summary)
    assert checked.doubts  # The number exists, but the proposition says its opposite.


def test_missing_second_entity_citation_is_reviewed_against_complete_evidence(summary):
    checked = check("SP销售额20，RJ销售额10。", summary, ["/sample_rows/0"])
    assert not checked.errors and checked.doubts


def test_preview_facts_are_distinct_from_model_rows_and_selection_limits():
    summary = build_result_evidence(pd.DataFrame({"gmv": range(250)}),
        required_columns=("gmv",), metric_ids=("gmv",), preview_rows=200)
    data = parse_result_evidence(summary)
    assert data["presentation"] == {"total_row_count": 250, "preview_row_count": 200, "display_truncated": True}
    assert data["selection"]["limit"] == 50
    assert not check("前端预览200行，完整查询结果250行。", summary, ["/presentation"]).errors
    assert check("销售额为200。", summary, ["/presentation"]).errors


def test_multi_entity_comparison_missing_one_citation_is_reviewed_not_a_swap(summary):
    checked = check("RJ销售额为10，与SP相比有差距。", summary, ["/sample_rows/1"])
    assert not checked.errors and checked.doubts


def test_computed_comparison_inherits_metric_currency_and_goes_to_review():
    from data_analysis_agent.result_analysis import ResultInspection

    summary = build_result_evidence(
        pd.DataFrame({"month": ["2017-11", "2017-12"], "gmv": [1003862.14, 742183.79]}),
        required_columns=("month", "gmv"),
        metric_ids=("gmv",),
        metric_semantics={"gmv": {"kind": "amount", "unit": "BRL"}},
        inspections=(
            ResultInspection(operation="compare", metric_id="gmv", row_numbers=(2, 1)),
        ),
    )
    checked = check("两个月差额为-261678.35 BRL。", summary, ["/analyses/0"])
    assert not checked.errors and checked.doubts


def test_non_currency_unit_conversion_is_reviewed_not_rejected():
    summary = build_result_evidence(
        pd.DataFrame({"state": ["SP"], "days": [2]}),
        required_columns=("state", "days"),
        metric_ids=("days",),
        metric_semantics={"days": {"kind": "duration", "unit": "天"}},
    )
    checked = check("SP的履约时长为48小时。", summary)
    assert not checked.errors and any(
        "unit_conversion" in issue for issue in checked.doubts
    )


def test_scaled_yuan_cannot_replace_catalog_currency(summary):
    assert any(
        "unit：" in issue for issue in check("SP销售额0.002万元。", summary).errors
    )


def test_currency_description_can_reference_catalog_without_an_observed_number(summary):
    refs = ['/metric_semantics/gmv', '/scope']
    assert not check('销售额单位为巴西雷亚尔（BRL）。', summary, refs).errors
    assert check('销售额单位为人民币。', summary, refs).errors
    scoped = check('本表金额单位为BRL。', summary, ['/scope'])
    assert not scoped.errors and scoped.doubts


def test_invalid_reference_feedback_identifies_the_path_to_repair(summary):
    path = '/statistics/gmv/advertising_effect'
    errors = check('目前无法说明广告带来了销售增长。', summary, [path]).errors
    assert any(path in issue for issue in errors)


@pytest.mark.asyncio
async def test_result_metric_and_column_metadata_can_support_a_limitations_explanation(summary):
    data = parse_result_evidence(summary)
    assert resolve_evidence_ref(data, '/metric_ids') == ['gmv']
    assert resolve_evidence_ref(data, '/columns') == ['state', 'gmv']
    text = '当前结果只有州与销售额，没有广告投放数据，不能据此确认广告造成增长。'
    draft = answer(text, refs=['/metric_ids', '/columns'])
    assert not check_answer(AnswerDraft.model_validate_json(draft), data).errors
    outcome, requests = await run_answer(summary, [draft, '{"verdict":"supported","issues":[]}'])
    assert outcome.status == 'accepted' and outcome.text == text
    assert outcome.review_count == 1 and outcome.revision_count == 0 and len(requests) == 2
    assert check('销售额为123。', summary, ['/metric_ids', '/columns']).errors


def test_partition_rank_position_cannot_be_used_as_a_money_observation():
    from data_analysis_agent.result_explanation import _numbers
    frame = pd.DataFrame({'time': ['2024-01-01'] * 2, 'state': ['SP', 'RJ'], 'gmv': [20, 10]})
    summary = build_result_evidence(frame, required_columns=frame.columns, metric_ids=('gmv',),
        metric_semantics={'gmv': {'kind': 'amount', 'unit': 'BRL'}}, time_column='time', top_n=2,
        query_scope={'dimensions': ['state'], 'time_grain': 'month'})
    data = parse_result_evidence(summary)
    assert _numbers(1, '/analyses/0/rows/0/rank', data)[0][1] == '__count__'
    wrong = check('销售额为1 BRL。', summary, ['/analyses/0/rows/0/rank'])
    assert wrong.errors


def test_named_metric_must_match_the_cited_metric():
    summary = build_result_evidence(
        pd.DataFrame({"gmv": [20], "orders": [20]}),
        required_columns=("gmv", "orders"),
        metric_ids=("gmv", "orders"),
        column_labels={"gmv": "销售额", "orders": "订单数"},
    )
    checked = check("销售额为20。", summary, ["/sample_rows/0/orders"])
    assert any("metric：" in issue for issue in checked.doubts)


@pytest.mark.asyncio
async def test_semantic_review_repairs_entity_swap_and_rechecks_once(summary):
    original = answer("SP为10、RJ为20。", ["/sample_rows/0", "/sample_rows/1"])
    revised = answer("SP为20、RJ为10。", ["/sample_rows/0", "/sample_rows/1"])
    outcome, requests = await run_answer(
        summary,
        [
            original,
            '{"verdict":"revise","issues":["两个州的金额对调了"]}',
            revised,
            '{"verdict":"supported","issues":[]}',
        ],
    )
    assert outcome.text == "SP为20、RJ为10。"
    assert outcome.review_count == 2 and outcome.revision_count == 1
    assert len(requests) == 4
    assert (
        original in requests[1].messages[-2].content
        or "SP为10" in requests[1].messages[-2].content
    )
    assert all(request.tools is None for request in requests)
    assert all(request.messages[-1].content == requests[0].messages[-1].content for request in requests)
    assert parse_result_evidence(requests[0].messages[-1].content)['sample_rows'] == parse_result_evidence(summary)['sample_rows']
    assert all("2017" in request.messages[0].content for request in requests)


@pytest.mark.asyncio
async def test_obvious_error_gets_only_one_revision_not_an_extra_judge(summary):
    outcome, requests = await run_answer(
        summary, [answer("SP为10。"), answer("SP为20。")]
    )
    assert outcome.text == "SP为20。"
    assert (
        len(requests) == 2 and outcome.review_count == 0 and outcome.revision_count == 1
    )


@pytest.mark.asyncio
async def test_revision_keeps_original_semantic_doubt_even_when_draft_also_has_a_code_error(
    summary,
):
    outcome, requests = await run_answer(
        summary,
        [
            answer("SP销售额为10，是因为促销。"),
            answer("SP销售额为20 BRL。"),
            '{"verdict":"supported","issues":[]}',
        ],
    )
    assert outcome.status == "accepted"
    assert [r.metadata["purpose"] for r in requests] == [
        "result_answer_draft",
        "result_answer_revision",
        "result_answer_review",
    ]
    assert outcome.review_count == 1 and outcome.revision_count == 1


@pytest.mark.asyncio
async def test_revision_failure_never_exposes_bad_text_or_business_templates(summary):
    outcome, requests = await run_answer(
        summary, [answer("SP为987654321。"), answer("SP为987654321。")]
    )
    assert outcome.text == ANSWER_UNVERIFIED and "987654321" not in outcome.text
    assert outcome.status == "unverified" and len(requests) == 2
    assert "最高" not in outcome.text and "合计" not in outcome.text


@pytest.mark.asyncio
async def test_malformed_draft_can_be_revised_once(summary):
    outcome, requests = await run_answer(summary, ["not json", answer("SP为20。")])
    assert outcome.text == "SP为20。" and len(requests) == 2


@pytest.mark.asyncio
async def test_json_fence_does_not_consume_a_revision(summary):
    outcome, requests = await run_answer(
        summary, ["```json\n" + answer("SP销售额20。") + "\n```"]
    )
    assert outcome.text == "SP销售额20。" and len(requests) == 1


def test_multiline_list_ordinals_are_not_business_numbers(summary):
    result = check(
        "## 1. 州数据\n1. SP销售额20。\n2. RJ销售额10。",
        summary,
        ["/sample_rows/0", "/sample_rows/1"],
    )
    assert not result.errors and result.doubts


def test_scaled_row_count_is_reviewed_without_a_parse_error(summary):
    data = parse_result_evidence(summary)
    data.update(source_row_count=10000, row_count=10000)
    draft = AnswerDraft.model_validate_json(
        answer("完整结果有1万行。", ["/source_row_count"])
    )
    result = check_answer(draft, data)
    assert not result.errors and result.doubts


def test_uncited_group_count_is_reviewed_rather_than_a_fake_amount_error(summary):
    result = check(
        "所有2个州合计30 BRL。", summary, ["/statistics/gmv/sum_of_returned_rows"]
    )
    assert not result.errors and result.doubts


@pytest.mark.parametrize('unit', ['种', '类'])
def test_result_category_count_is_not_an_amount_even_when_the_author_cites_rows(summary, unit):
    result = check(f'共有2{unit}客户州，SP销售额20、RJ销售额10。', summary,
                   ['/sample_rows/0', '/sample_rows/1'])
    assert not result.errors and result.doubts


@pytest.mark.asyncio
async def test_followup_fetches_named_metric_definitions_without_adding_observed_metrics():
    from types import SimpleNamespace
    from data_analysis_agent.analysis_state import AnalysisState
    from data_analysis_agent.budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.question_router import QuestionRouter
    from data_analysis_agent.runtime_paths import RESOURCES
    from data_analysis_agent.runtime_workspace import load_runtime_workspace
    from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
    catalog = CatalogLoader(load_runtime_workspace(RESOURCES / 'workspaces/olist.yaml').profile).load()
    summary = build_result_evidence(pd.DataFrame({'payment_amount':[20]}),
        required_columns=('payment_amount',), metric_ids=('payment_amount',),
        metric_definitions={'payment_amount':catalog.metrics_by_id['payment_amount'].description})
    sent = []
    async def send(request):
        sent.append(request)
        return LlmResponse(content='示例已核验解释')
    handler = BudgetedChatHandler(SimpleNamespace(_send_llm_request=send), RequestBudget(), None,
        question_router=QuestionRouter(CatalogRetriever(catalog)))
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        _ = [piece async for piece in handler._tool_free_answer(
            SimpleNamespace(message='为什么支付金额不能直接当GMV？', conversation_id='c', request_id='r'),
            SimpleNamespace(intent='result_followup'),
            AnalysisState(previous_result_summary=summary), User(id='owner',group_memberships=['analyst']))]
    finally:
        CURRENT_BUDGET.reset(token)
    evidence = parse_result_evidence(sent[0].messages[-1].content)
    assert evidence['metric_ids'] == ['payment_amount'] and evidence['columns'] == ['payment_amount']
    assert evidence['metric_definitions']['gmv'] == catalog.metrics_by_id['gmv'].description
    assert evidence['sample_rows'] == [{'payment_amount':20}]


@pytest.mark.asyncio
async def test_review_timeout_keeps_results_but_withholds_answer(summary):
    outcome, requests = await run_answer(
        summary,
        [
            answer("SP增长是因为促销。"),
            TimeoutError(),
        ],
    )
    assert outcome.text == ANSWER_UNVERIFIED and len(requests) == 2
    assert outcome.review_count == 1 and outcome.revision_count == 0


@pytest.mark.asyncio
async def test_review_and_revision_have_separate_timeouts_and_never_enable_tools(
    summary, monkeypatch
):
    monkeypatch.setattr("data_analysis_agent.result_explanation.perf_counter", lambda: 100.0)
    limits = []
    wait = asyncio.wait_for

    async def observed_wait(awaitable, timeout):
        limits.append(timeout)
        return await wait(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", observed_wait)
    outcome, requests = await run_answer(
        summary,
        [
            answer("SP为10、RJ为20。", ["/sample_rows/0", "/sample_rows/1"]),
            '{"verdict":"revise","issues":["金额对调"]}',
            answer("SP为20、RJ为10。", ["/sample_rows/0", "/sample_rows/1"]),
            '{"verdict":"supported","issues":[]}',
        ],
    )
    assert outcome.status == "accepted" and limits == [60, 20, 30, 20]
    assert len(requests) == 4 and requests[2].max_tokens == 2048
    assert all(r.tools is None and not r.stream for r in requests)


@pytest.mark.asyncio
async def test_review_context_overflow_does_not_drop_evidence_or_publish_draft(summary):
    from data_analysis_agent.token_budget import ContextCapacityError

    sent = []

    async def send(request):
        sent.append(request)
        return LlmResponse(content=answer("因为促销，SP销售额20。"))

    def prepare(request):
        if request.metadata["purpose"] == "result_answer_review":
            raise ContextCapacityError("not enough space")
        return request

    request = LlmRequest(
        user=User(id="owner"),
        messages=[
            LlmMessage(role="user", content="why"),
            LlmMessage(role="user", content=summary),
        ],
    )
    outcome = await generate_result_answer(
        request, send, prepare, lambda: None, lambda: None
    )
    assert outcome.text == ANSWER_UNVERIFIED and len(sent) == 1
    assert outcome.review_count == 0 and outcome.revision_count == 0


@pytest.mark.asyncio
async def test_cancelled_call_propagates_without_repair(summary):
    with pytest.raises(asyncio.CancelledError):
        await run_answer(summary, [asyncio.CancelledError()])


@pytest.mark.asyncio
async def test_hypothetical_numbers_are_reviewed_not_treated_as_observed_amounts(
    summary,
):
    text = "假设下月预算提高10%，建议先做小规模实验验证效果。"
    checked = check(text, summary, [], "recommendation")
    assert not checked.errors and checked.doubts
    outcome, requests = await run_answer(
        summary,
        [
            answer(text, [], "recommendation"),
            '{"verdict":"supported","issues":[]}',
        ],
    )
    assert outcome.text == text and len(requests) == 2


@pytest.mark.asyncio
async def test_pure_suggestion_needs_no_citation_or_judge(summary):
    text = "建议先访谈业务同事，再考虑是否增加投放。"
    outcome, requests = await run_answer(summary, [answer(text, [], "recommendation")])
    assert outcome.text == text and len(requests) == 1


@pytest.mark.asyncio
async def test_top_n_whole_population_claim_is_reviewed(summary):
    data = parse_result_evidence(summary)
    data.update(top_n=2, scope="returned_top_n")
    summary = summary.split("：", 1)[0] + "：" + json.dumps(data)
    text = "全国销售额合计30 BRL。"
    revised = "本次前2名销售额合计30 BRL，不代表全部业务总额。"
    outcome, requests = await run_answer(
        summary,
        [
            answer(text, ["/statistics/gmv/sum_of_returned_rows"]),
            '{"verdict":"revise","issues":["这是前N项的合计"]}',
            answer(revised, ["/statistics/gmv/sum_of_returned_rows"]),
            '{"verdict":"supported","issues":[]}',
        ],
    )
    assert outcome.text == revised and len(requests) == 4


@pytest.mark.parametrize(
    "text", ["两个州并列，不能说SP唯一最高。", "全年是否逐月增长，还不能确定。"]
)
def test_ties_and_trends_need_semantic_review(summary, text):
    assert check(text, summary, ["/statistics/gmv/maximum_row"]).doubts


def test_unknown_currency_cannot_be_silently_declared():
    summary = build_result_evidence(
        pd.DataFrame({"state": ["SP"], "gmv": [20]}),
        required_columns=("state", "gmv"),
        metric_ids=("gmv",),
        metric_semantics={"gmv": {"kind": "amount", "unit": "来源金额单位未声明"}},
    )
    assert check("SP销售额20美元。", summary).errors
    assert not check("SP销售额20，币种尚未声明。", summary).errors


def test_reference_resolves_only_current_supplied_rows_and_computed_facts(summary):
    data = parse_result_evidence(summary)
    assert resolve_evidence_ref(data, "/sample_rows/0") == {"state": "SP", "gmv": 20}
    assert resolve_evidence_ref(data, "/statistics/gmv/sum_of_returned_rows") == 30
    with pytest.raises(ValueError):
        resolve_evidence_ref(data, "/sample_rows/-1")
    with pytest.raises(ValueError):
        resolve_evidence_ref(data, "/csv_sha256")


def test_scalar_amount_references_keep_entity_binding(summary):
    assert not check("SP销售额20。", summary, ["/sample_rows/0/gmv"]).errors
    assert check("RJ销售额20。", summary, ["/sample_rows/0/gmv"]).errors
    assert not check("SP销售额最高为20。", summary, ["/statistics/gmv/maximum"]).errors
    assert not check("SP销售额BRL20。", summary, ["/sample_rows/0/gmv"]).errors
    assert check("SP销售额BRL10。", summary, ["/sample_rows/0/gmv"]).errors


def test_month_reference_is_not_mistaken_for_an_amount():
    summary = build_result_evidence(
        pd.DataFrame({"time": ["2017-12-01T00:00:00"], "gmv": [20]}),
        required_columns=("time", "gmv"),
        metric_ids=("gmv",),
        query_scope={"time_grain": "month"},
    )
    result = check("12月销售额20。", summary)
    assert not result.errors and not result.doubts
    assert check("11月销售额20。", summary).doubts


def monthly_summary():
    return build_result_evidence(
        pd.DataFrame({"time": ["2017-01-01", "2017-11-01", "2017-12-01"],
                      "gmv": [120098.27, 1003862.14, 742183.79]}),
        required_columns=("time", "gmv"), metric_ids=("gmv",), time_column="time",
        query_scope={"time_grain": "month", "time_range": {"start": "2017-01-01", "end": "2018-01-01"}},
        metric_semantics={"gmv": {"kind": "amount", "unit": "BRL"}},
    )


@pytest.mark.parametrize("wrong,right", [
    ("从1月到11月增长约518%。", "从1月到11月增长约735.9%。"),
    ("11月比12月高约26.1%。", "11月比12月高约35.3%。"),
    ("12月比11月下降约35.3%。", "12月比11月下降约26.1%。"),
])
def test_percentage_is_bound_to_named_months_and_comparison_direction(wrong, right):
    summary = monthly_summary()
    refs = ["/sample_rows", "/analyses/0"]
    assert any("comparison_binding" in error for error in check(wrong, summary, refs).errors)
    assert not check(right, summary, refs).errors


def test_two_different_months_cannot_both_be_called_annual_peak():
    summary = monthly_summary()
    assert any("ranking_binding" in error for error in check(
        "11月和12月都达到全年峰值。", summary, ["/sample_rows"]).errors)
    assert not check("11月达到全年峰值。", summary, ["/statistics/gmv/maximum_row"]).errors
    # A denial or a question is not an affirmative peak claim.
    assert not check("12月不是全年峰值。", summary, ["/sample_rows"]).errors
    assert not any("ranking_binding" in error for error in check(
        "从1月开始观察，全年峰值出现在11月。", summary, ["/sample_rows"]).errors)


@pytest.mark.asyncio
async def test_wrong_month_percentage_is_revised_even_if_all_numbers_exist_elsewhere():
    wrong = answer("从1月到11月增长约518%。", ["/sample_rows", "/analyses/0"])
    right = answer("从1月到11月增长约735.9%。", ["/sample_rows", "/analyses/0"])
    outcome, requests = await run_answer(monthly_summary(), [wrong, right, '{"verdict":"supported","issues":[]}'])
    assert outcome.status == "accepted" and "735.9%" in outcome.text
    assert [r.metadata["purpose"] for r in requests] == [
        "result_answer_draft", "result_answer_revision", "result_answer_review"]


@pytest.mark.asyncio
async def test_year_and_row_value_with_proven_scope_need_only_one_generation():
    summary = build_result_evidence(pd.DataFrame({"state": ["SP"], "gmv": [20]}),
        required_columns=("state", "gmv"), metric_ids=("gmv",),
        query_scope={"time_range": {"start": "2017-01-01", "end": "2018-01-01"}})
    outcome, requests = await run_answer(summary, [answer("2017年SP销售额为20。")])
    assert outcome.status == "accepted" and outcome.review_count == 0 and len(requests) == 1
    assert check("2018年SP销售额为20。", summary).doubts


@pytest.mark.asyncio
async def test_all_time_is_not_a_top_n_population_sum_claim():
    summary = build_result_evidence(pd.DataFrame({"state": ["SP"], "gmv": [20]}),
        required_columns=("state", "gmv"), metric_ids=("gmv",), top_n=1)
    outcome, requests = await run_answer(summary, [answer(
        "全部时间销售额最高的是SP州，为20。", ["/statistics/gmv/maximum_row"])])
    assert outcome.status == "accepted" and len(requests) == 1
    assert check("全部州销售额合计20。", summary).doubts


def test_month_row_does_not_prove_an_annual_amount_even_inside_the_right_year():
    summary = monthly_summary()
    assert check("2017年销售额为120098.27 BRL。", summary).doubts


def test_multiple_months_and_values_do_not_prove_their_pairing():
    summary = monthly_summary()
    swapped = check("1月销售额742183.79 BRL，12月销售额120098.27 BRL。", summary,
                    ["/sample_rows/0", "/sample_rows/2"])
    assert swapped.errors or swapped.doubts


@pytest.mark.asyncio
async def test_review_excludes_untrusted_narrative_but_keeps_question_and_exact_evidence(summary):
    requests = []
    async def send(request):
        requests.append(request)
        return LlmResponse(content=answer("SP增长可能来自促销。") if len(requests) == 1
                           else '{"verdict":"supported","issues":[]}')
    request = LlmRequest(user=User(id="owner"), messages=[
        LlmMessage(role="user", content="为什么有差异？\n叙事上下文（不是执行条件或结果事实）：旧回答里有广告增长999%"),
        LlmMessage(role="user", content=summary)])
    outcome = await generate_result_answer(request, send, lambda r: r, lambda: None, lambda: None)
    assert outcome.status == "accepted" and outcome.review_count == 1
    assert "旧回答" in requests[0].messages[0].content
    assert requests[1].messages[0].content == "为什么有差异？"
    assert requests[1].messages[-1].content == requests[0].messages[-1].content
    assert parse_result_evidence(requests[1].messages[-1].content)['sample_rows'] == parse_result_evidence(summary)['sample_rows']


@pytest.mark.asyncio
async def test_delivery_time_direction_is_reviewed_and_revised_without_exposing_bad_prose():
    summary = build_result_evidence(pd.DataFrame({"delivery_days": [4.016275834]}),
        required_columns=("delivery_days",), metric_ids=("delivery_days",),
        metric_definitions={"delivery_days": "已完成订单送达时间减创建时间的平均天数，排除负时长"})
    refs = ["/sample_rows/0", "/metric_definitions/delivery_days"]
    wrong = answer("平均为4.02天，送达时间不晚于创建时间。", refs)
    right = answer("平均为4.02天，排除送达早于创建的负时长订单。", refs)
    outcome, requests = await run_answer(summary, [wrong,
        '{"verdict":"revise","issues":["非负时长要求送达不早于创建，原文先后方向反了"]}',
        right, '{"verdict":"supported","issues":[]}'])
    assert outcome.status == "accepted" and outcome.text == "平均为4.02天，排除送达早于创建的负时长订单。"
    assert outcome.revision_count == 1 and outcome.review_count == 2 and len(requests) == 4


def test_formatted_display_citation_is_bound_to_its_original_numeric_row(summary):
    assert not check("SP销售额20 BRL。", summary, ["/formatted_metrics/0"]).errors
    assert check("SP销售额10 BRL。", summary, ["/formatted_metrics/0"]).errors
    assert check("RJ销售额20 BRL。", summary, ["/formatted_metrics/0"]).errors
    assert not check("SP销售额20 BRL。", summary, ["/statistics/gmv/maximum_display"]).errors
    assert check("SP销售额10 BRL。", summary, ["/statistics/gmv/maximum_display"]).errors


@pytest.mark.parametrize('ref', ['/formatted_metrics', '/sample_rows', '/statistics', '/analyses'])
def test_parent_evidence_references_never_crash_and_do_not_accept_fabricated_values(summary, ref):
    if ref != '/analyses':
        assert not check('SP销售额20、RJ销售额10。', summary, [ref]).errors
    checked = check('SP销售额999。', summary, [ref])
    assert checked.errors or checked.doubts


@pytest.mark.asyncio
async def test_parent_formatted_reference_can_finish_without_internal_error(summary):
    draft = answer('SP销售额20、RJ销售额10。', refs=['/formatted_metrics'])
    outcome, requests = await run_answer(summary, [draft, '{"verdict":"supported","issues":[]}'])
    assert outcome.status == 'accepted' and outcome.revision_count == 0 and len(requests) == 2


@pytest.mark.asyncio
async def test_missing_difference_can_be_computed_once_without_sql_or_unchecked_math(summary):
    request = json.dumps({'paragraphs': [], 'calculations': [
        {'operation':'compare', 'metric_id':'gmv', 'row_numbers':[1,2]}]})
    fixed = answer('SP比RJ多10 BRL。', ['/analyses/0'])
    outcome, requests = await run_answer(summary, [request, fixed,
        '{"verdict":"supported","issues":[]}'])
    assert outcome.status == 'accepted' and outcome.text == 'SP比RJ多10 BRL。'
    assert outcome.revision_count == 0 and len(requests) <= 3
    evidence = parse_result_evidence(outcome.evidence_summary)
    assert evidence['analyses'][0]['difference'] == 10
    assert evidence['analyses'][0]['operands'] == [{'state':'SP','gmv':20},{'state':'RJ','gmv':10}]
    assert all(not request.tools for request in requests)


@pytest.mark.asyncio
async def test_calculation_rejects_unknown_metric_and_cannot_loop_or_use_partial_rows(summary):
    for operation in [
        {'operation':'compare','metric_id':'unknown','row_numbers':[1,2]},
        {'operation':'compare','metric_id':'gmv','row_numbers':[1,99]},
        {'operation':'sum','metric_id':'gmv'},
    ]:
        data = parse_result_evidence(summary)
        data['coverage'] = 'selected_rows'
        partial = '已通过结果合同的可信结果摘要：' + json.dumps(data)
        calculate = json.dumps({'paragraphs':[], 'calculations':[operation]})
        outcome, requests = await run_answer(partial, [calculate, calculate])
        assert outcome.status == 'unverified' and outcome.evidence_summary is None
        assert len(requests) == 2 and outcome.revision_count == 1
        assert not outcome.calls[0]['check_errors']
        assert 'calculation_unavailable' in outcome.calls[0]['check_missing_evidence'][0]


@pytest.mark.asyncio
async def test_local_explanation_cannot_sum_a_nonadditive_metric():
    summary = build_result_evidence(pd.DataFrame({'aov':[20,10]}),
        required_columns=('aov',), metric_ids=('aov',))
    calculate = json.dumps({'paragraphs':[], 'calculations':[{'operation':'sum','metric_id':'aov'}]})
    outcome, requests = await run_answer(summary, [calculate, calculate])
    assert outcome.status == 'unverified' and len(requests) == 2
    assert not any(a['operation']=='sum' for a in parse_result_evidence(outcome.evidence_summary)['analyses'])


def test_global_minimum_is_not_proven_by_a_top_n_extreme_reference():
    summary = build_result_evidence(pd.DataFrame({'state':['A','B'], 'gmv':[20,10]}),
        required_columns=('state','gmv'), metric_ids=('gmv',), top_n=2)
    assert check('全年最低成交额为10。', summary, ['/statistics/gmv/minimum']).errors
    assert not check('在返回的前2名中，最低成交额为10。', summary, ['/statistics/gmv/minimum']).errors
    assert not check('不能由前2名判断全年最低成交额。', summary, ['/scope']).errors


def test_calendar_month_count_in_formula_explanation_is_not_a_business_amount():
    summary = build_result_evidence(pd.DataFrame({'average_order_value': [86.80044148686305]}),
        required_columns=('average_order_value',), metric_ids=('average_order_value',),
        query_scope={'time_range': {'start':'2023-01-01', 'end':'2024-01-01'}},
        metric_semantics={'average_order_value': {'kind':'mean', 'unit':'来源金额单位每单'}})
    checked = check('直接平均12个月的月均值会让每个月权重相同，不能代表全年客单价。',
                    summary, ['/sample_rows/0'])
    assert not checked.errors and checked.doubts
    assert check('全年客单价为12。', summary, ['/sample_rows/0']).errors


@pytest.mark.parametrize('text', [
    '12月为69单，比1月的31单增长约122.6%。',
    '从1月的31单增长至12月的69单，增幅约122.6%。',
    '12月有69单，较1月31单增加了约122.6%。',
    '12月相比1月增长约122.6%。',
    '与1月的31单相比，12月达到69单，增长约122.6%。',
    '1月为31单，与之相比，12月达到69单，增长约122.6%。',
    '12月达到峰值69单，较1月增长约122.6%。',
    '12月达到69单，比1月增加约122.6%。',
])
def test_month_comparison_with_intervening_values_uses_semantic_direction(text):
    summary = build_result_evidence(pd.DataFrame({'time':['2023-01-01','2023-12-01'],
        'completed_order_count':[31,69]}), required_columns=('time','completed_order_count'),
        metric_ids=('completed_order_count',), time_column='time', query_scope={'time_grain':'month'},
        metric_semantics={'completed_order_count':{'kind':'count','unit':'单'}})
    assert not check(text, summary, ['/sample_rows', '/analyses/0']).errors


@pytest.mark.asyncio
async def test_month_value_list_is_verified_pairwise_without_a_redundant_review():
    summary = build_result_evidence(pd.DataFrame({'time':['2023-01-01','2023-02-01'], 'orders':[31,26]}),
        required_columns=('time','orders'), metric_ids=('orders',), time_column='time',
        query_scope={'time_grain':'month'}, metric_semantics={'orders':{'kind':'count','unit':'单'}})
    draft = answer('1月31单，2月26单。', ['/sample_rows'])
    outcome, requests = await run_answer(summary, [draft])
    assert outcome.status == 'accepted' and len(requests) == 1
    wrong = check('1月26单，2月31单。', summary, ['/sample_rows'])
    assert any('row_value_binding' in error for error in wrong.errors)


@pytest.mark.asyncio
async def test_year_introduction_does_not_invalidate_pairwise_verified_month_values():
    summary = build_result_evidence(pd.DataFrame({'time': ['2022-01-01', '2022-02-01'], 'orders': [26, 13]}),
        required_columns=('time', 'orders'), metric_ids=('orders',), time_column='time',
        query_scope={'time_grain': 'month', 'time_range': {'start': '2022-01-01', 'end': '2023-01-01'}},
        metric_semantics={'orders': {'kind': 'count', 'unit': '单'}})
    text = '2022年各月订单数如下：1月26单，2月13单。'
    outcome, requests = await run_answer(summary, [answer(text, ['/sample_rows'])])
    assert outcome.status == 'accepted' and outcome.text == text and len(requests) == 1
    # A valid year and number pool must not certify an annual number or another year.
    assert check('2022年合计26单。', summary, ['/sample_rows']).doubts
    assert check('2023年各月订单数：1月26单，2月13单。', summary, ['/sample_rows']).doubts
    assert check('2022年：1月13单，2月26单。', summary, ['/sample_rows']).errors


@pytest.mark.asyncio
async def test_returned_total_month_range_is_verified_against_half_open_scope():
    summary = build_result_evidence(pd.DataFrame({'time': pd.date_range('2022-01-01', periods=12, freq='MS'),
        'orders': [26, 13, 9, 27, 20, 19, 31, 26, 21, 32, 23, 25]}),
        required_columns=('time', 'orders'), metric_ids=('orders',), additive_metric_ids=('orders',),
        time_column='time', query_scope={'time_grain': 'month',
            'time_range': {'start': '2022-01-01', 'end': '2023-01-01'}},
        metric_semantics={'orders': {'kind': 'count', 'unit': '单'}})
    ref = '/statistics/orders/sum_of_returned_rows'
    text = '全年订单总数（1月至12月）为272单。'
    outcome, requests = await run_answer(summary, [answer(text, [ref])])
    assert outcome.status == 'accepted' and len(requests) == 1
    assert check('1月至11月合计272单。', summary, [ref]).doubts
    assert check('2021年1月至12月合计272单。', summary, [ref]).doubts
    assert check('1月至12月合计31单。', summary, [ref]).errors
    assert check('1月至12月合计26单。', summary, ['/sample_rows/0/orders']).doubts


@pytest.mark.asyncio
async def test_month_extrema_parentheses_are_bound_to_the_actual_rows_and_extremes():
    summary = build_result_evidence(pd.DataFrame({'time': ['2022-03-01', '2022-07-01', '2022-10-01'],
        'orders': [9, 31, 32]}), required_columns=('time', 'orders'), metric_ids=('orders',),
        time_column='time', query_scope={'time_grain': 'month'},
        metric_semantics={'orders': {'kind': 'count', 'unit': '单'}})
    refs = ['/statistics/orders/maximum_row', '/statistics/orders/minimum_row']
    text = '最高值出现在10月（32单），最低值出现在3月（9单）。'
    outcome, requests = await run_answer(summary, [answer(text, refs)])
    assert outcome.status == 'accepted' and len(requests) == 1
    wrong = check('最高值出现在7月（31单），最低值出现在3月（9单）。', summary, refs)
    assert any('ranking_binding' in error for error in wrong.errors)
    assert check('最高值出现在10月（31单）。', summary, refs).errors


def test_overview_evidence_labels_monotonicity_without_denying_endpoint_growth():
    summary = build_result_evidence(pd.DataFrame({'time': pd.date_range('2023-01-01', periods=4, freq='MS'),
        'orders': [31, 26, 41, 69]}), required_columns=('time', 'orders'), metric_ids=('orders',),
        time_column='time', query_scope={'time_grain': 'month'},
        metric_semantics={'orders': {'kind': 'count', 'unit': '单'}})
    group = parse_result_evidence(summary)['analyses'][0]['groups'][0]
    assert group['monotonicity'] == {'predicate': 'every_period_strictly_increases', 'verdict': 'false'}
    assert group['endpoints']['difference'] == 38
    checked = check('从1月的31单到4月的69单，上升约122.6%，但并非每月增长。', summary, ['/analyses/0'])
    assert not checked.errors and checked.doubts


def test_rate_trend_keeps_percentage_points_separate_from_relative_growth():
    summary = build_result_evidence(
        pd.DataFrame({"time": ["2017-02-01", "2017-03-01"], "rate": [0.8195329087, 0.7553405885]}),
        required_columns=("time", "rate"), metric_ids=("rate",), time_column="time",
        metric_semantics={"rate": {"kind": "rate", "unit": "比例0至1"}},
        query_scope={"time_grain": "month", "time_range": {"start": "2017-02-01", "end": "2017-04-01"}})
    refs = ["/analyses/0/groups/0/largest_drop"]
    assert not check("3月环比下降6.42个百分点。", summary, refs).errors
    assert check("3月环比下降7.83个百分点。", summary, refs).errors
    assert not check("3月环比下降7.83%。", summary, refs).errors
    assert check("金额为7.83。", summary, refs).errors or check("金额为7.83。", summary, refs).doubts


def test_rate_level_after_a_decline_is_not_the_decline_percentage():
    summary = build_result_evidence(pd.DataFrame({"time": ["2017-01-01", "2017-12-01"], "rate": [0.0332, 0.0113]}),
        required_columns=("time", "rate"), metric_ids=("rate",), time_column="time",
        query_scope={"time_grain": "month"}, metric_semantics={"rate": {"kind": "rate", "unit": "比例0至1"}})
    text = "整体呈下降趋势：从1月的3.32%下降至12月的1.13%，首尾相对降幅约65.96%。"
    assert not check(text, summary, ["/analyses/0/groups/0/endpoints"]).errors
    assert check("从1月到12月下降了1.13%。", summary, ["/analyses/0/groups/0/endpoints"]).errors


@pytest.mark.asyncio
async def test_all_answer_phases_share_one_deadline(summary, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr("data_analysis_agent.result_explanation.perf_counter", lambda: elapsed[0])
    limits = []
    wait = asyncio.wait_for
    async def observed_wait(awaitable, timeout):
        limits.append(timeout)
        result = await wait(awaitable, timeout)
        elapsed[0] += 25
        return result
    monkeypatch.setattr(asyncio, "wait_for", observed_wait)
    outcome, _ = await run_answer(summary, [
        answer("SP为10、RJ为20。", ["/sample_rows/0", "/sample_rows/1"]),
        '{"verdict":"revise","issues":["金额对调"]}',
        answer("SP为20、RJ为10。", ["/sample_rows/0", "/sample_rows/1"]),
        '{"verdict":"supported","issues":[]}',
    ])
    assert outcome.status == "accepted" and limits == [60, 20, 30, 15]


def test_chinese_negative_sign_is_not_a_value_error():
    summary = build_result_evidence(
        pd.DataFrame({"state": ["SP"], "gmv": [-10]}),
        required_columns=("state", "gmv"),
        metric_ids=("gmv",),
    )
    assert not check("SP金额为负10。", summary).errors


def test_non_additive_metrics_cannot_cite_a_nonexistent_group_sum():
    summary = build_result_evidence(
        pd.DataFrame({"aov": [20, 10]}), required_columns=("aov",), metric_ids=("aov",)
    )
    result = check("客单价合计30。", summary, ["/statistics/aov/sum_of_returned_rows"])
    assert any("invalid_reference" in issue for issue in result.errors)


def test_maximum_outside_samples_is_still_valid_evidence():
    summary = build_result_evidence(
        pd.DataFrame({"state": [f"州{i}" for i in range(250)], "gmv": range(250)}),
        required_columns=("state", "gmv"),
        metric_ids=("gmv",),
        max_tokens=2000,
    )
    result = check("州249销售额最高，为249。", summary, ["/statistics/gmv/maximum_row"])
    assert not result.errors


def test_provenance_keeps_original_scope_and_actual_model_row_count(summary):
    from data_analysis_agent.budget import BudgetUsage, RequestBudget
    from data_analysis_agent.chat_runtime import BudgetedChatHandler

    usage = BudgetUsage(RequestBudget())
    usage.set_result_summary(summary)
    view = result_evidence_view(summary)
    view.update(
        source_row_count=250,
        row_count=250,
        included_row_count=20,
        coverage="selected_rows",
    )
    usage.record_catalog(
        {
            "result_evidence_view": view,
            "result_inspection": {
                "query_scope": {
                    "time_range": {"start": "2017-01-01", "end": "2018-01-01"},
                    "filters": [{"dimension": "state", "value": "SP"}],
                }
            },
        }
    )
    content = BudgetedChatHandler._trusted_result_evidence_chunk("c", "r", usage).rich[
        "data"
    ]["description"]
    assert "完整查询结果250行" in content and "解释依据含20行明细" in content
    assert "2017-01-01（含）至2018-01-01（不含）" in content and "客户州=SP" in content


def test_empty_top_n_mapping_is_not_a_top_n_query():
    summary = build_result_evidence(
        pd.DataFrame({"gmv": [20]}),
        required_columns=("gmv",),
        metric_ids=("gmv",),
        top_n={},
    )
    assert parse_result_evidence(summary)["top_n"] is None
