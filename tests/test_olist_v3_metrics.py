from __future__ import annotations

import os

import pandas as pd
import psycopg2
import pytest

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE
from data_analysis_agent.olist_queryspec import (
    QuerySpec,
    QueryTime,
    WorkspacePin,
    render_gold_sql,
)
from data_analysis_agent.olist_schema_link_plan import derive_schema_link_plan
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.semantic_catalog import CatalogRetriever, ResultContract
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.sql_policy import SqlPolicy
from vanna.core.user import User


V3_METRICS = (
    "unique_customer_count",
    "review_count",
    "canceled_order_count",
    "delivered_order_count",
    "unavailable_order_count",
    "average_items_per_order",
    "average_item_price",
    "approval_latency_days",
    "carrier_handoff_days",
)


@pytest.fixture(scope="module")
def catalog():
    return CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()


def _spec(catalog, **kwargs) -> QuerySpec:
    return QuerySpec.create(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        **kwargs,
    )


def _user() -> User:
    return User(id="olist-v3-metric-test", group_memberships=["analyst"])


def test_v3_catalog_is_isolated_from_the_default_v2_snapshot(catalog) -> None:
    assert catalog.catalog_version == "olist-catalog-v3"
    assert catalog.metric_version == "0.3-proposal"
    assert len(catalog.metrics) == 19
    assert set(V3_METRICS) <= set(catalog.metrics_by_id)


def test_v3_workspace_pin_loads_its_own_catalog_without_a_caller_override() -> None:
    spec = QuerySpec.create_validated(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        metric_ids=("review_count",),
        result_shape="scalar",
    )
    artifact = render_gold_sql(spec)

    assert spec.workspace.catalog_version == "olist-catalog-v3"
    assert artifact.metric_ids == ("review_count",)


@pytest.mark.parametrize(
    ("question", "metric_id"),
    [
        ("统计独立客户数", "unique_customer_count"),
        ("看看有效评价数", "review_count"),
        ("取消订单数是多少", "canceled_order_count"),
        ("统计已送达订单数", "delivered_order_count"),
        ("查询不可用订单数", "unavailable_order_count"),
        ("平均每单商品件数", "average_items_per_order"),
        ("按品类统计商品均价", "average_item_price"),
        ("平均订单确认时长", "approval_latency_days"),
        ("下单到交运平均天数", "carrier_handoff_days"),
    ],
)
def test_v3_catalog_aliases_retrieve_the_intended_metric_and_contract(
    question: str, metric_id: str, catalog
) -> None:
    selection = CatalogRetriever(catalog).retrieve(question, _user())
    contract = ResultContract.from_selection(selection, question, catalog=catalog)

    assert selection.trace.selected_metrics == (metric_id,)
    assert contract.metric_result_columns == (metric_id,)
    assert contract.result_column_labels[metric_id] == catalog.metrics_by_id[metric_id].name


def test_specific_alias_deduplication_preserves_a_separate_generic_metric(catalog) -> None:
    selection = CatalogRetriever(catalog).retrieve("有效订单数和取消订单数", _user())

    assert selection.trace.selected_metrics == (
        "canceled_order_count",
        "paid_order_count",
    )


@pytest.mark.parametrize("metric_id", V3_METRICS)
def test_each_v3_metric_has_a_policy_approved_scalar_gold(metric_id: str, catalog) -> None:
    artifact = render_gold_sql(_spec(catalog, metric_ids=(metric_id,), result_shape="scalar"), catalog)

    assert artifact.metric_ids == (metric_id,)
    assert f"AS {metric_id}" in artifact.sql
    assert artifact.sql_sha256 == artifact.evidence["sql_sha256"]
    assert SqlPolicy().evaluate(artifact.sql, role="analyst").status == "allowed"
    assert derive_schema_link_plan(_spec(catalog, metric_ids=(metric_id,), result_shape="scalar"), catalog).metric_programs[0].metric_id == metric_id


def test_unique_customer_count_uses_customer_unique_id_and_state_join(catalog) -> None:
    spec = _spec(
        catalog,
        metric_ids=("unique_customer_count",),
        result_shape="state_grouped",
        time=QueryTime("absolute_range", "2017-01-01", "2018-01-01"),
    )
    artifact = render_gold_sql(spec, catalog)
    plan = derive_schema_link_plan(spec, catalog)

    assert "COUNT(DISTINCT c.customer_unique_id) AS unique_customer_count" in artifact.sql
    assert "o.order_status NOT IN ('canceled', 'unavailable')" in artifact.sql
    assert plan.metric_programs[0].join_ids == ("orders_customers",)
    assert "c.customer_unique_id" in plan.metric_programs[0].required_column_refs
    assert SqlPolicy().evaluate(artifact.sql, role="analyst").status == "allowed"


