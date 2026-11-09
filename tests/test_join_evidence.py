from data_analysis_agent.join_evidence import build_join_probes
from data_analysis_agent.sql_policy import SqlPolicy
from sqlglot import parse_one, exp
import pytest


def test_internal_probe_can_group_by_private_foreign_key_without_exposing_it():
    sql = (
        "WITH amounts AS (SELECT i.product_id, SUM(i.price) AS amount "
        "FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id "
        "GROUP BY i.product_id) SELECT p.product_category_name, SUM(a.amount) AS gmv "
        "FROM amounts a JOIN analytics.dim_products p ON p.product_id=a.product_id GROUP BY p.product_category_name"
    )
    policy = SqlPolicy()
    policy.evaluate(sql)
    probes, _ = build_join_probes(
        sql,
        row_keys={"fact_order_items": ("order_id", "order_item_id"), "fact_orders": ("order_id",)},
        source_grains={"fact_order_items": "order_item", "fact_orders": "order"},
        metric_grains={"gmv": "order_item"},
    )
    assert probes
    for probe in probes:
        decision = policy.evaluate(probe.sql, complete_result=True, result_limit=1)
        assert parse_one(decision.final_sql, read="postgres").named_selects == ["input_rows", "distinct_facts"]


def test_internal_probe_keeps_private_projections_inside_ctes_and_avoids_name_collision():
    sql = (
        "WITH daa_join_inputs AS (SELECT i.order_id, i.order_item_id, i.price "
        "FROM analytics.fact_order_items i) SELECT c.customer_state, SUM(i.price) AS gmv "
        "FROM daa_join_inputs i JOIN analytics.fact_orders o ON o.order_id=i.order_id "
        "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id GROUP BY c.customer_state"
    )
    probes, _ = build_join_probes(
        sql,
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={"fact_order_items": "order_item"}, metric_grains={"gmv": "order_item"},
    )
    assert probes
    tree = parse_one(probes[0].sql, read="postgres")
    names = [cte.alias_or_name for cte in tree.find_all(exp.CTE)]
    assert len(names) == len(set(names))
    assert tree.named_selects == ["input_rows", "distinct_facts"]
    SqlPolicy().evaluate(probes[0].sql, complete_result=True, result_limit=1)


def test_probe_counts_fact_identity_without_exposing_sensitive_grouping():
    probes, uncovered = build_join_probes(
        "SELECT SUM(i.price) AS gmv "
        "FROM analytics.fact_order_items i "
        "JOIN analytics.fact_payments p ON p.order_id=i.order_id",
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={"fact_order_items": "order_item", "fact_payments": "payment"},
        metric_grains={"gmv": "order_item"},
    )
    assert not uncovered and len(probes) == 1
    assert "COUNT(DISTINCT (i.order_id, i.order_item_id))" in probes[0].sql
    assert "input_rows" in probes[0].sql and "distinct_facts" in probes[0].sql
    assert "GROUP BY i.order_id" not in probes[0].sql
    decision = SqlPolicy().evaluate(probes[0].sql, complete_result=True, result_limit=1)
    assert decision.row_limit == 1


def test_probe_keeps_group_dimensions_when_checking_partitioned_aggregate():
    probes, uncovered = build_join_probes(
        "SELECT c.customer_state, SUM(i.price) AS gmv "
        "FROM analytics.fact_order_items i "
        "JOIN analytics.fact_orders o ON o.order_id=i.order_id "
        "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
        "GROUP BY c.customer_state",
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={
            "fact_order_items": "order_item",
            "fact_orders": "order",
            "dim_customers": "customer",
        },
        metric_grains={"gmv": "order_item"},
    )
    assert not uncovered and len(probes) == 1
    assert "GROUP BY c.customer_state" in probes[0].sql


def test_probe_reports_weighted_expression_as_uncovered():
    probes, uncovered = build_join_probes(
        "SELECT SUM(i.price * p.payment_value / p.total_payment) AS gmv "
        "FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id",
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={"fact_order_items": "order_item", "fact_payments": "payment"},
        metric_grains={"gmv": "order_item"},
    )
    assert not probes
    assert {item["reason"] for item in uncovered} == {"weighted_or_multiple_sources"}


