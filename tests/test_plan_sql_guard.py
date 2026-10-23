import pytest

from data_analysis_agent.plan_sql_guard import sql_plan_mismatch


def test_state_filter_and_top_n_must_appear_in_sql() -> None:
    plan = {
        "warnings": ["filter_customer_state:SP"],
        "dimensions": ["product_category_name", "customer_city"],
        "attribution_requirements": [],
    }
    assert sql_plan_mismatch("SELECT product_category_name, gmv FROM orders", plan)
    assert sql_plan_mismatch(
        "SELECT product_category_name, gmv FROM orders WHERE customer_state = 'SP' LIMIT 100",
        plan,
    ) is not None
    assert (
        sql_plan_mismatch(
            "SELECT product_category_name, gmv FROM orders "
            "WHERE customer_state = 'SP' GROUP BY product_category_name "
            "ORDER BY gmv DESC LIMIT 20",
            plan,
        )
        is None
    )


def test_payment_share_requires_payment_value() -> None:
    plan = {
        "warnings": [],
        "dimensions": ["payment_type"],
        "attribution_requirements": [
            {"policy_mode": "payment_share", "dimension": "payment_type"}
        ],
    }
    assert "payment_value" in sql_plan_mismatch("SELECT payment_type, SUM(price) FROM items", plan)
    assert (
        sql_plan_mismatch(
            "SELECT payment_type, SUM(price * payment_value / total_payment) "
            "FROM items GROUP BY payment_type",
            plan,
        )
        is None
    )


def test_ranking_category_plan_requires_limit() -> None:
    plan = {
        "warnings": [],
        "dimensions": ["product_category_name"],
        "top_n": {"product_category_name": 20},
        "attribution_requirements": [],
    }
    assert sql_plan_mismatch("SELECT product_category_name, item_count FROM items", plan)
    assert (
        sql_plan_mismatch(
            "SELECT product_category_name, item_count FROM items "
            "ORDER BY item_count DESC LIMIT 20",
            plan,
        )
        is None
    )


def test_v2_workspace_city_without_top_n_does_not_inherit_olist_limit():
    plan = {"query_plan_version": "query-plan-v2", "dimensions": ["customer_city"], "top_n": {}}
    sql = "SELECT city AS customer_city, COUNT(*) AS completed_item_count FROM users GROUP BY city"
    assert sql_plan_mismatch(sql, plan) is None
    assert sql_plan_mismatch(sql, {**plan, "top_n": {"customer_city": 20}}) is not None
    # Legacy plans keep their original rule for historical callers.
    assert sql_plan_mismatch(sql, {"dimensions": ["customer_city"]}) is not None


def test_online_ranking_limit_is_owned_by_execution_policy_not_model_repair():
    from data_analysis_agent.sql_policy import SqlPolicy
    plan = {"dimensions": ["customer_state"], "top_n": {"customer_state": 5},
            "result_budget": {"query_mode": "aggregate"}}
    for suffix in ("", " LIMIT 1", " LIMIT 500"):
        sql = ("SELECT c.customer_state, COUNT(*) AS paid_order_count FROM analytics.fact_orders o "
               "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
               "GROUP BY c.customer_state ORDER BY paid_order_count DESC" + suffix)
        assert sql_plan_mismatch(sql, plan) is None
        decision = SqlPolicy().evaluate(sql, complete_result=True, top_n=5, result_limit=27)
        assert decision.row_limit == 5 and decision.final_sql.endswith("LIMIT 5")


def test_partitioned_top_n_requires_window_rank_and_partition_filter():
    plan = {
        "dimensions": ["customer_state", "product_category_name"],
        "top_n": {"product_category_name": 5},
        "top_n_partition_dimensions": ["customer_state"],
        "result_budget": {"query_mode": "aggregate"},
    }
    valid = (
        "WITH ranked AS (SELECT customer_state, product_category_name, gmv, "
        "ROW_NUMBER() OVER (PARTITION BY customer_state ORDER BY gmv DESC) AS rn "
        "FROM grouped) SELECT customer_state, product_category_name, gmv "
        "FROM ranked WHERE rn <= 5"
    )
    assert sql_plan_mismatch(valid, plan) is None
    assert "分区" in sql_plan_mismatch(
        valid.replace("PARTITION BY customer_state", ""), plan
    )
    assert "分区" in sql_plan_mismatch(
        valid.replace("PARTITION BY customer_state", "PARTITION BY seller_state"), plan
    )
    assert "排名" in sql_plan_mismatch(valid.replace("rn <= 5", "rn <= 8"), plan)


