from __future__ import annotations

import dataclasses

import pytest

from data_analysis_agent.olist_queryspec import (
    METRIC_SQL_REGISTRY,
    QUERY_SPEC_SCHEMA_VERSION,
    QuerySpec,
    QuerySpecValidationError,
    QueryTime,
    WorkspacePin,
    render_gold_sql,
    validate_query_spec,
)
from data_analysis_agent.sql_policy import SqlPolicy


ALL_METRICS = (
    "gmv",
    "paid_order_count",
    "average_delivery_days",
    "positive_review_rate",
    "item_count",
    "average_order_value",
    "average_review_score",
    "on_time_delivery_rate",
    "cancellation_rate",
    "freight_amount",
)


def _spec(**kwargs) -> QuerySpec:
    return QuerySpec.create(**kwargs)


def _assert_rejected(spec: QuerySpec, reason: str) -> None:
    with pytest.raises(QuerySpecValidationError) as exc_info:
        validate_query_spec(spec)
    assert exc_info.value.reason_code == reason


def test_query_spec_is_immutable_and_has_canonical_id() -> None:
    spec = _spec(metric_ids=("gmv",), result_shape="scalar")

    assert dataclasses.is_dataclass(spec)
    assert spec.schema_version == QUERY_SPEC_SCHEMA_VERSION
    assert spec.query_spec_id.startswith("qs_")
    assert spec.query_spec_id == spec.expected_query_spec_id()
    assert spec.canonical_json() == spec.canonical_json()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.metric_ids = ("item_count",)  # type: ignore[misc]
    assert validate_query_spec(spec) == spec


def test_mapping_round_trip_preserves_a_valid_canonical_query_spec() -> None:
    original = _spec(
        metric_ids=("gmv", "paid_order_count"),
        result_shape="state_grouped",
        time=QueryTime("absolute_range", "2017-01-01", "2018-01-01"),
    )

    recovered = QuerySpec.from_mapping(original.as_dict())

    assert recovered == original
    assert validate_query_spec(recovered) == original


@pytest.mark.parametrize("metric_id", ALL_METRICS)
def test_all_metrics_have_read_only_registry_definitions_and_scalar_gold(metric_id: str) -> None:
    spec = _spec(metric_ids=(metric_id,), result_shape="scalar")
    artifact = render_gold_sql(spec)
    definition = METRIC_SQL_REGISTRY[metric_id]

    assert definition.metric_id == metric_id
    assert definition.where_sql
    assert f"AS {metric_id}" in artifact.sql
    assert "LIMIT" not in artifact.sql.upper()
    assert artifact.required_result_columns == (metric_id,)
    assert artifact.sql_sha256 == artifact.evidence["sql_sha256"]
    assert SqlPolicy().evaluate(artifact.sql, role="analyst").status == "allowed"


def test_aov_gold_preserves_order_grain_before_average() -> None:
    artifact = render_gold_sql(_spec(metric_ids=("average_order_value",), result_shape="scalar"))

    assert "aov_order_totals_01" in artifact.sql
    assert "SUM(i.price) AS order_total" in artifact.sql
    assert "AVG(aov_order_totals_01.order_total) AS average_order_value" in artifact.sql
    assert "payment_value" not in artifact.sql


