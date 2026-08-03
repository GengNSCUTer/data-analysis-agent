from __future__ import annotations

import pytest

from data_analysis_agent.sql_policy import SqlPolicy
from data_analysis_agent.sql_repair import (
    OneShotSqlRepair,
    build_repair_prompt,
    sanitize_sql_error,
)


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        ("column foo does not exist", "unknown_column", True),
        ('relation "secret" does not exist', "unknown_table", True),
        ("syntax error at or near SELECT", "syntax", True),
        ("canceling statement due to statement timeout", "timeout", False),
        ("permission denied for table fact_orders", "permission", False),
    ],
)
def test_sql_error_is_sanitized(error: str, category: str, retryable: bool) -> None:
    result = sanitize_sql_error(error)

    assert result.category == category
    assert result.retryable is retryable
    assert error not in result.public_reason


def test_repair_candidate_must_pass_full_policy_and_is_limited_to_one() -> None:
    coordinator = OneShotSqlRepair(SqlPolicy(), role="analyst")
    original = "SELECT missing_column FROM fact_orders"
    prompts: list[str] = []

    accepted = coordinator.repair(
        original,
        "column missing_column does not exist",
        lambda prompt: prompts.append(prompt)
        or "SELECT COUNT(order_id) AS paid_order_count FROM fact_orders",
    )

    assert accepted.accepted is True
    assert accepted.policy_decision is not None
    assert accepted.policy_decision.role == "analyst"
    assert prompts and "missing_column" in prompts[0]
    assert "does not exist" not in prompts[0]
    second = coordinator.repair(
        original,
        "column missing_column does not exist",
        lambda _: "SELECT COUNT(order_id) FROM fact_orders",
    )
    assert second.accepted is False
    assert second.reason == "repair_budget_exhausted"


def test_repair_rejects_unsafe_candidate() -> None:
    coordinator = OneShotSqlRepair(SqlPolicy(), role="analyst")
    outcome = coordinator.repair(
        "SELECT missing_column FROM fact_orders",
        "column missing_column does not exist",
        lambda _: "DELETE FROM fact_orders",
    )

    assert outcome.attempted is True
    assert outcome.accepted is False
    assert outcome.reason == "repaired_sql_rejected_by_policy"


def test_repair_prompt_is_bounded_and_contains_only_allowed_context() -> None:
    error = sanitize_sql_error("column x does not exist")
    prompt = build_repair_prompt(
        "SELECT " + "x" * 10000,
        error,
        catalog_context="analytics.fact_orders.order_id",
        max_chars=200,
    )

    assert len(prompt) <= 200
    assert "<allowed_catalog>" not in prompt or "fact_orders" in prompt
    assert "column x does not exist" not in prompt
