from __future__ import annotations

import asyncio

import pytest
from openai import APITimeoutError

from data_analysis_agent.budget import (
    BudgetUsage,
    CURRENT_BUDGET,
    RequestBudget,
)
from data_analysis_agent.llm_observability import ObservedLlmService
from vanna.core.llm import LlmRequest, LlmResponse, LlmService, LlmStreamChunk
from vanna.core.user import User
from vanna.core.llm import LlmMessage
from vanna.core.tool import ToolSchema
from data_analysis_agent.token_budget import ContextCapacityError


class ImmediateLlm(LlmService):
    model = "test-model"

    async def send_request(self, request: LlmRequest) -> LlmResponse:
        return LlmResponse(
            content="ok",
            finish_reason="stop",
            usage={"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
        )

    async def stream_request(self, request: LlmRequest):
        if False:
            yield None  # pragma: no cover

    async def validate_tools(self, tools) -> list[str]:
        return []


class MissingUsageLlm(ImmediateLlm):
    async def send_request(self, request: LlmRequest) -> LlmResponse:
        return LlmResponse(content="ok", finish_reason="stop")


class SlowLlm(ImmediateLlm):
    async def send_request(self, request: LlmRequest) -> LlmResponse:
        await asyncio.sleep(0.05)
        return await super().send_request(request)


class ProviderTimeoutLlm(ImmediateLlm):
    async def send_request(self, request: LlmRequest) -> LlmResponse:
        raise APITimeoutError(request=object())


class FailingLlm(ImmediateLlm):
    async def send_request(self, request: LlmRequest) -> LlmResponse:
        raise AssertionError("trusted result finalization must avoid the provider")


def _request() -> LlmRequest:
    return LlmRequest(messages=[], user=User(id="observability-test"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_fresh_planned_query_requires_sql_but_followup_does_not(stream):
    captured = []

    class Capturing(ImmediateLlm):
        async def send_request(self, request):
            captured.append(request)
            return LlmResponse(content="ok")

        async def stream_request(self, request):
            captured.append(request)
            yield LlmStreamChunk(content="ok")

    usage = BudgetUsage(RequestBudget())
    usage.set_query_plan({"metric_ids": ["gmv"]}, "已确认查询计划")
    request = _request().model_copy(update={"tools": [
        ToolSchema(name="run_sql", description="SQL", parameters={"type": "object"}),
        ToolSchema(name="other", description="other", parameters={"type": "object"}),
    ]})
    service = ObservedLlmService(Capturing())
    token = CURRENT_BUDGET.set(usage)
    try:
        if stream:
            async for _ in service.stream_request(request):
                pass
        else:
            await service.send_request(request)
        assert captured[0].tool_choice == "required"
        assert [t.name for t in captured[0].tools] == ["run_sql"]
        assert service._query_request(_request()).tool_choice == "auto"
        usage.sql_calls_used = 1
        assert service._query_request(request).tool_choice == "auto"
        assert request.tool_choice == "auto"  # no in-place request mutation
    finally:
        CURRENT_BUDGET.reset(token)
    from vanna.integrations.openai.llm import OpenAILlmService
    client = OpenAILlmService(model="test", api_key="test-only-not-a-secret")
    assert client._build_payload(captured[0])["tool_choice"] == "required"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_full_request_budget_includes_system_tools_and_output(stream):
    request = LlmRequest(messages=[LlmMessage(role="user", content="查询")], user=User(id="test"),
                         system_prompt="Catalog" * 1000, max_tokens=4000,
                         tools=[ToolSchema(name="run_sql", description="SQL" * 1000, parameters={"type": "object"})])
    usage = BudgetUsage(RequestBudget(max_prompt_tokens=5000, max_history_tokens=4000))
    token = CURRENT_BUDGET.set(usage)
    try:
        service = ObservedLlmService(FailingLlm(), timeout_seconds=1)
        with pytest.raises(ContextCapacityError):
            if stream:
                async for _ in service.stream_request(request):
                    pass
            else:
                await service.send_request(request)
    finally:
        CURRENT_BUDGET.reset(token)
    evidence = usage.catalog_trace["prompt_budgets"][0]
    assert evidence["prompt_tokens"] + evidence["output_reserve"] > 5000
    assert "Catalog" not in str(evidence) and "查询" not in str(evidence)


@pytest.mark.asyncio
async def test_summary_timeout_does_not_terminate_analysis_request():
    from data_analysis_agent.history_summary import build_llm_summary_provider, HistorySummary
    from vanna.core.storage import Message
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        turns = [[Message(role="user", content="之前讨论的目标")]]
        provider = build_llm_summary_provider(ObservedLlmService(SlowLlm(), timeout_seconds=0.001))
        result = await provider(turns, HistorySummary.from_turns(turns, source_turn_start=1, reason="message_budget"))
    finally:
        CURRENT_BUDGET.reset(token)
    assert result is None and usage.termination_reason == "running"


@pytest.mark.asyncio
async def test_observed_llm_records_reported_usage_and_duration() -> None:
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(ImmediateLlm(), timeout_seconds=1).send_request(
            _request()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.content == "ok"
    assert usage.total_tokens == 16
    assert len(usage.llm_observations) == 1
    assert usage.llm_observations[0]["status"] == "completed"
    assert usage.llm_observations[0]["usage_status"] == "reported"
    assert usage.llm_observations[0]["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_observed_llm_preserves_unknown_usage_status() -> None:
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        await ObservedLlmService(MissingUsageLlm(), timeout_seconds=1).send_request(
            _request()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert usage.total_tokens is None
    assert usage.llm_observations[0]["usage_status"] == "unknown"


@pytest.mark.asyncio
async def test_llm_timeout_without_trusted_result_is_safe_and_terminal() -> None:
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(SlowLlm(), timeout_seconds=0.001).send_request(
            _request()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.finish_reason == "llm_timeout"
    assert "未输出未经完整验证的数字结论" in (response.content or "")
    assert usage.termination_reason == "query_timeout"
    assert usage.error_type == "llm_timeout"
    assert usage.llm_observations[0]["timeout_phase"] == "first_byte"
    assert usage.llm_observations[0]["status"] == "timeout"
    assert usage.llm_observations[0]["usage_status"] == "unknown"


@pytest.mark.asyncio
async def test_llm_timeout_after_trusted_result_keeps_verified_result_available() -> None:
    usage = BudgetUsage(RequestBudget(deterministic_result_finalization=False))
    usage.mark_result_contract_satisfied()
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(SlowLlm(), timeout_seconds=0.001).send_request(
            _request()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.finish_reason == "trusted_result_timeout"
    assert "已验证结果" in (response.content or "")
    assert usage.termination_reason == "completed"
    assert usage.error_type is None
    assert usage.result_contract_satisfied is True


@pytest.mark.asyncio
async def test_provider_timeout_uses_the_same_safe_terminal_response() -> None:
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(
            ProviderTimeoutLlm(), timeout_seconds=1
        ).send_request(_request())
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.finish_reason == "llm_timeout"
    assert usage.termination_reason == "query_timeout"
    assert usage.error_type == "llm_timeout"
    assert usage.llm_observations == [
        {
            "status": "timeout",
            "elapsed_ms": usage.llm_observations[0]["elapsed_ms"],
            "usage_status": "unknown",
            "error_type": "llm_timeout",
            "timeout_phase": "provider",
        }
    ]


@pytest.mark.asyncio
async def test_validated_result_is_finalized_without_a_provider_summary() -> None:
    usage = BudgetUsage(RequestBudget())
    usage.mark_result_contract_satisfied()
    usage.set_result_summary(
        '已通过结果合同的可信结果摘要：{"metric_ids":['
        '"average_delivery_days","positive_review_rate"],'
        '"columns":["productcategoryname","averagedeliverydays",'
        '"positivereviewrate"],"row_count":75,'
        '"column_labels":{"productcategoryname":"商品品类",'
        '"averagedeliverydays":"平均履约天数",'
        '"positivereviewrate":"好评率"},"sample_rows":['
        '{"productcategoryname":"health_beauty","averagedeliverydays":2.5,'
        '"positivereviewrate":0.9187},'
        '{"productcategoryname":"computers","averagedeliverydays":3.25,'
        '"positivereviewrate":0.8}]}'
    )
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(FailingLlm(), timeout_seconds=1).send_request(
            _request()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.finish_reason == "trusted_result_finalized"
    content = response.content or ""
    assert "已返回 75 个分组，完整明细见上表" in content
    assert "| 商品品类 | 平均履约天数 | 好评率 |" in content
    assert "health_beauty | 2.50 天 | 91.87%" in content
    assert "不代表排名或趋势" in content
    assert "持续上升" not in content
    assert "最高" not in content
    assert "因为" not in content
    assert usage.deterministic_result_finalized is True
    assert usage.llm_observations == []


@pytest.mark.asyncio
async def test_trusted_result_finalization_falls_back_for_malformed_summary() -> None:
    usage = BudgetUsage(RequestBudget())
    usage.mark_result_contract_satisfied()
    usage.set_result_summary("not a trusted summary")
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(FailingLlm(), timeout_seconds=1).send_request(
            _request()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.finish_reason == "trusted_result_finalized"
    assert "未生成额外模型推断" in (response.content or "")
    assert usage.deterministic_result_finalized is True


@pytest.mark.asyncio
async def test_explicit_chart_path_uses_the_same_provider_free_trusted_summary() -> None:
    usage = BudgetUsage(RequestBudget())
    usage.mark_result_contract_satisfied()
    usage.set_result_summary(
        '已通过结果合同的可信结果摘要：{"metric_ids":["gmv"],'
        '"columns":["gmv"],"row_count":1,"column_labels":{"gmv":"商品成交额"},'
        '"sample_rows":[{"gmv":100}]}'
    )
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(FailingLlm(), timeout_seconds=1).send_request(
            _request()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.finish_reason == "trusted_result_finalized"
    assert "商品成交额" in (response.content or "")
    assert usage.deterministic_result_finalized is True
    assert usage.llm_observations == []


class PacedLlm(ImmediateLlm):
    def __init__(self, delays: list[tuple[float, str]]) -> None:
        self.delays = delays

    async def stream_request(self, request: LlmRequest):
        for delay, text in self.delays:
            await asyncio.sleep(delay)
            yield LlmStreamChunk(content=text)


@pytest.mark.asyncio
async def test_stream_first_byte_and_idle_timeouts_are_separate() -> None:
    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        first_byte = [
            chunk
            async for chunk in ObservedLlmService(
                PacedLlm([(0.05, "晚")]),
                first_byte_timeout_seconds=0.01,
                stream_idle_timeout_seconds=1,
            ).stream_request(_request())
        ]
    finally:
        CURRENT_BUDGET.reset(token)

    assert first_byte[-1].metadata["timeout_phase"] == "first_byte"
    assert usage.llm_observations[-1]["timeout_phase"] == "first_byte"

    usage = BudgetUsage(RequestBudget())
    token = CURRENT_BUDGET.set(usage)
    try:
        idle = [
            chunk
            async for chunk in ObservedLlmService(
                PacedLlm([(0.0, "先"), (0.05, "后")]),
                first_byte_timeout_seconds=1,
                stream_idle_timeout_seconds=0.01,
            ).stream_request(_request())
        ]
    finally:
        CURRENT_BUDGET.reset(token)

    assert idle[0].content == "先"
    assert idle[-1].metadata["timeout_phase"] == "stream_idle"
    assert usage.llm_observations[-1]["timeout_phase"] == "stream_idle"


@pytest.mark.asyncio
async def test_stream_waits_let_other_tasks_run() -> None:
    order: list[str] = []

    class OrderedLlm(ImmediateLlm):
        async def stream_request(self, request: LlmRequest):
            order.append("model-start")
            await asyncio.sleep(0.05)
            order.append("model-token")
            yield LlmStreamChunk(content="token")

    async def other() -> None:
        await asyncio.sleep(0.01)
        order.append("other-request")

    other_task = asyncio.create_task(other())
    chunks = [
        chunk
        async for chunk in ObservedLlmService(
            OrderedLlm(),
            first_byte_timeout_seconds=1,
            stream_idle_timeout_seconds=1,
        ).stream_request(_request())
    ]
    await other_task

    assert [chunk.content for chunk in chunks] == ["token"]
    assert order == ["model-start", "other-request", "model-token"]
