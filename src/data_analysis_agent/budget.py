"""Request resource budgets shared by the trusted Agent runtime and tests."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import os
from typing import Any, Final

from vanna.core.llm import LlmRequest, LlmResponse
from vanna.core.middleware import LlmMiddleware
from vanna.core.registry import ToolRegistry
from vanna.core.tool import ToolCall, ToolContext, ToolResult


TERMINATION_REASONS: Final[frozenset[str]] = frozenset(
    {
        "running",
        "completed",
        "clarification_required",
        "tool_budget_exhausted",
        "context_truncated",
        "sql_policy_rejected",
        "query_timeout",
        "execution_error",
        "unsupported_request",
        "input_too_long",
    }
)


@dataclass(frozen=True)
class RequestBudget:
    """Hard limits for one Agent request."""

    max_tool_iterations: int = 4
    max_tool_calls: int = 4
    max_sql_calls: int = 2
    max_visualization_calls: int = 1
    max_input_chars: int = 4_000
    max_context_chars: int = 12_000
    max_context_messages: int = 40
    max_output_tokens: int = 1_200

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_sql_calls > self.max_tool_calls:
            raise ValueError("max_sql_calls cannot exceed max_tool_calls")
        if self.max_visualization_calls > self.max_tool_calls:
            raise ValueError("max_visualization_calls cannot exceed max_tool_calls")

    @classmethod
    def from_environment(cls) -> "RequestBudget":
        def read(name: str, default: int) -> int:
            value = os.getenv(name)
            return default if value is None else int(value)

        return cls(
            max_tool_iterations=read(
                "DATA_ANALYSIS_MAX_TOOL_ITERATIONS", cls.max_tool_iterations
            ),
            max_tool_calls=read("DATA_ANALYSIS_MAX_TOOL_CALLS", cls.max_tool_calls),
            max_sql_calls=read("DATA_ANALYSIS_MAX_SQL_CALLS", cls.max_sql_calls),
            max_visualization_calls=read(
                "DATA_ANALYSIS_MAX_VISUALIZATION_CALLS", cls.max_visualization_calls
            ),
            max_input_chars=read("DATA_ANALYSIS_MAX_INPUT_CHARS", cls.max_input_chars),
            max_context_chars=read(
                "DATA_ANALYSIS_MAX_CONTEXT_CHARS", cls.max_context_chars
            ),
            max_context_messages=read(
                "DATA_ANALYSIS_MAX_CONTEXT_MESSAGES", cls.max_context_messages
            ),
            max_output_tokens=read(
                "DATA_ANALYSIS_MAX_OUTPUT_TOKENS", cls.max_output_tokens
            ),
        )


@dataclass
class BudgetUsage:
    """Mutable usage and terminal state for one request."""

    budget: RequestBudget
    tool_calls_used: int = 0
    sql_calls_used: int = 0
    visualization_calls_used: int = 0
    llm_rounds_used: int = 0
    input_chars: int = 0
    context_chars: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    context_truncated: bool = False
    termination_reason: str = "running"
    error_type: str | None = None
    catalog_trace: dict[str, Any] | None = None
    catalog_question: str | None = None
    working_memory: dict[str, Any] | None = None
    last_response_had_tool_calls: bool = False
    _tool_counts: dict[str, int] = field(default_factory=dict)

    def set_input(self, message: str) -> None:
        self.input_chars = len(message)
        if self.input_chars > self.budget.max_input_chars:
            self.terminate("input_too_long")

    def record_llm_round(self) -> None:
        self.llm_rounds_used += 1

    def record_llm_response(self, has_tool_calls: bool) -> None:
        self.last_response_had_tool_calls = has_tool_calls

    def consume_tool(self, tool_name: str) -> bool:
        """Consume one individual call, including calls in one model response."""
        if self.termination_reason != "running":
            return False
        next_total = self.tool_calls_used + 1
        next_count = self._tool_counts.get(tool_name, 0) + 1
        if next_total > self.budget.max_tool_calls:
            self.terminate("tool_budget_exhausted")
            return False
        if tool_name == "run_sql" and next_count > self.budget.max_sql_calls:
            self.terminate("tool_budget_exhausted")
            return False
        if (
            tool_name == "visualize_data"
            and next_count > self.budget.max_visualization_calls
        ):
            self.terminate("tool_budget_exhausted")
            return False
        self.tool_calls_used = next_total
        self._tool_counts[tool_name] = next_count
        self.sql_calls_used = self._tool_counts.get("run_sql", 0)
        self.visualization_calls_used = self._tool_counts.get("visualize_data", 0)
        return True

    def record_context(self, char_count: int, truncated: bool = False) -> None:
        self.context_chars = max(0, char_count)
        if truncated:
            self.context_truncated = True

    def record_catalog(self, trace: dict[str, Any]) -> None:
        """Record only the server-generated, non-content retrieval evidence."""
        if not isinstance(trace, dict):
            raise TypeError("catalog trace must be a mapping")
        self.catalog_trace = dict(trace)

    def set_catalog_context(
        self, retrieval_question: str, working_memory: dict[str, Any] | None = None
    ) -> None:
        """Set per-request retrieval context without adding it to run evidence."""
        self.catalog_question = retrieval_question[: self.budget.max_input_chars]
        self.working_memory = dict(working_memory) if working_memory else None

    def record_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.input_tokens = self._first_int(usage, "input_tokens", "prompt_tokens")
        self.output_tokens = self._first_int(
            usage, "output_tokens", "completion_tokens"
        )
        self.total_tokens = self._first_int(usage, "total_tokens")

    def terminate(self, reason: str, error_type: str | None = None) -> None:
        if reason not in TERMINATION_REASONS:
            raise ValueError(f"unknown termination reason: {reason}")
        if self.termination_reason in {"running", "context_truncated"}:
            self.termination_reason = reason
        if error_type:
            self.error_type = error_type

    def finish(self) -> None:
        if self.termination_reason == "running":
            self.termination_reason = "completed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_calls_used": self.tool_calls_used,
            "sql_calls_used": self.sql_calls_used,
            "visualization_calls_used": self.visualization_calls_used,
            "llm_rounds_used": self.llm_rounds_used,
            "input_chars": self.input_chars,
            "context_chars": self.context_chars,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "context_truncated": self.context_truncated,
            "termination_reason": self.termination_reason,
            "error_type": self.error_type,
            "catalog_trace": self.catalog_trace,
        }

    @staticmethod
    def _first_int(usage: dict[str, Any], *names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return None
        return None


CURRENT_BUDGET: ContextVar[BudgetUsage | None] = ContextVar(
    "current_agent_budget", default=None
)


class BudgetedToolRegistry(ToolRegistry):
    """Count every tool invocation before delegating to Vanna's registry."""

    async def execute(self, tool_call: ToolCall, context: ToolContext) -> ToolResult:
        usage = context.metadata.get("budget_usage")
        if isinstance(usage, BudgetUsage) and not usage.consume_tool(tool_call.name):
            message = (
                "本次请求已达到工具调用预算，不能继续执行查询；请缩小问题范围后重试。"
            )
            return ToolResult(
                success=False,
                result_for_llm=message,
                error=message,
                metadata={"termination_reason": "tool_budget_exhausted"},
            )
        return await super().execute(tool_call, context)


class BudgetSafetyMiddleware(LlmMiddleware):
    """Prevent a model from emitting a numeric answer after budget exhaustion."""

    async def before_llm_request(self, request: LlmRequest) -> LlmRequest:
        usage = CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_llm_round()
        return request

    async def after_llm_response(
        self, request: LlmRequest, response: LlmResponse
    ) -> LlmResponse:
        usage = CURRENT_BUDGET.get()
        if usage is None:
            return response
        usage.record_llm_response(response.is_tool_call())
        usage.record_usage(response.usage)
        if usage.termination_reason == "tool_budget_exhausted":
            return LlmResponse(
                content=(
                    "本次分析在工具调用预算内未完成，未输出未经完整验证的数值结论。"
                    "请缩小问题范围或拆分成多个问题后重试。"
                ),
                finish_reason="budget_exhausted",
            )
        return response
