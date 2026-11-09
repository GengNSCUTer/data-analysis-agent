import pytest

from data_analysis_agent.plan_sql_guard import sql_plan_mismatch


@pytest.mark.parametrize("condition,accepted", [
    ("category IS NOT NULL", False),
    ("category IS NOT NULL AND price>0", False),
    ("category IS NOT NULL OR price>0", True),
    ("NOT (category IS NOT NULL)", True),
    ("price IS NOT NULL", True),
    ("TRUE", True),
])
def test_unrequested_missing_dimension_filter_is_checked_without_blocking_metric_null_rules(condition, accepted):
    sql = f"WITH a AS (SELECT category AS kind,SUM(price) AS amount FROM items WHERE {condition} GROUP BY category) SELECT kind AS product_category,amount AS gmv FROM a"
    plan = {"query_plan_version": "query-plan-v2", "query_mode": "aggregate", "metric_ids": ["gmv"], "dimensions": ["product_category"]}
    schema = {"items": {"category": "TEXT", "price": "NUMERIC"}}
    assert (sql_plan_mismatch(sql, plan, column_schema=schema) is None) is accepted


def test_unused_null_filter_is_ignored_and_explicit_dimension_filter_is_allowed():
    schema = {"items": {"category": "TEXT", "price": "NUMERIC"}}
    plan = {"query_plan_version": "query-plan-v2", "query_mode": "aggregate", "metric_ids": ["gmv"], "dimensions": ["product_category"]}
    good = "WITH unused AS (SELECT category,SUM(price) FROM items WHERE category IS NOT NULL GROUP BY category) SELECT category AS product_category,SUM(price) AS gmv FROM items GROUP BY category"
    assert sql_plan_mismatch(good, plan, column_schema=schema) is None
    filtered = "SELECT category AS product_category,SUM(price) AS gmv FROM items WHERE category='A' AND category IS NOT NULL GROUP BY category"
    plan["filters"] = [{"dimension": "product_category", "value": "A", "sources": [{"table": "items", "column": "category"}]}]
    assert sql_plan_mismatch(filtered, plan, column_schema=schema) is None


@pytest.mark.parametrize('sql,allowed', [
    ('SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i', True),
    ("SELECT SUM(i.price) FILTER (WHERE p.payment_type='credit_card') AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id", True),
    ("SELECT SUM(CASE WHEN p.payment_value>0 THEN i.price ELSE 0 END) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id", True),
    ('WITH a AS (SELECT SUM(price) AS v FROM analytics.fact_order_items) SELECT COALESCE(v,0) AS gmv FROM a', True),
    ('WITH a AS (SELECT payment_value AS price FROM analytics.fact_payments) SELECT SUM(price) AS gmv FROM a', False),
    ('WITH unused AS (SELECT price FROM analytics.fact_order_items) SELECT SUM(p.payment_value) AS gmv FROM analytics.fact_payments p', False),
    ('SELECT SUM(p.payment_value) AS gmv FROM analytics.fact_payments p WHERE EXISTS (SELECT 1 FROM analytics.fact_order_items i WHERE i.price>0)', False),
    ('SELECT SUM(p.payment_value)+SUM(i.price)*0 AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id', False),
    ('SELECT 0 AS gmv', False),
    ('SELECT MAX(price) AS gmv FROM analytics.fact_order_items', False),
    ('SELECT SUM(DISTINCT price) AS gmv FROM analytics.fact_order_items', False),
    ('SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i UNION ALL SELECT SUM(p.payment_value) AS gmv FROM analytics.fact_payments p', False),
])
def test_amount_provenance_follows_output_through_ctes_and_ignores_decoys(sql, allowed):
    rules = {'gmv': {'aggregation':'sum', 'required':['fact_order_items.price'], 'allowed':['fact_order_items.price','fact_orders.order_status']}}
    mismatch = sql_plan_mismatch(sql, {'metric_ids':['gmv']}, metric_value_sources=rules)
    assert (mismatch is None) is allowed


