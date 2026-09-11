from __future__ import annotations

import dataclasses

import pytest

from data_analysis_agent.olist_queryspec import (
    METRIC_SQL_REGISTRY,
    QuerySpec,
    QueryTime,
    WorkspacePin,
)
from data_analysis_agent.olist_schema_link_plan import (
    SCHEMA_LINK_PLAN_SCHEMA_VERSION,
    SCHEMA_LINK_REGISTRY_VERSION,
    SchemaLinkPlan,
    SchemaLinkPlanValidationError,
    derive_schema_link_plan,
    validate_schema_link_plan,
)
from data_analysis_agent.semantic_catalog import CatalogLoader


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


def _spec(**kwargs: object) -> QuerySpec:
    return QuerySpec.create_validated(**kwargs)


def _rejected(plan: SchemaLinkPlan, spec: QuerySpec, reason: str) -> None:
    with pytest.raises(SchemaLinkPlanValidationError) as exc_info:
        validate_schema_link_plan(plan, spec)
    assert exc_info.value.reason_code == reason


@pytest.mark.parametrize("metric_id", ALL_METRICS)
def test_each_frozen_metric_derives_one_reproducible_scalar_program(
    metric_id: str,
) -> None:
    spec = _spec(metric_ids=(metric_id,), result_shape="scalar")

    first = derive_schema_link_plan(spec)
    second = derive_schema_link_plan(spec)
    program = first.metric_programs[0]

    assert first == second
    assert first.schema_version == SCHEMA_LINK_PLAN_SCHEMA_VERSION
    assert first.registry_version == SCHEMA_LINK_REGISTRY_VERSION
    assert first.schema_link_plan_id.startswith("slp_")
    assert first.schema_link_plan_id == first.expected_schema_link_plan_id()
    assert first.query_spec_id == spec.query_spec_id
    assert first.workspace == spec.workspace
    assert first.time == spec.time
    assert first.required_result_columns == spec.required_result_columns
    assert program.metric_id == metric_id
    assert program.cte_name == f"m01_{metric_id}"
    assert program.time_owner == METRIC_SQL_REGISTRY[metric_id].time_field
    assert program.result_alias == metric_id
    assert program.group_key_refs == ()
    assert first.final_merge.strategy == "single_metric_cte"
    assert first.final_merge.key_alias is None
    assert validate_schema_link_plan(first, spec) == first


def test_scalar_multi_metric_uses_independent_ctes_and_cross_join_merge() -> None:
    spec = _spec(
        metric_ids=("gmv", "cancellation_rate", "positive_review_rate"),
        result_shape="scalar",
    )
    plan = derive_schema_link_plan(spec)

    assert [item.cte_name for item in plan.metric_programs] == [
        "m01_gmv",
        "m02_cancellation_rate",
        "m03_positive_review_rate",
    ]
    assert plan.final_merge.as_dict() == {
        "strategy": "cross_join_metric_ctes",
        "key_alias": None,
        "output_columns": ["gmv", "cancellation_rate", "positive_review_rate"],
    }
    assert plan.metric_programs[2].time_owner == "r.review_creation_date"


def test_state_grouped_review_program_declares_minimal_review_order_customer_path() -> (
    None
):
    spec = _spec(
        metric_ids=("positive_review_rate",),
        result_shape="state_grouped",
        time=QueryTime("absolute_range", "2017-01-01", "2018-01-01"),
    )
    program = derive_schema_link_plan(spec).metric_programs[0]

    assert [item.as_dict() for item in program.relation_aliases] == [
        {"relation": "analytics.fact_reviews", "alias": "r"},
        {"relation": "analytics.fact_orders", "alias": "o"},
        {"relation": "analytics.dim_customers", "alias": "c"},
    ]
    assert program.join_ids == ("orders_reviews", "orders_customers")
    assert program.group_key_refs == ("c.customer_state",)
    assert program.filter_rule_ids == (
        "valid_review_score",
        "exclude_null_customer_state",
    )
    assert program.time_owner == "r.review_creation_date"


def test_category_grouped_item_program_declares_order_item_product_path() -> None:
    spec = _spec(metric_ids=("gmv",), result_shape="category_grouped")
    plan = derive_schema_link_plan(spec)
    program = plan.metric_programs[0]

    assert [item.alias for item in program.relation_aliases] == ["o", "i", "p"]
    assert program.join_ids == ("orders_items", "items_products")
    assert program.group_key_refs == ("p.product_category_name",)
    assert program.filter_rule_ids == (
        "exclude_canceled_unavailable",
        "exclude_null_product_category",
    )
    assert plan.final_merge.key_alias == "product_category_name"


