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
        ('missing FROM-clause entry for table "fact_reviews"', "unknown_column", True),
        ('relation "secret" does not exist', "unknown_table", True),
        ("syntax error at or near SELECT", "syntax", True),
        ("canceling statement due to statement timeout", "timeout", False),
        ("permission denied for table fact_orders", "permission", False),
        ('column "order_id" must appear in the GROUP BY clause or be used in an aggregate function', "syntax", True),
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


def test_repair_prompt_preserves_complete_catalog_and_plan() -> None:
    error = sanitize_sql_error("column x does not exist")
    sql = "SELECT " + "x" * 10000
    catalog = "analytics.fact_orders.order_id\n" * 400 + "filter_customer_state:SP top_n:10"
    prompt = build_repair_prompt(sql, error, catalog_context=catalog)
    assert sql in prompt and catalog in prompt
    assert "column x does not exist" not in prompt


@pytest.mark.parametrize(("code", "category", "retryable"), [
    ("42803", "syntax", True), ("42601", "syntax", True),
    ("42703", "unknown_column", True), ("42P01", "unknown_table", True),
    ("42702", "ambiguous_reference", True), ("42501", "permission", False),
    ("57014", "timeout", False), ("08006", "connection", False),
])
def test_error_category_uses_postgres_sqlstate_without_exposing_driver_text(code, category, retryable):
    class DatabaseError(Exception):
        pgcode = code

    error = sanitize_sql_error(DatabaseError("private driver detail"))
    assert error.category == category and error.retryable is retryable
    assert "private" not in error.public_reason


@pytest.mark.asyncio
async def test_provider_failure_has_its_own_outcome():
    async def unavailable(_):
        raise RuntimeError("private provider response")

    outcome = await OneShotSqlRepair(SqlPolicy()).repair_async(
        "SELECT missing_column FROM fact_orders",
        "column missing_column does not exist", unavailable,
    )
    assert not outcome.accepted and outcome.reason == "repair_provider_failed"
    assert "private" not in str(outcome.as_dict())


def test_sync_provider_failure_is_sanitized_and_consumes_the_one_attempt():
    def unavailable(_):
        raise RuntimeError("private provider response")

    repair = OneShotSqlRepair(SqlPolicy())
    outcome = repair.repair("SELECT missing FROM fact_orders", "column missing does not exist", unavailable)
    assert outcome.reason == "repair_provider_failed" and not outcome.accepted
    assert "private" not in str(outcome.as_dict())
    assert repair.repair("SELECT missing FROM fact_orders", "column missing does not exist", unavailable).reason == "repair_budget_exhausted"


@pytest.mark.asyncio
async def test_cancelled_repair_propagates_cancellation():
    import asyncio

    async def cancelled(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await OneShotSqlRepair(SqlPolicy()).repair_async(
            "SELECT missing FROM fact_orders", "column missing does not exist", cancelled,
        )
