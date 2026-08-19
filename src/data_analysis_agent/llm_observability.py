"""Bounded timeout and provider-usage evidence for project LLM calls."""

from __future__ import annotations

import asyncio
import json
from time import perf_counter
from typing import Any, AsyncGenerator

from vanna.core.llm import LlmRequest, LlmResponse, LlmService, LlmStreamChunk

from .budget import CURRENT_BUDGET


def _is_timeout_exception(exc: BaseException) -> bool:
    """Recognize asyncio, OpenAI SDK, and HTTP client timeout errors.

    Providers are OpenAI-compatible but do not necessarily expose the same
    exception class.  Keep optional imports local so this core wrapper remains
    usable with alternate providers and lightweight test environments.
    """
    timeout_types: list[type[BaseException]] = [asyncio.TimeoutError, TimeoutError]
    try:
        from openai import APITimeoutError

        timeout_types.append(APITimeoutError)
    except ImportError:
        pass
    try:
        import httpx

        timeout_types.append(httpx.TimeoutException)
    except ImportError:
        pass
    return isinstance(exc, tuple(timeout_types))


class ObservedLlmService(LlmService):
    """Wrap an LLM service with a request timeout and redacted run evidence.

    The wrapper does not store prompts or model output.  It records only status,
    duration, finish reason and whether the provider returned usage.  A timeout
    after a validated SQL result becomes a normal trusted-result completion;
    without such a result it becomes a safe query-timeout response.
    """

    def __init__(self, delegate: LlmService, *, timeout_seconds: float = 120.0):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.delegate = delegate
        self.timeout_seconds = float(timeout_seconds)
        self.model = getattr(delegate, "model", "unknown")

    async def send_request(self, request: LlmRequest) -> LlmResponse:
        usage = CURRENT_BUDGET.get()
        if usage is not None and usage.can_finalize_trusted_result():
            usage.mark_deterministic_result_finalized()
            usage.finish()
            return LlmResponse(
                content=_trusted_result_completion(usage.result_summary),
                finish_reason="trusted_result_finalized",
                metadata={"deterministic_result_finalized": True},
            )
        started = perf_counter()
        try:
            response = await asyncio.wait_for(
                self.delegate.send_request(request), timeout=self.timeout_seconds
            )
        except Exception as exc:
            if _is_timeout_exception(exc):
                return self._timeout_response(started)
            self._observe(
                status="error",
                started=started,
                usage_status="unknown",
                error_type=type(exc).__name__,
            )
            raise
        self._observe(
            status="completed",
            started=started,
            usage_status="reported" if response.usage is not None else "unknown",
            finish_reason=response.finish_reason,
        )
        usage = CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_usage(response.usage)
        return response

    async def stream_request(
        self, request: LlmRequest
    ) -> AsyncGenerator[LlmStreamChunk, None]:
        started = perf_counter()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async for chunk in self.delegate.stream_request(request):
                    yield chunk
        except Exception as exc:
            if _is_timeout_exception(exc):
                self._timeout_response(started)
                return
            self._observe(
                status="error",
                started=started,
                usage_status="unknown",
                error_type=type(exc).__name__,
            )
            raise
        self._observe(status="completed", started=started, usage_status="unknown")

    async def validate_tools(self, tools: list[Any]) -> list[str]:
        return await self.delegate.validate_tools(tools)

    def _timeout_response(self, started: float) -> LlmResponse:
        usage = CURRENT_BUDGET.get()
        self._observe(
            status="timeout",
            started=started,
            usage_status="unknown",
            error_type="llm_timeout",
        )
        if usage is not None and usage.result_contract_satisfied:
            usage.finish()
            return LlmResponse(
                content=(
                    "查询结果已经通过服务器结果合同；模型补充说明超时，"
                    "当前先返回已验证结果。"
                ),
                finish_reason="trusted_result_timeout",
            )
        if usage is not None:
            usage.terminate("query_timeout", "llm_timeout")
        return LlmResponse(
            content=(
                "模型响应超时，本轮未输出未经完整验证的数字结论。"
                "请缩小问题范围或稍后重试。"
            ),
            finish_reason="llm_timeout",
        )

    @staticmethod
    def _observe(
        *,
        status: str,
        started: float,
        usage_status: str,
        finish_reason: str | None = None,
        error_type: str | None = None,
    ) -> None:
        usage = CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_llm_observation(
                status=status,
                elapsed_ms=int((perf_counter() - started) * 1000),
                usage_status=usage_status,
                finish_reason=finish_reason,
                error_type=error_type,
            )


def _trusted_result_completion(summary: str | None) -> str:
    """Render only server-derived result metadata after a validated SQL result.

    The DataFrame component has already been emitted by the SQL tool.  This
    concise closing text deliberately avoids model-generated trend, currency,
    or causal claims that the result contract cannot prove.
    """
    payload: dict[str, Any] = {}
    prefix = "已通过结果合同的可信结果摘要："
    if summary and summary.startswith(prefix):
        try:
            decoded = json.loads(summary[len(prefix) :])
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            pass
    columns = [str(value) for value in payload.get("columns", [])[:16]]
    metric_ids = [str(value) for value in payload.get("metric_ids", [])[:16]]
    row_count = payload.get("row_count")
    lines = ["查询已完成，以上表格已通过服务器结果合同校验。"]
    if isinstance(row_count, int):
        lines.append(f"- 返回行数：{row_count}")
    if columns:
        lines.append(f"- 返回列：{', '.join(f'`{column}`' for column in columns)}")
    if metric_ids:
        lines.append(f"- 已核对指标：{', '.join(f'`{metric}`' for metric in metric_ids)}")
    lines.append("- 本轮未生成额外模型总结，避免对趋势、币种或因果作出超出结果证据的推断。")
    return "\n".join(lines)
