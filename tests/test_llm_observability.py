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
from vanna.core.llm import LlmRequest, LlmResponse, LlmService
from vanna.core.user import User


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
async def test_explicit_chart_path_keeps_provider_summary_available() -> None:
    usage = BudgetUsage(RequestBudget())
    usage.mark_result_contract_satisfied()
    usage.disable_deterministic_result_finalization()
    token = CURRENT_BUDGET.set(usage)
    try:
        response = await ObservedLlmService(ImmediateLlm(), timeout_seconds=1).send_request(
            _request()
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.content == "ok"
    assert usage.deterministic_result_finalized is False
    assert usage.llm_observations[0]["status"] == "completed"
