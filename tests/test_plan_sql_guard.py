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