@pytest.mark.parametrize(
    ("metric_id", "expression", "required_filters"),
    [
        ("gmv", "SUM(i.price)", ("o.order_status NOT IN ('canceled', 'unavailable')",)),
        ("item_count", "COUNT(i.order_item_id)", ("o.order_status NOT IN ('canceled', 'unavailable')",)),
        ("freight_amount", "SUM(i.freight_value)", ("o.order_status NOT IN ('canceled', 'unavailable')",)),
        ("paid_order_count", "COUNT(DISTINCT o.order_id)", ("o.order_status NOT IN ('canceled', 'unavailable')",)),
        ("average_delivery_days", "AVG(EXTRACT(EPOCH FROM (o.order_delivered_customer_date - o.order_purchase_timestamp)) / 86400.0)", ("o.order_purchase_timestamp IS NOT NULL", "o.order_delivered_customer_date IS NOT NULL")),
        ("average_order_value", "AVG(order_totals.order_total)", ("o.order_status NOT IN ('canceled', 'unavailable')",)),
        ("positive_review_rate", "AVG(CASE WHEN r.review_score >= 4 THEN 1.0 ELSE 0.0 END)", ("r.review_score BETWEEN 1 AND 5",)),
        ("average_review_score", "AVG(r.review_score)", ("r.review_score BETWEEN 1 AND 5",)),
        ("on_time_delivery_rate", "AVG(CASE WHEN o.order_delivered_customer_date <= o.order_estimated_delivery_date THEN 1.0 ELSE 0.0 END)", ("o.order_status = 'delivered'", "o.order_purchase_timestamp IS NOT NULL", "o.order_delivered_customer_date IS NOT NULL", "o.order_estimated_delivery_date IS NOT NULL")),
        ("cancellation_rate", "AVG(CASE WHEN o.order_status = 'canceled' THEN 1.0 ELSE 0.0 END)", ("o.order_purchase_timestamp IS NOT NULL",)),
    ],
)
def test_metric_registry_encodes_frozen_formula_and_denominator_boundaries(
    metric_id: str,
    expression: str,
    required_filters: tuple[str, ...],
) -> None:
    definition = METRIC_SQL_REGISTRY[metric_id]

    assert definition.scalar_expression == expression
    assert definition.grouped_expression == expression
    assert definition.where_sql == required_filters


@pytest.mark.parametrize(
    ("shape", "metrics", "expected_columns", "program"),
    [
        ("scalar", ("gmv", "cancellation_rate"), ("gmv", "cancellation_rate"), "JP09_scalar_multi_metric"),
        ("state_grouped", ("gmv",), ("customer_state", "gmv"), "JP04_customer_geo_item"),
        ("state_grouped", ("paid_order_count",), ("customer_state", "paid_order_count"), "JP05_customer_geo_order"),
        ("state_grouped", ("average_review_score",), ("customer_state", "average_review_score"), "JP06_customer_geo_review"),
        ("state_grouped", ("gmv", "paid_order_count"), ("customer_state", "gmv", "paid_order_count"), "JP10_state_multi_metric"),
        ("category_grouped", ("gmv",), ("product_category_name", "gmv"), "JP07_category_item"),
        ("time_series", ("gmv", "paid_order_count"), ("gmv", "paid_order_count", "time"), "JP11_purchase_time_multi_metric"),
        ("time_series", ("positive_review_rate", "average_review_score"), ("positive_review_rate", "average_review_score", "time"), "JP12_review_time_multi_metric"),
    ],
)
def test_supported_shapes_have_stable_contract_and_policy_parse(
    shape: str,
    metrics: tuple[str, ...],
    expected_columns: tuple[str, ...],
    program: str,
) -> None:
    time = (
        QueryTime("series", "2017-01-01", "2017-02-01", "month")
        if shape == "time_series"
        else QueryTime("all_time")
    )
    spec = _spec(metric_ids=metrics, result_shape=shape, time=time)
    artifact = render_gold_sql(spec)

    assert spec.required_result_columns == expected_columns
    assert spec.join_program_id == program
    assert SqlPolicy().evaluate(artifact.sql, role="analyst").status == "allowed"
    assert artifact.sql.endswith(";")


def test_render_is_byte_stable_and_evidence_is_redacted() -> None:
    spec = _spec(
        metric_ids=("gmv", "paid_order_count"),
        result_shape="state_grouped",
        time=QueryTime("absolute_range", "2017-01-01", "2018-01-01"),
    )
    first = render_gold_sql(spec)
    second = render_gold_sql(spec)

    assert first.sql == second.sql
    assert first.sql_sha256 == second.sql_sha256
    assert first.evidence == second.evidence
    assert "question" not in first.evidence
    assert "password" not in first.evidence


