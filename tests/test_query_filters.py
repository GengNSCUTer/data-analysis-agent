"""Behavioral regression for the previously blocked Catalog filter paths."""
from dataclasses import replace
import json

import pytest
from vanna.core.user import User

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.semantic_intent import parse_semantic_intent, route_semantic_intent, select_intent_catalog
from data_analysis_agent.semantic_intent import resolve_state_intent
from data_analysis_agent.analysis_state import AnalysisState
from data_analysis_agent.plan_sql_guard import sql_plan_mismatch


def _plan(filters, *, metric="gmv", dimensions=(), catalog=None):
    catalog = catalog or CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    user = User(id="filter-test", group_memberships=["analyst"])
    router = QuestionRouter(CatalogRetriever(catalog, max_tables=6), max_result_rows=100_000, full_results=True)
    intent = parse_semantic_intent(json.dumps({"request_kind": "data_query", "metric_ids": [metric],
        "dimensions": list(dimensions), "filters": filters}), catalog, user=user)
    selection = select_intent_catalog(intent, router.retriever, user, question="任意自然问法")
    route = route_semantic_intent(intent, router, selection)
    plan = QueryPlan.from_selection(selection, "", route, {"semantic_intent": intent.as_dict()},
        result_budget=ResultBudget.aggregate(execution_max_rows=100_000)) if route.should_generate_sql else None
    return route, plan


@pytest.mark.parametrize("filters", [
    [{"dimension": "product_category_name", "value": "bed_bath_table"}],
    [{"dimension": "customer_state", "value": "SP"}, {"dimension": "product_category_name", "value": "cama_mesa_banho"}],
    [{"dimension": "customer_city", "value": "sao paulo"}],
    [{"dimension": "seller_state", "value": "RJ"}],
    [{"dimension": "payment_type", "value": "credit_card"}],
])
def test_catalog_filters_reach_plan_and_prompt(filters):
    route, plan = _plan(filters)
    assert route.should_generate_sql
    assert [{"dimension": f["dimension"], "value": f["value"]} for f in plan.as_dict()["filters"]] == filters
    assert plan.dimensions == ()
    for f in filters:
        assert f["value"] in plan.prompt_context()
    if filters[0]["dimension"] == "product_category_name":
        assert ("dim_category_translation", "product_category_name_english") in plan.filters[0].sources


def test_unsupported_metric_dimension_is_a_capability_response_not_missing_context():
    route, plan = _plan([{"dimension": "seller_state", "value": "SP"}], metric="average_order_value")
    assert route.state == "unsupported" and not route.needs_clarification
    assert plan is None


def test_sensitive_catalog_source_cannot_be_bound_as_filter():
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    tables = tuple(replace(t, columns=tuple(replace(c, sensitive=True) if c.name == "customer_city" else c for c in t.columns)) for t in catalog.tables)
    route, plan = _plan([{"dimension": "customer_city", "value": "sao paulo"}], catalog=replace(catalog, tables=tables))
    assert route.state == "unsupported" and plan is None


FILTERS = [
    {"dimension": "customer_state", "value": "SP", "sources": [{"table": "dim_customers", "column": "customer_state"}]},
    {"dimension": "product_category_name", "value": "bed_bath_table", "sources": [
        {"table": "dim_products", "column": "product_category_name"},
        {"table": "dim_category_translation", "column": "product_category_name_english"}]},
]
BASE = "SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id JOIN analytics.dim_customers c ON c.customer_id=o.customer_id JOIN analytics.dim_products p ON p.product_id=i.product_id JOIN analytics.dim_category_translation t ON t.product_category_name=p.product_category_name "


@pytest.mark.parametrize("condition", [
    "c.customer_state IN ('SP', 'RJ')",
    "(c.customer_state='SP' OR c.customer_state='RJ')",
    "LOWER(c.customer_state) IN ('sp', 'rj')",
])
def test_multiple_values_on_one_dimension_are_one_allowed_set(condition):
    filters = [FILTERS[0], {**FILTERS[0], "value": "RJ"}]
    assert sql_plan_mismatch(BASE + "WHERE " + condition, {"metric_ids": ["gmv"], "filters": filters}) is None


@pytest.mark.parametrize("condition", [
    "c.customer_state='SP'",
    "c.customer_state IN ('SP','RJ','MG')",
    "c.customer_state='SP' OR c.customer_state='RJ' OR i.price>0",
    "c.customer_state='SP' AND c.customer_state='RJ'",
    "c.customer_city IN ('SP','RJ')",
    "COALESCE(c.customer_state,'SP') IN ('SP','RJ')",
])
def test_multiple_value_check_rejects_missing_extra_or_wrong_source(condition):
    filters = [FILTERS[0], {**FILTERS[0], "value": "RJ"}]
    assert sql_plan_mismatch(BASE + "WHERE " + condition, {"metric_ids": ["gmv"], "filters": filters})


