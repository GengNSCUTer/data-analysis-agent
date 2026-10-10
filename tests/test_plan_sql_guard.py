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