def test_absolute_range_uses_half_open_filter_on_each_metric_time_field() -> None:
    artifact = render_gold_sql(
        _spec(
            metric_ids=("positive_review_rate", "average_review_score"),
            result_shape="time_series",
            time=QueryTime("series", "2017-01-01", "2018-01-01", "year"),
        )
    )

    assert "r.review_creation_date >= TIMESTAMP '2017-01-01'" in artifact.sql
    assert "r.review_creation_date < TIMESTAMP '2018-01-01'" in artifact.sql
    assert "date_trunc('year', r.review_creation_date) AS time" in artifact.sql


def test_from_mapping_rejects_stale_or_tampered_query_spec_id() -> None:
    spec = _spec(metric_ids=("gmv",), result_shape="scalar")
    payload = spec.as_dict()
    payload["query_spec_id"] = "qs_tampered"

    _assert_rejected(QuerySpec.from_mapping(payload), "invalid_query_spec")


@pytest.mark.parametrize(
    "spec",
    [
        _spec(metric_ids=("gmv", "gmv"), result_shape="scalar"),
        _spec(metric_ids=("gmv", "item_count", "freight_amount", "paid_order_count", "cancellation_rate"), result_shape="scalar"),
        _spec(metric_ids=("gmv", "positive_review_rate"), result_shape="time_series", time=QueryTime("series", "2017-01-01", "2018-01-01", "month")),
        _spec(metric_ids=("gmv",), result_shape="time_series", time=QueryTime("series", None, None, "day")),
        _spec(metric_ids=("gmv",), result_shape="scalar", attribution_rule_id="payment_allocation_v1"),
    ],
)
def test_frozen_coverage_and_attribution_boundaries_fail_closed(spec: QuerySpec) -> None:
    reason = "invalid_metric_ids" if len(set(spec.metric_ids)) != len(spec.metric_ids) or len(spec.metric_ids) > 4 else "coverage_shape_not_permitted"
    if spec.attribution_rule_id:
        reason = "attribution_not_frozen"
    if spec.result_shape == "time_series" and spec.time.grain == "day" and spec.time.start is None:
        reason = "invalid_time_contract"
    _assert_rejected(spec, reason)


def test_sensitive_and_attribution_dimensions_have_specific_rejection_codes() -> None:
    seller_spec = _spec(metric_ids=("gmv",), result_shape="category_grouped", dimension="seller_id")
    payment_spec = _spec(metric_ids=("gmv",), result_shape="state_grouped", dimension="payment_type")
    category_order_spec = _spec(metric_ids=("paid_order_count",), result_shape="category_grouped")

    _assert_rejected(seller_spec, "sensitive_dimension_not_displayable")
    _assert_rejected(payment_spec, "attribution_not_frozen")
    _assert_rejected(category_order_spec, "attribution_not_frozen")


def test_from_mapping_rejects_unsupported_query_features() -> None:
    payload = _spec(metric_ids=("gmv",), result_shape="scalar").as_dict()
    payload["limit"] = 20

    with pytest.raises(QuerySpecValidationError) as exc_info:
        QuerySpec.from_mapping(payload)

    assert exc_info.value.reason_code == "unsupported_query_feature"


def test_version_drift_and_result_column_drift_are_rejected() -> None:
    original = _spec(metric_ids=("gmv",), result_shape="scalar")
    stale_workspace = dataclasses.replace(
        original,
        workspace=dataclasses.replace(original.workspace, catalog_version="olist-catalog-v1"),
    )
    tampered_columns = dataclasses.replace(
        original,
        required_result_columns=("wrong",),
    )

    _assert_rejected(stale_workspace, "workspace_version_mismatch")
    _assert_rejected(tampered_columns, "result_columns_do_not_match_contract")


def test_workspace_pin_is_explicit_and_not_an_input_for_user_text() -> None:
    pin = WorkspacePin.current()
    assert pin.workspace_id == "olist-demo"
    assert pin.prompt_version == "olist-candidate-sql-v1"
    assert "question" not in pin.as_dict()
