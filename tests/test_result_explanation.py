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


def check(text, summary, refs=("/sample_rows/0",), kind="fact"):
    return check_answer(
        AnswerDraft.model_validate_json(answer(text, refs, kind)),
        parse_result_evidence(summary),
    )


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
    assert all(request.messages[-1].content == summary for request in requests)
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
    assert outcome.status == "accepted" and limits == [120, 20, 30, 20]
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
    )
    result = check("12月销售额20。", summary)
    assert not result.errors and result.doubts


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