@pytest.mark.parametrize('expression,accepted', [
    ('SUM(i.price)', True),
    ('CAST(SUM(i.price) AS NUMERIC)', True),
    ('COALESCE(SUM(i.price),0)', True),
    ('SUM(i.price)*1', True),
    ('SUM(i.price)*2/2', True),
    ('SUM(i.price)/3*3', True),
    ('SUM(i.price*2)/2', True),
    ('SUM(i.price)+0', True),
    ('SUM(i.price)*0+SUM(i.price)', True),
    ('SUM(i.price)*2', False),
    ('SUM(i.price/2)', False),
    ('SUM(i.price)/2', False),
    ('SUM(i.price)+1', False),
    ('SUM(i.price+i.price)', False),
    ('SUM(i.price)*0', False),
])
def test_amount_alias_cannot_hide_scalar_formula_changes(expression, accepted):
    rules = {'gmv': {'aggregation':'sum', 'required':['fact_order_items.price'],
                     'allowed':['fact_order_items.price']}}
    statement = f'SELECT {expression} AS gmv FROM analytics.fact_order_items i'
    assert (sql_plan_mismatch(statement, {'metric_ids':['gmv']}, metric_value_sources=rules) is None) is accepted


def test_amount_formula_changes_are_checked_inside_contributing_ctes():
    rules = {'gmv': {'aggregation':'sum', 'required':['fact_order_items.price'],
                     'allowed':['fact_order_items.price']}}
    for definition in ('SUM(price)*2 AS total', 'SUM(price*2) AS total'):
        statement = f'WITH totals AS (SELECT {definition} FROM analytics.fact_order_items) SELECT total AS gmv FROM totals'
        assert sql_plan_mismatch(statement, {'metric_ids':['gmv']}, metric_value_sources=rules)
    equivalent = ('WITH totals AS (SELECT SUM(price)*2 AS total FROM analytics.fact_order_items) '
                  'SELECT total/2 AS gmv FROM totals')
    assert sql_plan_mismatch(equivalent, {'metric_ids':['gmv']}, metric_value_sources=rules) is None


def test_split_amount_components_are_not_assumed_to_be_duplicate_inputs():
    rules = {'gmv': {'aggregation':'sum', 'required':['fact_order_items.price'],
                     'allowed':['fact_order_items.price','fact_orders.order_status']}}
    statement = (
        "WITH a AS (SELECT SUM(price) AS part FROM analytics.fact_order_items WHERE price<100), "
        "b AS (SELECT SUM(price) AS part FROM analytics.fact_order_items WHERE price>=100) "
        "SELECT COALESCE(a.part,0)+COALESCE(b.part,0) AS gmv FROM a CROSS JOIN b"
    )
    assert sql_plan_mismatch(statement, {'metric_ids':['gmv']}, metric_value_sources=rules) is None


@pytest.mark.parametrize('suffix,accepted', [
    ("WHERE o.order_status NOT IN ('canceled','unavailable')", True),
    ("WHERE NOT o.order_status IN ('canceled','unavailable')", True),
    ("WHERE o.order_status!='canceled' AND o.order_status!='unavailable'", True),
    ('WHERE i.price>0', False),
    ('', False),
    ("WHERE o.order_status NOT IN ('canceled','unavailable') OR TRUE", False),
])
def test_missing_catalog_status_field_is_detected_on_actual_metric_paths(suffix, accepted):
    plan = {'metric_ids':['gmv'], 'metric_plans':[{'metric_id':'gmv','grain':'order_item',
        'time_field':'fact_orders.order_purchase_timestamp',
        'default_filters':["fact_orders.order_status NOT IN ('canceled','unavailable')"]}]}
    statement = ('SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i '
                 'JOIN analytics.fact_orders o ON o.order_id=i.order_id ' + suffix)
    assert (sql_plan_mismatch(statement,plan) is None) is accepted


def test_catalog_status_can_be_applied_in_conditional_sum_but_not_an_unused_cte():
    plan = {'metric_ids':['gmv'],'metric_plans':[{'metric_id':'gmv',
        'default_filters':["fact_orders.order_status NOT IN ('canceled','unavailable')"]}]}
    conditional = ("SELECT SUM(CASE WHEN o.order_status NOT IN ('canceled','unavailable') THEN i.price ELSE 0 END) AS gmv "
        "FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id")
    assert sql_plan_mismatch(conditional,plan) is None
    decoy = ("WITH unused AS (SELECT order_id FROM analytics.fact_orders WHERE order_status NOT IN ('canceled','unavailable')) "
        "SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id")
    assert sql_plan_mismatch(decoy,plan)


