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
    usage = BudgetUsage(RequestBudget())
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
