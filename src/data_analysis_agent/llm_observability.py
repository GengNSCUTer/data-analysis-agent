"""Bounded timeout and provider-usage evidence for project LLM calls."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from time import perf_counter
from typing import Any, AsyncGenerator

from vanna.core.llm import LlmMessage, LlmRequest, LlmResponse, LlmService, LlmStreamChunk

from .budget import CURRENT_BUDGET
from .token_budget import ContextCapacityError, TokenCounter
from .result_explanation import ANSWER_UNVERIFIED, generate_result_answer, result_evidence_view


class LlmPhaseTimeout(TimeoutError):
    """A project timeout for one phase of a model call."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(phase)


def _timeout_phase(exc: BaseException) -> str | None:
    """Classify a timeout as connect, first byte, or stream idle."""

    if isinstance(exc, LlmPhaseTimeout):
        return exc.phase
    try:
        import httpx

        if isinstance(exc, httpx.ConnectTimeout):
            return "connect"
        if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
            return "stream_idle"
    except ImportError:
        pass
    if _is_timeout_exception(exc):
        return "provider"
    return None


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

    def __init__(
        self,
        delegate: LlmService,
        *,
        connect_timeout_seconds: float = 10.0,
        first_byte_timeout_seconds: float = 120.0,
        stream_idle_timeout_seconds: float = 30.0,
        timeout_seconds: float | None = None,
        capacity=None,
    ):
        if timeout_seconds is not None:
            first_byte_timeout_seconds = timeout_seconds
            stream_idle_timeout_seconds = timeout_seconds
        for name, value in (
            ("connect_timeout_seconds", connect_timeout_seconds),
            ("first_byte_timeout_seconds", first_byte_timeout_seconds),
            ("stream_idle_timeout_seconds", stream_idle_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.delegate = delegate
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.first_byte_timeout_seconds = float(first_byte_timeout_seconds)
        self.stream_idle_timeout_seconds = float(stream_idle_timeout_seconds)
        self.model = getattr(delegate, "model", "unknown")
        self.token_counter = TokenCounter()
        self.capacity = capacity

    @asynccontextmanager
    async def _permit(self):
        if self.capacity is None:
            yield
            return
        async with self.capacity.slot() as wait_ms:
            usage = CURRENT_BUDGET.get()
            if usage is not None:
                waits = list((usage.catalog_trace or {}).get("model_queue_ms", []))
                usage.record_catalog({"model_queue_ms": [*waits, wait_ms]})
            yield

    def _check_context(self, request: LlmRequest) -> None:
        usage = CURRENT_BUDGET.get()
        if usage is None:
            return
        prompt_tokens = self.token_counter.count_request(request)
        reserve = request.max_tokens or usage.budget.max_output_tokens
        evidence = {
            "purpose": request.metadata.get("purpose", "agent"),
            "prompt_tokens": prompt_tokens, "output_reserve": reserve,
            "limit": usage.budget.max_prompt_tokens,
            "tokenizer_mode": "local_tokenizer_estimate" if self.token_counter.mode == "exact" else self.token_counter.mode,
            "tokenizer_id": self.token_counter.tokenizer_id,
        }
        calls = list((usage.catalog_trace or {}).get("prompt_budgets", []))
        usage.record_catalog({"prompt_budgets": [*calls, evidence]})
        if prompt_tokens + reserve > usage.budget.max_prompt_tokens:
            raise ContextCapacityError("当前问题、状态与工具定义超过上下文预算，请拆分分析。")

    def _query_request(self, request: LlmRequest) -> LlmRequest:
        """A planned fresh query must execute SQL, never quote a prior answer."""
        usage = CURRENT_BUDGET.get()
        if usage is not None and usage.query_plan and usage.sql_calls_used == 0:
            sql_tools = [tool for tool in (request.tools or []) if tool.name == "run_sql"]
            if sql_tools:
                return request.model_copy(update={"tools": sql_tools, "tool_choice": "required"})
        return request

    async def _ensure_context(self, request):
        request = self._fit_result_request(request)
        try:
            self._check_context(request)
        except ContextCapacityError:
            usage = CURRENT_BUDGET.get()
            rebuild = request._rebuild_context
            if usage is None or usage.context_compactor is None or rebuild is None:
                raise
            await usage.context_compactor()
            request = self._query_request(await rebuild())
            self._check_context(request)
        return request

    def _fit_result_request(self, request: LlmRequest) -> LlmRequest:
        """Use actual system/message/tool sizes, rather than a fixed result allowance."""
        from .result_evidence import RESULT_EVIDENCE_PREFIX, resize_result_evidence
        usage = CURRENT_BUDGET.get()
        if usage is None or request.metadata.get("purpose") not in {"tool_free_response", "result_answer_draft"}:
            return request
        if self.token_counter.count_request(request) + (request.max_tokens or usage.budget.max_output_tokens) <= usage.budget.max_prompt_tokens:
            if request.messages and (request.messages[-1].content or "").startswith(RESULT_EVIDENCE_PREFIX):
                usage.record_catalog({"result_evidence_view": result_evidence_view(request.messages[-1].content)})
            return request
        for index, message in enumerate(request.messages):
            text = message.content or ""
            start = text.find(RESULT_EVIDENCE_PREFIX)
            if start < 0:
                continue
            _, length = json.JSONDecoder().raw_decode(text[start + len(RESULT_EVIDENCE_PREFIX):])
            end = start + len(RESULT_EVIDENCE_PREFIX) + length
            messages = list(request.messages)
            messages[index] = message.model_copy(update={"content": text[:start] + text[end:]})
            without = request.model_copy(update={"messages": messages})
            available = usage.budget.max_prompt_tokens - (request.max_tokens or usage.budget.max_output_tokens) - self.token_counter.count_request(without) - 32
            # Evidence is JSON embedded inside a message; serialized quotes cost
            # additional tokens. Measure that actual request, not just its text.
            while True:
                fitted = resize_result_evidence(text[start:end], available, self.token_counter)
                messages[index] = message.model_copy(update={"content": text[:start] + fitted + text[end:]})
                candidate = request.model_copy(update={"messages": list(messages)})
                excess = self.token_counter.count_request(candidate) + (request.max_tokens or usage.budget.max_output_tokens) - usage.budget.max_prompt_tokens
                if excess <= 0:
                    break
                available -= excess
            usage.record_catalog({"result_request_resize": {"evidence_tokens": self.token_counter.count_text(fitted), "available": available}})
            usage.record_catalog({"result_evidence_view": result_evidence_view(fitted)})
            return candidate
        return request

    async def send_request(self, request: LlmRequest) -> LlmResponse:
        usage = CURRENT_BUDGET.get()
        followup = request.metadata.get("purpose") == "tool_free_response" and request.metadata.get("intent") == "result_followup"
        if not followup and (usage is None or not usage.can_finalize_trusted_result()):
            # Compaction can itself call the model. Do not hold a provider
            # permit while waiting for that nested summary call.
            request = await self._ensure_context(self._query_request(request))
        async with self._permit():
            return await self._send_request(request)

    async def _send_request(self, request: LlmRequest) -> LlmResponse:
        usage = CURRENT_BUDGET.get()
        if request.metadata.get("purpose") == "tool_free_response" and request.metadata.get("intent") == "result_followup":
            return await self._explain_result(request, usage)
        if usage is not None and usage.can_finalize_trusted_result():
            if usage.result_summary and '"coverage":' in usage.result_summary:
                return await self._explain_result(request, usage)
            usage.mark_deterministic_result_finalized()
            usage.finish()
            return LlmResponse(
                content=ANSWER_UNVERIFIED,
                finish_reason="trusted_result_finalized",
                metadata={"deterministic_result_finalized": True},
            )
        started = perf_counter()
        try:
            response = await asyncio.wait_for(
                self.delegate.send_request(request),
                timeout=self.first_byte_timeout_seconds,
            )
        except TimeoutError:
            if request.metadata.get("purpose") == "history_semantic_summary":
                self._observe(status="timeout", started=started, usage_status="unknown", error_type="TimeoutError")
                return LlmResponse(content="", finish_reason="llm_timeout")
            return self._timeout_response(started, phase="first_byte")
        except Exception as exc:
            phase = _timeout_phase(exc)
            if phase is not None:
                if request.metadata.get("purpose") == "history_semantic_summary":
                    self._observe(status="timeout", started=started, usage_status="unknown", error_type=type(exc).__name__)
                    return LlmResponse(content="", finish_reason="llm_timeout")
                return self._timeout_response(started, phase=phase)
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
        usage = CURRENT_BUDGET.get()
        if usage is None or not usage.can_finalize_trusted_result():
            request = await self._ensure_context(self._query_request(request))
        async with self._permit():
            iterator = self._stream_request(request)
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                await iterator.aclose()

    async def _stream_request(self, request: LlmRequest) -> AsyncGenerator[LlmStreamChunk, None]:
        usage = CURRENT_BUDGET.get()
        if usage is not None and usage.can_finalize_trusted_result():
            if usage.result_summary and '"coverage":' in usage.result_summary:
                response = await self._explain_result(request, usage)
                yield LlmStreamChunk(content=response.content, finish_reason=response.finish_reason,
                                     metadata=response.metadata)
                return
            usage.mark_deterministic_result_finalized()
            usage.finish()
            yield LlmStreamChunk(
                content=ANSWER_UNVERIFIED,
                finish_reason="trusted_result_finalized",
                metadata={"deterministic_result_finalized": True},
            )
            return
        started = perf_counter()
        iterator = self.delegate.stream_request(request).__aiter__()
        reported_usage = None
        try:
            try:
                chunk = await self._next_stream_chunk(
                    iterator, self.first_byte_timeout_seconds, "first_byte"
                )
            except StopAsyncIteration:
                self._observe(status="completed", started=started, usage_status="unknown")
                return
            except Exception as exc:
                timeout_chunk = self._timeout_chunk(started, exc)
                if timeout_chunk is None:
                    raise
                yield timeout_chunk
                return
            reported_usage = _chunk_usage(chunk)
            yield chunk
            while True:
                try:
                    chunk = await self._next_stream_chunk(
                        iterator, self.stream_idle_timeout_seconds, "stream_idle"
                    )
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    timeout_chunk = self._timeout_chunk(started, exc)
                    if timeout_chunk is None:
                        raise
                    yield timeout_chunk
                    return
                reported_usage = _chunk_usage(chunk) or reported_usage
                yield chunk
            self._observe(
                status="completed",
                started=started,
                usage_status="reported" if reported_usage is not None else "unknown",
            )
            usage = CURRENT_BUDGET.get()
            if usage is not None and reported_usage is not None:
                usage.record_usage(reported_usage)
        finally:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

    async def _explain_result(self, request: LlmRequest, usage) -> LlmResponse:
        """Generate prose for both first results and follow-ups, checking before display."""
        if usage is not None:
            usage.mark_deterministic_result_finalized()
        followup = request.metadata.get("intent") == "result_followup"
        explanation = request if followup else LlmRequest(
            user=request.user, messages=[LlmMessage(role="user", content=(
                f"用户问题：{usage.catalog_question or ''}\n查询范围："
                + json.dumps(usage.query_plan or {}, ensure_ascii=False)
            )), LlmMessage(role="user", content=usage.result_summary)],
            temperature=0.0, max_tokens=usage.budget.max_output_tokens,
        )

        def prepare(value):
            # Only the initial draft may reduce row detail. Reviews/revisions
            # see exactly the same evidence, or fail visibly on context overflow.
            value = self._fit_result_request(value)
            self._check_context(value)
            return value

        outcome = await generate_result_answer(explanation, self._send_request, prepare,
            usage.record_llm_round if usage else lambda: None,
            usage.finish_llm_round if usage else lambda: None)
        if usage is not None:
            usage.record_catalog({"result_explanation": {
                "server_calculated":True, "tools_enabled":False, "model_used":True,
                "answer_status":outcome.status, "review_count":outcome.review_count,
                "revision_count":outcome.revision_count, "calls":outcome.calls,
                "issue_codes":list(outcome.issue_codes),
            }})
            usage.finish()
        return LlmResponse(content=outcome.text, tool_calls=[], finish_reason="trusted_result_finalized")

    async def _next_stream_chunk(
        self, iterator: Any, timeout_seconds: float, phase: str
    ) -> LlmStreamChunk:
        try:
            return await asyncio.wait_for(anext(iterator), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise LlmPhaseTimeout(phase) from exc

    def _timeout_chunk(self, started: float, exc: BaseException) -> LlmStreamChunk | None:
        phase = _timeout_phase(exc)
        if phase is None:
            self._observe(
                status="error",
                started=started,
                usage_status="unknown",
                error_type=type(exc).__name__,
            )
            return None
        response = self._timeout_response(started, phase=phase)
        return LlmStreamChunk(
            content=response.content,
            finish_reason=response.finish_reason,
            metadata={"replace_content": True, "timeout_phase": phase},
        )

    async def validate_tools(self, tools: list[Any]) -> list[str]:
        return await self.delegate.validate_tools(tools)

    def _timeout_response(self, started: float, *, phase: str) -> LlmResponse:
        usage = CURRENT_BUDGET.get()
        self._observe(
            status="timeout",
            started=started,
            usage_status="unknown",
            error_type="llm_timeout",
            timeout_phase=phase,
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
        timeout_phase: str | None = None,
    ) -> None:
        usage = CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_llm_observation(
                status=status,
                elapsed_ms=int((perf_counter() - started) * 1000),
                usage_status=usage_status,
                finish_reason=finish_reason,
                error_type=error_type,
                timeout_phase=timeout_phase,
            )


def _chunk_usage(chunk: LlmStreamChunk) -> dict[str, int] | None:
    usage = chunk.metadata.get("usage")
    if not isinstance(usage, dict):
        return None
    parsed: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            parsed[key] = value
    return parsed or None