@pytest.mark.parametrize('nested_schema', [False, True])
@pytest.mark.parametrize('time_field,accepted', [('o.created_at',True),('i.created_at',False)])
def test_time_bucket_follows_catalog_fact_time_not_another_created_at(time_field, accepted, nested_schema):
    plan = {'metric_ids':['completed_sale_amount'],'time_grain':'month',
        'metric_plans':[{'metric_id':'completed_sale_amount','time_field':'orders.created_at'}]}
    schema = {'orders':{'order_id':'INT','created_at':'TIMESTAMPTZ'},
              'order_items':{'order_id':'INT','created_at':'TIMESTAMPTZ','sale_price':'NUMERIC'}}
    if nested_schema:
        schema = {'analytics':schema}
    statement = f"SELECT DATE_TRUNC('month',{time_field}) AS time,SUM(i.sale_price) AS completed_sale_amount FROM analytics.orders o JOIN analytics.order_items i ON i.order_id=o.order_id GROUP BY 1"
    assert (sql_plan_mismatch(statement,plan,column_schema=schema) is None) is accepted


def test_order_mean_cannot_average_raw_item_amounts_but_preaggregation_remains_legal():
    plan = {'metric_ids':['average_order_value'],'metric_plans':[{'metric_id':'average_order_value','grain':'order'}]}
    grains = {'fact_orders':'order','fact_order_items':'order_item'}
    wrong = 'SELECT AVG(i.price) AS average_order_value FROM analytics.fact_order_items i'
    right = ('WITH totals AS (SELECT order_id,SUM(price) AS amount FROM analytics.fact_order_items GROUP BY order_id) '
             'SELECT AVG(amount) AS average_order_value FROM totals')
    assert sql_plan_mismatch(wrong,plan,table_grains=grains)
    assert sql_plan_mismatch(right,plan,table_grains=grains) is None


@pytest.mark.parametrize('denominator', ['COUNT(*)', 'COUNT(i.price)', 'COUNT(o.order_id)'])
def test_order_mean_cannot_divide_item_totals_by_raw_joined_row_count(denominator):
    plan = {'metric_ids': ['average_order_value'], 'metric_plans': [
        {'metric_id': 'average_order_value', 'grain': 'order'}]}
    statement = (f'SELECT SUM(i.price)/NULLIF({denominator},0) AS average_order_value '
                 'FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id')
    assert sql_plan_mismatch(statement, plan, table_grains={
        'fact_orders': 'order', 'fact_order_items': 'order_item'})


@pytest.mark.parametrize('statement', [
    'SELECT SUM(i.price)/NULLIF(COUNT(DISTINCT i.order_id),0) AS average_order_value FROM analytics.fact_order_items i',
    'WITH totals AS (SELECT order_id,SUM(price) AS amount FROM analytics.fact_order_items GROUP BY order_id) '
    'SELECT SUM(amount)/NULLIF(COUNT(*),0) AS average_order_value FROM totals',
    'SELECT SUM(price)/NULLIF(COUNT(*),0) AS average_item_price FROM analytics.fact_order_items',
])
def test_distinct_orders_and_preaggregated_counts_are_not_raw_item_denominators(statement):
    metric = 'average_item_price' if 'AS average_item_price' in statement else 'average_order_value'
    plan = {'metric_ids': [metric], 'metric_plans': [
        {'metric_id': metric, 'grain': 'order_item' if metric == 'average_item_price' else 'order'}]}
    assert sql_plan_mismatch(statement, plan, table_grains={
        'fact_orders': 'order', 'fact_order_items': 'order_item'}) is None