@pytest.mark.parametrize(("condition", "accepted"), [
    ("rn <= 5 OR TRUE", False),
    ("NOT (rn <= 5)", False),
    ("CASE WHEN rn <= 5 THEN TRUE ELSE TRUE END", False),
    ("rn <= 5 AND gmv > 0", True),
    ("rn <= 5 OR rn < 3", True),
    ("5 >= rn", True),
    ("6 > rn", True),
])
def test_partitioned_rank_bound_must_actually_restrict_rows(condition, accepted):
    plan = {
        "dimensions": ["customer_state", "product_category_name"],
        "top_n": {"product_category_name": 5},
        "top_n_partition_dimensions": ["customer_state"],
        "result_budget": {"query_mode": "aggregate"},
    }
    sql = (
        "WITH ranked AS (SELECT customer_state, product_category_name, gmv, "
        "ROW_NUMBER() OVER (PARTITION BY customer_state ORDER BY gmv DESC) AS rn "
        "FROM grouped) SELECT customer_state, product_category_name, gmv "
        "FROM ranked WHERE " + condition
    )
    assert (sql_plan_mismatch(sql, plan) is None) is accepted


def test_unused_ranked_cte_cannot_supply_top_n_evidence():
    plan = {"top_n": {"product_category_name": 5},
            "top_n_partition_dimensions": ["customer_state"]}
    sql = (
        "WITH ranked AS (SELECT customer_state, product_category_name, gmv, "
        "ROW_NUMBER() OVER (PARTITION BY customer_state ORDER BY gmv DESC) AS rn FROM grouped), "
        "unused AS (SELECT customer_state, gmv FROM ranked WHERE rn <= 5) "
        "SELECT customer_state, product_category_name, gmv FROM grouped"
    )
    assert sql_plan_mismatch(sql, plan)


@pytest.mark.parametrize("operation", ["UNION ALL", "UNION", "INTERSECT", "EXCEPT"])
def test_rank_bound_survives_aliases_and_must_hold_for_both_union_branches(operation):
    plan = {"top_n": {"product_category_name": 5},
            "top_n_partition_dimensions": ["customer_state"]}
    ctes = (
        "WITH ranked AS (SELECT customer_state, product_category_name, gmv, "
        "ROW_NUMBER() OVER (PARTITION BY customer_state ORDER BY gmv DESC) AS rn FROM grouped), "
        "renamed AS (SELECT customer_state, product_category_name, gmv, rn AS position FROM ranked) "
    )
    query = "SELECT customer_state, product_category_name, gmv FROM renamed WHERE position <= 5"
    assert sql_plan_mismatch(ctes + query, plan) is None
    assert sql_plan_mismatch(ctes + query + f" {operation} " + query, plan) is None
    assert sql_plan_mismatch(ctes + query + f" {operation} " + query.replace("position <= 5", "TRUE"), plan)
    assert sql_plan_mismatch(ctes + query.replace("position <= 5", "TRUE") + f" {operation} " + query, plan)


@pytest.mark.parametrize("operation", ["UNION ALL", "UNION"])
def test_state_filter_must_hold_for_each_nested_union_branch(operation):
    plan = {"metric_ids":["gmv"], "filters":[{
        "dimension":"customer_state", "value":"SP",
        "sources":[{"table":"dim_customers", "column":"customer_state"}],
    }]}
    query = ("SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i "
             "JOIN analytics.fact_orders o ON o.order_id=i.order_id "
             "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
             "WHERE c.customer_state='SP'")
    assert sql_plan_mismatch(f"{query} {operation} {query} {operation} {query}", plan) is None
    for index in range(3):
        branches = [query] * 3
        branches[index] = query.replace("c.customer_state='SP'", "TRUE")
        assert sql_plan_mismatch(f" {operation} ".join(branches), plan)


def test_rank_filter_must_belong_to_the_planned_partition():
    plan = {"top_n": {"product_category_name": 5},
            "top_n_partition_dimensions": ["customer_state"]}
    sql = (
        "WITH ranked AS (SELECT customer_state, product_category_name, gmv, "
        "ROW_NUMBER() OVER (PARTITION BY customer_state ORDER BY gmv DESC) AS state_rank, "
        "ROW_NUMBER() OVER (PARTITION BY seller_state ORDER BY gmv DESC) AS rn FROM grouped) "
        "SELECT customer_state, product_category_name, gmv FROM ranked WHERE rn <= 5"
    )
    assert sql_plan_mismatch(sql, plan)
    assert sql_plan_mismatch(sql.replace("WHERE rn <= 5", "WHERE state_rank <= 5"), plan) is None


@pytest.mark.parametrize("sql", [
    "SELECT payment_type, SUM(price) FROM items GROUP BY payment_type /* payment_value */",
    "SELECT payment_type, 'payment_value' AS note, SUM(price) FROM items GROUP BY payment_type",
])
def test_payment_share_requires_real_column_references(sql):
    plan = {"attribution_requirements": [
        {"policy_mode": "payment_share", "dimension": "payment_type"}
    ]}
    assert sql_plan_mismatch(sql, plan)