def test_average_items_per_order_keeps_order_level_intermediate(catalog) -> None:
    spec = _spec(
        catalog,
        metric_ids=("average_items_per_order",),
        result_shape="state_grouped",
    )
    artifact = render_gold_sql(spec, catalog)
    plan = derive_schema_link_plan(spec, catalog)

    assert "COUNT(i.order_item_id) AS item_count" in artifact.sql
    assert "AVG(items_per_order_01.item_count) AS average_items_per_order" in artifact.sql
    assert plan.metric_programs[0].source_grain == "order_item_count"
    assert plan.metric_programs[0].dedup_rule_id == "preaggregate_order_item_count"
    assert SqlPolicy().evaluate(artifact.sql, role="analyst").status == "allowed"


@pytest.mark.parametrize(
    ("metric_id", "required_filter", "expression"),
    [
        (
            "approval_latency_days",
            "o.order_approved_at >= o.order_purchase_timestamp",
            "o.order_approved_at - o.order_purchase_timestamp",
        ),
        (
            "carrier_handoff_days",
            "o.order_delivered_carrier_date >= o.order_purchase_timestamp",
            "o.order_delivered_carrier_date - o.order_purchase_timestamp",
        ),
    ],
)
def test_latency_metrics_enforce_nonnegative_timestamp_boundaries(
    metric_id: str, required_filter: str, expression: str, catalog
) -> None:
    spec = _spec(
        catalog,
        metric_ids=(metric_id,),
        result_shape="time_series",
        time=QueryTime("series", "2017-01-01", "2018-01-01", "month"),
    )
    artifact = render_gold_sql(spec, catalog)

    assert expression in artifact.sql
    assert required_filter in artifact.sql
    assert "date_trunc('month', o.order_purchase_timestamp) AS time" in artifact.sql
    assert SqlPolicy().evaluate(artifact.sql, role="analyst").status == "allowed"