@pytest.mark.parametrize("sql", [
    BASE + "WHERE c.customer_state='SP' AND t.product_category_name_english='bed_bath_table'",
    BASE + "WHERE 'SP'=c.customer_state AND t.product_category_name_english IN ('bed_bath_table')",
    BASE + "WHERE LOWER(c.customer_state)='sp' AND COALESCE(t.product_category_name_english,p.product_category_name)='bed_bath_table'",
    "WITH a AS (SELECT c.customer_state AS state, t.product_category_name_english AS category, i.price FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id JOIN analytics.dim_customers c ON c.customer_id=o.customer_id JOIN analytics.dim_products p ON p.product_id=i.product_id JOIN analytics.dim_category_translation t ON t.product_category_name=p.product_category_name) SELECT SUM(price) AS gmv FROM a WHERE state='SP' AND category='bed_bath_table'",
    "WITH a AS (" + BASE + "WHERE c.customer_state='SP' AND t.product_category_name_english='bed_bath_table') SELECT gmv FROM a",
])
def test_valid_filter_shapes_are_not_blocked(sql):
    assert sql_plan_mismatch(sql, {"metric_ids": ["gmv"], "filters": FILTERS}) is None


@pytest.mark.parametrize("suffix", [
    "WHERE c.customer_state='SP' /* bed_bath_table */",
    "WHERE c.customer_state='SP' AND t.product_category_name_english!='bed_bath_table'",
    "WHERE c.customer_state='SP' AND (t.product_category_name_english='bed_bath_table' OR TRUE)",
    "WHERE c.customer_state='SP' AND t.product_category_name='bed_bath_table'",
    "WHERE c.customer_state='SP' AND COALESCE('bed_bath_table',t.product_category_name_english)='bed_bath_table'",
    "WHERE c.customer_state='SP' AND NOT(t.product_category_name_english='bed_bath_table')",
])
def test_ineffective_or_wrong_filter_cannot_supply_evidence(suffix):
    assert sql_plan_mismatch(BASE + suffix, {"metric_ids": ["gmv"], "filters": FILTERS})


def test_unused_cte_does_not_satisfy_filter():
    sql = "WITH unused AS (SELECT customer_state FROM analytics.dim_customers WHERE customer_state='SP') SELECT SUM(price) AS gmv FROM analytics.fact_order_items"
    assert sql_plan_mismatch(sql, {"metric_ids": ["gmv"], "filters": FILTERS[:1]})


def test_every_metric_branch_needs_its_filter():
    first = "SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id JOIN analytics.dim_customers c ON c.customer_id=o.customer_id WHERE c.customer_state='SP'"
    second = "SELECT COUNT(*) AS item_count FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id JOIN analytics.dim_customers c ON c.customer_id=o.customer_id"
    sql = f"WITH a AS ({first}), b AS ({second}) SELECT a.gmv,b.item_count FROM a CROSS JOIN b"
    plan = {"metric_ids": ["gmv", "item_count"], "filters": FILTERS[:1]}
    assert sql_plan_mismatch(sql, plan)
    assert sql_plan_mismatch(sql.replace(second, second + " WHERE c.customer_state='SP'"), plan) is None


def test_filtered_order_cte_and_correlated_exists_are_supported():
    plan = {"metric_ids": ["gmv"], "filters": FILTERS[:1]}
    sql = "WITH a AS (SELECT o.order_id FROM analytics.fact_orders o JOIN analytics.dim_customers c ON c.customer_id=o.customer_id WHERE c.customer_state='SP') SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN a ON a.order_id=i.order_id"
    assert sql_plan_mismatch(sql, plan) is None
    sql = "SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id WHERE EXISTS(SELECT 1 FROM analytics.dim_customers c WHERE c.customer_id=o.customer_id AND c.customer_state='SP')"
    assert sql_plan_mismatch(sql, plan) is None
    assert sql_plan_mismatch(sql.replace("c.customer_id=o.customer_id AND ", ""), plan)


def test_fixed_group_does_not_require_redundant_top_n_limit():
    plan = {"metric_ids": ["gmv"], "filters": FILTERS,
            "dimensions": ["product_category_name"], "top_n": {"product_category_name": 10}}
    sql = BASE.replace("SUM(i.price) AS gmv", "'bed_bath_table' AS product_category_name, SUM(i.price) AS gmv")
    sql += "WHERE c.customer_state='SP' AND t.product_category_name_english='bed_bath_table'"
    assert sql_plan_mismatch(sql, plan) is None
    # A different, non-fixed grouping must still respect the requested bound.
    plan.update(dimensions=["customer_city"], top_n={"customer_city": 10})
    assert sql_plan_mismatch(sql, plan)