def test_probe_excludes_null_measures_and_retains_aggregate_filter():
    probes, _ = build_join_probes(
        "SELECT SUM(i.price) FILTER (WHERE i.price > 0) AS gmv FROM analytics.fact_order_items i "
        "JOIN analytics.fact_orders o ON o.order_id=i.order_id",
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={"fact_order_items": "order_item"},
        metric_grains={"gmv": "order_item"},
    )
    assert "NOT i.price IS NULL" in probes[0].sql
    assert "i.price > 0" in probes[0].sql


def test_probe_flattens_simple_cte_but_keeps_preaggregated_boundary():
    raw = (
        "WITH items AS (SELECT i.order_id, i.order_item_id, i.price FROM analytics.fact_order_items i) "
        "SELECT SUM(items.price) AS gmv FROM items JOIN analytics.fact_orders o ON o.order_id=items.order_id"
    )
    probes, _ = build_join_probes(
        raw,
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={"fact_order_items": "order_item", "fact_orders": "order"},
        metric_grains={"gmv": "order_item"},
    )
    assert len(probes) == 1
    assert "COUNT(DISTINCT" in probes[0].sql
    grouped = (
        "WITH amounts AS (SELECT i.order_id, SUM(i.price) AS amount FROM analytics.fact_order_items i GROUP BY i.order_id) "
        "SELECT SUM(amounts.amount) AS gmv FROM amounts JOIN analytics.fact_orders o ON o.order_id=amounts.order_id"
    )
    probes, uncovered = build_join_probes(
        grouped,
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={"fact_order_items": "order_item", "fact_orders": "order"},
        metric_grains={"gmv": "order_item"},
    )
    assert not probes
    assert any(item["reason"] == "source_identity_unavailable" for item in uncovered)


def test_item_count_uses_item_identity_even_when_counting_order_foreign_key():
    probes, _ = build_join_probes(
        "SELECT COUNT(o.order_id) AS item_count FROM analytics.fact_order_items i "
        "JOIN analytics.fact_orders o ON o.order_id=i.order_id",
        row_keys={
            "fact_order_items": ("order_id", "order_item_id"),
            "fact_orders": ("order_id",),
        },
        source_grains={"fact_order_items": "order_item", "fact_orders": "order"},
        metric_grains={"item_count": "order_item"},
    )
    assert probes[0].table == "fact_order_items"


def test_unresolved_cte_group_alias_is_uncovered_not_an_invalid_probe():
    probes, uncovered = build_join_probes(
        "WITH x AS (SELECT i.price, DATE_TRUNC('month', o.created_at) AS month_start "
        "FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id) "
        "SELECT month_start AS time, SUM(price) AS gmv FROM x GROUP BY month_start",
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={"fact_order_items": "order_item", "fact_orders": "order"},
        metric_grains={"gmv": "order_item"},
    )
    assert not probes
    assert any(item["reason"] == "unresolved_group_expression" for item in uncovered)


def test_schema_qualification_resolves_cte_group_alias_before_merging():
    probes, uncovered = build_join_probes(
        "WITH x AS (SELECT i.price, DATE_TRUNC('month', o.order_purchase_timestamp) AS month_start "
        "FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id) "
        "SELECT month_start AS time, SUM(price) AS gmv FROM x GROUP BY month_start",
        row_keys={"fact_order_items": ("order_id", "order_item_id")},
        source_grains={"fact_order_items": "order_item", "fact_orders": "order"},
        metric_grains={"gmv": "order_item"},
        column_schema={"analytics": {
            "fact_order_items": {"order_id":"UNKNOWN", "order_item_id":"UNKNOWN", "price":"UNKNOWN"},
            "fact_orders": {"order_id":"UNKNOWN", "order_purchase_timestamp":"UNKNOWN"},
        }},
    )
    assert not uncovered and len(probes) == 1
    assert "GROUP BY DATE_TRUNC('MONTH', o.order_purchase_timestamp)" in probes[0].sql
    SqlPolicy().evaluate(probes[0].sql, complete_result=True, result_limit=1)


def test_legitimate_multimembership_keeps_the_full_group_identity():
    probes, _ = build_join_probes(
        "SELECT p.product_category_name, AVG(o.order_id) AS average_order_value "
        "FROM analytics.fact_orders o JOIN analytics.fact_order_items i ON i.order_id=o.order_id "
        "JOIN analytics.dim_products p ON p.product_id=i.product_id GROUP BY p.product_category_name",
        row_keys={
            "fact_orders": ("order_id",),
            "fact_order_items": ("order_id", "order_item_id"),
        },
        source_grains={"fact_orders": "order", "fact_order_items": "order_item"},
        metric_grains={"average_order_value": "order"},
    )
    # Numeric IDs only exercise identity semantics; they are not a business AOV.
    assert probes[0].table == "fact_orders"
    assert "GROUP BY p.product_category_name" in probes[0].sql