@pytest.mark.parametrize('value,accepted', [
    ('SUM(CASE WHEN i.price>0 THEN i.price*2 ELSE 0 END)', False),
    ('SUM(CASE WHEN i.price>0 THEN i.price/2 ELSE NULL END)', False),
    ('SUM(CASE WHEN i.price>0 THEN i.price ELSE 0 END)', True),
    ('SUM(CASE WHEN i.price>0 THEN i.price*2 ELSE 0 END)/2', True),
    ('SUM(CASE WHEN i.price>0 THEN i.price ELSE NULL END)', True),
])
def test_conditional_amount_preserves_scalar_checks_and_equivalent_rescaling(value, accepted):
    rule = {'gmv': {'aggregation': 'sum', 'required': ['fact_order_items.price'],
                    'allowed': ['fact_order_items.price']}}
    statement = f'SELECT {value} AS gmv FROM analytics.fact_order_items i'
    assert (sql_plan_mismatch(statement, {'metric_ids': ['gmv']}, metric_value_sources=rule) is None) is accepted


@pytest.mark.parametrize('projection', ["'2017年上半年'", "'not-a-date'", 'NULL'])
def test_time_series_cannot_replace_real_month_buckets_with_literal_labels(projection):
    plan = {'metric_ids': ['paid_order_count'], 'time_grain': 'month',
        'metric_plans': [{'metric_id': 'paid_order_count', 'time_field': 'fact_orders.order_purchase_timestamp'}]}
    sql = f'SELECT {projection} AS time, COUNT(*) AS paid_order_count FROM analytics.fact_orders'
    assert sql_plan_mismatch(sql, plan)


def test_one_month_query_can_label_its_known_bucket_with_a_valid_date_literal():
    plan = {'metric_ids': ['paid_order_count'], 'time_grain': 'month',
        'time_range': {'start': '2017-01-01', 'end': '2017-02-01'},
        'metric_plans': [{'metric_id': 'paid_order_count', 'time_field': 'fact_orders.order_purchase_timestamp'}]}
    sql = ("SELECT '2017-01-01' AS time, COUNT(*) AS paid_order_count FROM analytics.fact_orders "
           "WHERE order_purchase_timestamp >= '2017-01-01' AND order_purchase_timestamp < '2017-02-01'")
    assert sql_plan_mismatch(sql, plan) is None


def test_conditional_average_uses_value_grain_not_status_predicate_grain():
    plan = {'metric_ids':['average_item_price'],
            'metric_plans':[{'metric_id':'average_item_price','grain':'order_item'}]}
    sql = ("SELECT AVG(CASE WHEN o.order_status='delivered' THEN i.price ELSE NULL END) AS average_item_price "
           'FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id')
    grains = {'fact_orders':'order','fact_order_items':'order_item'}
    assert sql_plan_mismatch(sql,plan,table_grains=grains) is None


@pytest.mark.parametrize("column,accepted", [("order_id", True), ("status", False)])
def test_preaggregate_can_rejoin_the_same_catalog_target_key_but_not_non_key(column, accepted):
    plan = {"metric_ids": ["average_order_value"], "lookup_joins": [
        {"table": "orders", "on": "order_items.order_id = orders.order_id"},
        {"table": "users", "on": "orders.user_id = users.id"},
    ]}
    sql = (
        f"WITH amounts AS (SELECT o.{column} AS join_key, SUM(i.sale_price) AS amount "
        "FROM analytics.orders o JOIN analytics.order_items i ON i.order_id=o.order_id "
        f"WHERE o.status='Complete' GROUP BY o.{column}) "
        "SELECT u.traffic_source AS customer_traffic_source, AVG(a.amount) AS average_order_value "
        f"FROM amounts a JOIN analytics.orders o ON o.{column}=a.join_key "
        "JOIN analytics.users u ON u.id=o.user_id GROUP BY u.traffic_source"
    )
    assert (sql_plan_mismatch(sql, plan) is None) is accepted


def test_preaggregate_join_on_wrong_item_key_still_rejected():
    plan = {"lookup_joins": [{"table": "orders", "on": "order_items.order_id = orders.order_id"}]}
    sql = (
        "WITH a AS (SELECT i.product_id, SUM(i.sale_price) AS amount FROM analytics.order_items i GROUP BY i.product_id) "
        "SELECT SUM(a.amount) AS completed_sale_amount FROM a JOIN analytics.orders o ON o.order_id=a.product_id"
    )
    assert sql_plan_mismatch(sql, plan)


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
