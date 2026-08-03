from __future__ import annotations

import pytest

from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy


@pytest.fixture()
def policy() -> SqlPolicy:
    return SqlPolicy(analyst_limit=200, admin_limit=1000)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM fact_orders",
        "UPDATE fact_orders SET order_status = 'delivered'",
        "INSERT INTO fact_orders VALUES ('x')",
        "DROP TABLE analytics.fact_orders",
        "SELECT 1; DROP TABLE analytics.fact_orders",
        "SELECT * FROM information_schema.tables",
        "SELECT pg_sleep(1) FROM fact_orders",
        "SELECT * FROM fact_orders FOR UPDATE",
        "SELECT * FROM fact_orders",
    ],
)
def test_policy_rejects_unsafe_or_unbounded_queries(policy: SqlPolicy, sql: str) -> None:
    with pytest.raises(PolicyViolation):
        policy.evaluate(sql, role="analyst")


def test_policy_qualifies_tables_and_adds_analyst_limit(policy: SqlPolicy) -> None:
    decision = policy.evaluate(
        "SELECT customer_state, COUNT(order_id) AS orders "
        "FROM fact_orders JOIN dim_customers USING (customer_id) GROUP BY customer_state",
        role="analyst",
    )

    assert "analytics.fact_orders" in decision.final_sql
    assert "analytics.dim_customers" in decision.final_sql
    assert decision.final_sql.endswith("LIMIT 200")
    assert decision.tables == ("dim_customers", "fact_orders")


def test_policy_caps_a_large_limit(policy: SqlPolicy) -> None:
    decision = policy.evaluate(
        "SELECT customer_state FROM dim_customers LIMIT 10000", role="analyst"
    )

    assert decision.final_sql.endswith("LIMIT 200")
    assert decision.row_limit == 200


def test_policy_allows_count_of_join_key_but_not_raw_identifier(policy: SqlPolicy) -> None:
    decision = policy.evaluate(
        "SELECT COUNT(order_id) FROM fact_orders", role="analyst"
    )
    assert "COUNT(order_id)" in decision.final_sql

    with pytest.raises(PolicyViolation, match="sensitive columns"):
        policy.evaluate("SELECT order_id FROM fact_orders", role="analyst")


def test_admin_can_read_version_metadata_with_a_larger_limit(policy: SqlPolicy) -> None:
    decision = policy.evaluate(
        "SELECT dataset_version_id, source_version FROM dataset_versions", role="admin"
    )

    assert "analytics.dataset_versions" in decision.final_sql
    assert decision.row_limit == 1000


def test_analyst_cannot_read_version_metadata(policy: SqlPolicy) -> None:
    with pytest.raises(PolicyViolation, match="table is not allowed"):
        policy.evaluate("SELECT dataset_version_id FROM dataset_versions", role="analyst")


def test_policy_allows_read_only_cte(policy: SqlPolicy) -> None:
    decision = policy.evaluate(
        "WITH monthly AS (SELECT DATE_TRUNC('month', order_purchase_timestamp) AS month, "
        "COUNT(order_id) AS order_count FROM fact_orders GROUP BY 1) "
        "SELECT month, order_count FROM monthly",
        role="analyst",
    )

    assert decision.tables == ("fact_orders",)
    assert "LIMIT 200" in decision.final_sql
