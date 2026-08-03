from __future__ import annotations

import pytest

from data_analysis_agent.budget import (
    BudgetSafetyMiddleware,
    BudgetUsage,
    RequestBudget,
)
from vanna.core.llm import LlmRequest, LlmResponse
from vanna.core.tool import ToolCall
from vanna.core.user import User


def test_budget_rejects_non_positive_and_inconsistent_limits() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        RequestBudget(max_tool_calls=0)
    with pytest.raises(ValueError, match="cannot exceed"):
        RequestBudget(max_tool_calls=1, max_sql_calls=2)


@pytest.mark.parametrize(
    ("tool_names", "expected_reason", "expected_total"),
    [
        (["run_sql", "run_sql", "run_sql"], "tool_budget_exhausted", 2),
        (["run_sql", "visualize_data", "visualize_data"], "tool_budget_exhausted", 2),
        (["run_sql", "unknown_tool"], "running", 2),
    ],
)
def test_each_individual_tool_call_is_counted(
    tool_names: list[str], expected_reason: str, expected_total: int
) -> None:
    usage = BudgetUsage(
        RequestBudget(max_tool_calls=2, max_sql_calls=2, max_visualization_calls=1)
    )

    accepted = [usage.consume_tool(name) for name in tool_names]

    assert (
        sum(accepted) == expected_total
        if expected_reason == "running"
        else sum(accepted) == 2
    )
    assert usage.tool_calls_used == 2
    assert usage.termination_reason == expected_reason


@pytest.mark.asyncio
async def test_budget_middleware_replaces_model_answer_after_exhaustion() -> None:
    usage = BudgetUsage(RequestBudget(max_tool_calls=1, max_sql_calls=1))
    usage.consume_tool("run_sql")
    usage.consume_tool("run_sql")
    from data_analysis_agent.budget import CURRENT_BUDGET

    token = CURRENT_BUDGET.set(usage)
    try:
        middleware = BudgetSafetyMiddleware()
        response = await middleware.after_llm_response(
            LlmRequest(messages=[], user=User(id="u")),
            LlmResponse(
                content="GMV 是 999",
                tool_calls=[ToolCall(id="x", name="run_sql", arguments={})],
            ),
        )
    finally:
        CURRENT_BUDGET.reset(token)

    assert response.tool_calls is None
    assert "未输出未经完整验证的数值结论" in (response.content or "")


def test_unknown_termination_reason_is_rejected() -> None:
    usage = BudgetUsage(RequestBudget())
    with pytest.raises(ValueError, match="unknown termination reason"):
        usage.terminate("made_up_reason")


def test_tool_iteration_limit_is_terminal_only_when_last_response_requested_tools() -> (
    None
):
    usage = BudgetUsage(RequestBudget(max_tool_iterations=1))
    usage.record_llm_round()
    usage.record_llm_response(True)

    usage.terminate("tool_budget_exhausted")

    assert usage.termination_reason == "tool_budget_exhausted"


def test_completed_cannot_overwrite_a_terminal_budget_reason() -> None:
    usage = BudgetUsage(RequestBudget())
    usage.terminate("tool_budget_exhausted")
    usage.terminate("completed")

    assert usage.termination_reason == "tool_budget_exhausted"


def test_budget_usage_serializes_server_generated_catalog_trace_only() -> None:
    usage = BudgetUsage(RequestBudget())
    trace = {
        "catalog_version": "olist-catalog-v1",
        "question_fingerprint": "abc123",
        "selected_tables": ["fact_orders"],
    }

    usage.record_catalog(trace)

    assert usage.as_dict()["catalog_trace"] == trace
    with pytest.raises(TypeError, match="mapping"):
        usage.record_catalog("raw question")  # type: ignore[arg-type]