def test_state_grouped_aov_keeps_order_total_grain_and_preaggregation_rule() -> None:
    spec = _spec(metric_ids=("average_order_value",), result_shape="state_grouped")
    program = derive_schema_link_plan(spec).metric_programs[0]

    assert program.source_grain == "order_total"
    assert program.dedup_rule_id == "preaggregate_order_price"
    assert program.aggregation_rule_id == "average_order_total"
    assert [item.alias for item in program.relation_aliases] == ["o", "i", "c"]
    assert program.join_ids == ("orders_items", "orders_customers")
    assert program.group_key_refs == ("c.customer_state",)


def test_time_series_multi_metric_keeps_each_time_owner_and_full_outer_time_merge() -> (
    None
):
    spec = _spec(
        metric_ids=("gmv", "paid_order_count", "average_order_value"),
        result_shape="time_series",
        time=QueryTime("series", "2017-01-01", "2018-01-01", "month"),
    )
    plan = derive_schema_link_plan(spec)

    assert plan.final_merge.as_dict() == {
        "strategy": "full_outer_join_on_time",
        "key_alias": "time",
        "output_columns": ["gmv", "paid_order_count", "average_order_value", "time"],
    }
    assert all(
        program.time_owner == "o.order_purchase_timestamp"
        and program.group_key_refs == ("o.order_purchase_timestamp",)
        for program in plan.metric_programs
    )


def test_mapping_round_trip_and_immutability_preserve_plan() -> None:
    spec = _spec(metric_ids=("gmv",), result_shape="scalar")
    original = derive_schema_link_plan(spec)
    recovered = SchemaLinkPlan.from_mapping(original.as_dict())

    assert recovered == original
    assert validate_schema_link_plan(recovered, spec) == original
    with pytest.raises(dataclasses.FrozenInstanceError):
        recovered.join_program_id = "JP99"  # type: ignore[misc]


def test_tampered_program_field_or_plan_id_fails_closed() -> None:
    spec = _spec(metric_ids=("paid_order_count",), result_shape="state_grouped")
    original = derive_schema_link_plan(spec)
    tampered = original.as_dict()
    tampered["metric_programs"][0]["time_owner"] = "r.review_creation_date"
    altered_program = SchemaLinkPlan.from_mapping(tampered)
    _rejected(altered_program, spec, "invalid_schema_link_plan")

    forged = original.as_dict()
    forged["schema_link_plan_id"] = "slp_forged"
    _rejected(SchemaLinkPlan.from_mapping(forged), spec, "invalid_schema_link_plan")


def test_plan_cannot_be_reused_for_a_different_validated_query_spec() -> None:
    scalar = _spec(metric_ids=("gmv",), result_shape="scalar")
    state = _spec(metric_ids=("gmv",), result_shape="state_grouped")

    _rejected(derive_schema_link_plan(scalar), state, "schema_link_plan_mismatch")


def test_workspace_version_drift_is_rejected_before_plan_derivation() -> None:
    stale_workspace = dataclasses.replace(
        WorkspacePin.current(), catalog_version="olist-catalog-stale"
    )
    stale_spec = QuerySpec.create(
        metric_ids=("gmv",), result_shape="scalar", workspace=stale_workspace
    )

    with pytest.raises(SchemaLinkPlanValidationError) as exc_info:
        derive_schema_link_plan(stale_spec)

    assert exc_info.value.reason_code == "workspace_version_mismatch"


def test_catalog_join_drift_is_rejected_even_when_catalog_versions_match() -> None:
    catalog = CatalogLoader().load()
    missing_join_catalog = dataclasses.replace(
        catalog,
        joins=tuple(join for join in catalog.joins if join.join_id != "orders_items"),
    )
    spec = _spec(metric_ids=("gmv",), result_shape="scalar")

    with pytest.raises(SchemaLinkPlanValidationError) as exc_info:
        derive_schema_link_plan(spec, missing_join_catalog)

    assert exc_info.value.reason_code == "registry_catalog_mismatch"


def test_unknown_or_direct_sql_fields_are_rejected_at_mapping_boundary() -> None:
    spec = _spec(metric_ids=("gmv",), result_shape="scalar")
    payload = derive_schema_link_plan(spec).as_dict()
    payload["sql"] = "SELECT * FROM analytics.fact_orders"
    with pytest.raises(SchemaLinkPlanValidationError) as exc_info:
        SchemaLinkPlan.from_mapping(payload)

    assert exc_info.value.reason_code == "invalid_schema_link_plan"