def test_average_item_price_is_the_only_new_category_safe_metric(catalog) -> None:
    spec = _spec(
        catalog,
        metric_ids=("average_item_price",),
        result_shape="category_grouped",
    )
    artifact = render_gold_sql(spec, catalog)

    assert "AVG(i.price) AS average_item_price" in artifact.sql
    assert "JOIN analytics.dim_products AS p ON i.product_id = p.product_id" in artifact.sql
    assert SqlPolicy().evaluate(artifact.sql, role="analyst").status == "allowed"


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("case_id", "metric_ids", "result_shape", "query_time", "expected_state", "expected_rows"),
    [
        (
            "distinct_customer_by_state",
            ("unique_customer_count",),
            "state_grouped",
            QueryTime("absolute_range", "2017-01-01", "2018-01-01"),
            "valid",
            27,
        ),
        (
            "review_count_by_state",
            ("review_count",),
            "state_grouped",
            QueryTime("absolute_range", "2017-01-01", "2018-01-01"),
            "valid",
            27,
        ),
        (
            "average_item_price_by_category",
            ("average_item_price",),
            "category_grouped",
            QueryTime("absolute_range", "2017-01-01", "2018-01-01"),
            "valid",
            72,
        ),
        (
            "two_stage_average_items_by_state",
            ("average_items_per_order",),
            "state_grouped",
            QueryTime("absolute_range", "2017-01-01", "2018-01-01"),
            "valid",
            27,
        ),
        (
            "two_stage_average_items_monthly",
            ("average_items_per_order",),
            "time_series",
            QueryTime("series", "2017-01-01", "2018-01-01", "month"),
            "valid",
            12,
        ),
        (
            "purchase_latency_pair_quarterly",
            ("approval_latency_days", "carrier_handoff_days"),
            "time_series",
            QueryTime("series", "2017-01-01", "2018-01-01", "quarter"),
            "valid",
            4,
        ),
        (
            "review_metrics_monthly",
            ("review_count", "average_review_score"),
            "time_series",
            QueryTime("series", "2017-01-01", "2018-01-01", "month"),
            "valid",
            12,
        ),
        # A scalar COUNT over an empty range is a known zero, and is safe to
        # show. It must not be conflated with an empty grouped/time series.
        (
            "empty_window_scalar_count_is_zero",
            ("canceled_order_count",),
            "scalar",
            QueryTime("absolute_range", "2025-01-01", "2025-02-01"),
            "valid",
            1,
        ),
        # AVG over an empty range returns one NULL-bearing aggregate row. The
        # ResultValidator must reject it rather than inventing a zero.
        (
            "empty_window_scalar_average_is_refused",
            ("average_item_price",),
            "scalar",
            QueryTime("absolute_range", "2025-01-01", "2025-02-01"),
            "refuse",
            1,
        ),
        # A grouped time series has no buckets in an empty range, therefore
        # the correct user-facing state is clarification, not a numeric zero.
        (
            "empty_window_series_requires_clarification",
            ("canceled_order_count",),
            "time_series",
            QueryTime("series", "2025-01-01", "2025-02-01", "month"),
            "needs_clarification",
            0,
        ),
    ],
)
def test_v3_gold_regression_covers_grouping_series_and_empty_windows(
    case_id: str,
    metric_ids: tuple[str, ...],
    result_shape: str,
    query_time: QueryTime,
    expected_state: str,
    expected_rows: int,
    catalog,
) -> None:
    """Exercise the entire deterministic v3 Gold path against PostgreSQL.

    The case table is deliberately small and structural: it protects every
    newly introduced fact path, the order-level intermediate, legal direct
    dimensions, both time families, and the distinct empty-result semantics.
    It is a release gate for future coverage seeds, not a training set.
    """
    if os.getenv("RUN_PROJECT_DB") != "1":
        pytest.skip("set RUN_PROJECT_DB=1 to validate v3 Gold against the project database")

    spec = _spec(
        catalog,
        metric_ids=metric_ids,
        result_shape=result_shape,
        time=query_time,
    )
    artifact = render_gold_sql(spec, catalog)
    policy = SqlPolicy(workspace=OLIST_V3_WORKSPACE).evaluate(artifact.sql, role="analyst")

    assert policy.status == "allowed", case_id
    assert tuple(artifact.required_result_columns) == spec.required_result_columns

    connection = psycopg2.connect(
        host="/tmp",
        port=35434,
        database="data_analysis_agent",
        user="daa_analytics_reader",
    )
    connection.set_session(readonly=True, autocommit=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute(policy.final_sql)
            frame = pd.DataFrame(cursor.fetchall(), columns=[item.name for item in cursor.description])
    finally:
        connection.close()

    validation = ResultValidator(max_rows=200).validate(
        frame,
        required_columns=artifact.required_result_columns,
        metric_columns=metric_ids,
        time_column="time" if result_shape == "time_series" else None,
        time_bucket_grain=query_time.grain if result_shape == "time_series" else None,
        requested_start=query_time.start,
        requested_end=query_time.end_exclusive,
        exact_columns=True,
        metric_value_constraints={
            metric_id: catalog.metrics_by_id[metric_id].result_value_constraints
            for metric_id in metric_ids
        },
    )

    assert len(frame) == expected_rows, case_id
    assert validation.state == expected_state, case_id
    if case_id == "empty_window_scalar_count_is_zero":
        assert frame.iloc[0]["canceled_order_count"] == 0
    if case_id == "empty_window_scalar_average_is_refused":
        assert validation.null_metric_columns == ("average_item_price",)


@pytest.mark.postgres
def test_v3_scalar_gold_executes_as_reader_and_satisfies_result_contract(catalog) -> None:
    if os.getenv("RUN_PROJECT_DB") != "1":
        pytest.skip("set RUN_PROJECT_DB=1 to validate v3 Gold against the project database")
    connection = psycopg2.connect(
        host="/tmp",
        port=35434,
        database="data_analysis_agent",
        user="daa_analytics_reader",
    )
    connection.set_session(readonly=True, autocommit=False)
    try:
        with connection.cursor() as cursor:
            for metric_id in V3_METRICS:
                question = catalog.metrics_by_id[metric_id].aliases[0]
                selection = CatalogRetriever(catalog).retrieve(question, _user())
                contract = ResultContract.from_selection(selection, question, catalog=catalog)
                spec = _spec(catalog, metric_ids=(metric_id,), result_shape="scalar")
                sql = SqlPolicy(workspace=OLIST_V3_WORKSPACE).evaluate(
                    render_gold_sql(spec, catalog).sql, role="analyst"
                ).final_sql
                cursor.execute(sql)
                frame = pd.DataFrame(cursor.fetchall(), columns=[item.name for item in cursor.description])
                validation = ResultValidator(max_rows=200).validate(
                    frame,
                    required_columns=contract.required_result_columns,
                    metric_columns=contract.metric_result_columns,
                    exact_columns=contract.exact_result_columns,
                    metric_value_constraints=contract.metric_value_constraints,
                )
                assert validation.state == "valid", metric_id
    finally:
        connection.close()