@pytest.mark.parametrize('query,high_risk', [
    ('SELECT SUM(i.price*p.payment_value) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id', True),
    ('SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id HAVING SUM(i.price)>0', False),
    ('WITH a AS (SELECT order_id, SUM(price) AS amount FROM analytics.fact_order_items GROUP BY order_id) SELECT SUM(a.amount) AS gmv FROM a JOIN analytics.fact_orders o ON o.order_id=a.order_id', False),
    ('WITH a AS (SELECT order_id, SUM(price) AS amount FROM analytics.fact_order_items GROUP BY order_id) SELECT SUM(a.amount) AS gmv FROM a JOIN analytics.fact_payments p ON p.order_id=a.order_id', True),
    ('SELECT SUM(i.price) OVER (PARTITION BY i.order_id) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id', True),
    ('WITH a AS (SELECT order_id, SUM(price) AS amount FROM analytics.fact_order_items GROUP BY order_id) SELECT SUM(a.amount) AS gmv FROM a JOIN analytics.unregistered_source x ON x.order_id=a.order_id', True),
])
def test_partial_join_risk_distinguishes_preserved_keys_from_cross_fact_fanout(query, high_risk):
    _, uncovered = build_join_probes(
        query, row_keys={'fact_order_items': ('order_id','order_item_id'), 'fact_orders': ('order_id',),
                         'fact_payments': ('order_id','payment_sequential')},
        source_grains={'fact_order_items':'order_item', 'fact_orders':'order', 'fact_payments':'payment'},
        metric_grains={'gmv':'order_item'},
    )
    assert uncovered
    assert any(item['high_risk'] for item in uncovered) is high_risk


@pytest.mark.parametrize('duplicate', [False,True])
def test_shared_projection_preserves_a_catalog_key_but_union_all_does_not(duplicate):
    projection = 'SELECT order_id FROM analytics.fact_orders'
    if duplicate:
        projection += ' UNION ALL SELECT order_id FROM analytics.fact_orders'
    sql = (f'WITH valid_orders AS ({projection}), amounts AS ('
        'SELECT i.order_id,SUM(i.price) AS amount FROM analytics.fact_order_items i '
        'JOIN valid_orders v ON v.order_id=i.order_id GROUP BY i.order_id) '
        'SELECT SUM(a.amount) AS gmv FROM amounts a JOIN valid_orders v ON v.order_id=a.order_id')
    _, uncovered = build_join_probes(sql,
        row_keys={'fact_order_items':('order_id','order_item_id'),'fact_orders':('order_id',)},
        source_grains={'fact_order_items':'order_item','fact_orders':'order'},metric_grains={'gmv':'order_item'})
    assert uncovered
    assert any(item['high_risk'] for item in uncovered) is duplicate


def test_summarize_diagnostics_does_not_invent_false_rejection_labels():
    from data_analysis_agent.join_evidence import summarize_join_evidence

    evidence = dict(status='partial', covered_aggregates=1, planned_checks=1,
                    uncovered_aggregates=1, elapsed_ms=10, rejected=True)
    summary = summarize_join_evidence([{'evidence': evidence}])
    assert summary['aggregate_coverage_ratio'] == .5
    assert summary['diagnostic_rejection_rate'] == 1
    assert summary['false_rejection_rate'] is None
    summary = summarize_join_evidence([{'evidence': evidence, 'expected_safe': True}])
    assert summary['false_rejection_rate'] == 1


def test_unused_unsafe_cte_does_not_reject_or_probe_a_safe_result():
    probes, uncovered = build_join_probes(
        'WITH unused AS (SELECT SUM(i.price*p.payment_value) AS amount '
        'FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id) '
        'SELECT SUM(price) AS gmv FROM analytics.fact_order_items',
        row_keys={'fact_order_items':('order_id','order_item_id'), 'fact_payments':('order_id','payment_sequential')},
        source_grains={'fact_order_items':'order_item','fact_payments':'payment'}, metric_grains={'gmv':'order_item'},
    )
    assert not probes and not uncovered