def test_key_membership_subquery_is_not_mistaken_for_missing_filter():
    plan = {"metric_ids": ["gmv"], "filters": FILTERS[:1]}
    sql = "WITH a AS (SELECT o.order_id FROM analytics.fact_orders o JOIN analytics.dim_customers c ON c.customer_id=o.customer_id WHERE c.customer_state='SP'), b AS (SELECT i.order_id, SUM(i.price) AS amount FROM analytics.fact_order_items i WHERE i.order_id IN (SELECT order_id FROM a) GROUP BY i.order_id) SELECT SUM(amount) AS gmv FROM b"
    assert sql_plan_mismatch(sql, plan) is None
    assert sql_plan_mismatch(sql.replace("SELECT order_id FROM a", "SELECT 'SP' AS customer_state FROM a"), plan)
    assert sql_plan_mismatch(sql.replace("WHERE i.order_id IN", "WHERE i.product_id IN"), plan)


def test_removing_one_dimension_preserves_other_conditions():
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    user = User(id="filter-test", group_memberships=["analyst"])
    state = replace(AnalysisState.new(OLIST_RUNTIME_WORKSPACE), metric_ids=("gmv",),
                    filters=("customer_state=RJ", "product_category_name=bed_bath_table"))
    intent = parse_semantic_intent(json.dumps({"request_kind": "data_query", "action": "update_task",
        "state_patch": {"operations": [{"op": "remove", "field": "filters",
                                        "value": [{"dimension": "customer_state", "value": "RJ"}]}]}}),
        catalog, user=user)
    resolved, updated = resolve_state_intent(intent, state, catalog, user, message_count=12)
    assert updated.filters == ("product_category_name=bed_bath_table",)
    assert resolved.metric_ids == ("gmv",)


def test_resolved_display_value_still_accepts_authorized_coalesce_fallback():
    filters = [FILTERS[0], {**FILTERS[1], "value_sources": [
        {"table": "dim_category_translation", "column": "product_category_name_english"}]}]
    plan = {"metric_ids": ["gmv"], "filters": filters}
    prefix = BASE + "WHERE c.customer_state='SP' AND "
    assert sql_plan_mismatch(prefix + "COALESCE(t.product_category_name_english,p.product_category_name)='bed_bath_table'", plan) is None
    assert sql_plan_mismatch(prefix + "p.product_category_name='bed_bath_table'", plan)
    assert sql_plan_mismatch(prefix + "COALESCE(p.product_category_name,t.product_category_name_english)='bed_bath_table'", plan)
    assert sql_plan_mismatch(prefix + "COALESCE(t.product_category_name_english,'bed_bath_table')='bed_bath_table'", plan)


def test_catalog_lookup_key_rejects_real_fanout_but_accepts_cte_and_aggregate_joins():
    _, plan = _plan([{ "dimension": "customer_state", "value": "SP"},
                     {"dimension": "product_category_name", "value": "bed_bath_table"}])
    prefix = ("WITH fi AS (SELECT i.product_id AS pid, p.product_category_name, i.price "
              "FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id "
              "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
              "JOIN analytics.dim_products p ON p.product_id=i.product_id "
              "JOIN analytics.dim_category_translation t ON t.product_category_name=p.product_category_name "
              "WHERE c.customer_state='SP' AND t.product_category_name_english='bed_bath_table') ")
    bad = prefix + "SELECT SUM(fi.price) AS gmv FROM fi JOIN analytics.dim_products p ON fi.product_category_name=p.product_category_name"
    assert "多对一" in sql_plan_mismatch(bad, plan.as_dict())
    good = bad.replace("fi.product_category_name=p.product_category_name", "fi.pid=p.product_id")
    assert sql_plan_mismatch(good, plan.as_dict()) is None
    good = prefix + "SELECT SUM(fi.price) AS gmv FROM fi JOIN analytics.dim_category_translation t ON fi.product_category_name=t.product_category_name"
    assert sql_plan_mismatch(good, plan.as_dict()) is None
    derived = prefix.rstrip() + ", totals AS (SELECT product_category_name,SUM(price) AS amount FROM fi GROUP BY product_category_name) SELECT SUM(a.amount) AS gmv FROM totals a JOIN totals b ON a.product_category_name=b.product_category_name"
    assert sql_plan_mismatch(derived, plan.as_dict()) is None
