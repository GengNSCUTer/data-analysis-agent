from __future__ import annotations

import dataclasses

import pytest

from data_analysis_agent.thelook_queryspec import TheLookQueryTime
from data_analysis_agent.thelook_v2_queryspec import (
    METRIC_FACT_DOMAINS,
    QUERY_SPEC_SCHEMA_VERSION,
    TheLookV2QuerySpec,
    TheLookV2QuerySpecValidationError,
    validate_thelook_v2_query_spec,
)


def _spec(**kwargs) -> TheLookV2QuerySpec:
    return TheLookV2QuerySpec.create(**kwargs)


def _assert_rejected(spec: TheLookV2QuerySpec, reason: str) -> None:
    with pytest.raises(TheLookV2QuerySpecValidationError) as exc_info:
        validate_thelook_v2_query_spec(spec)
    assert exc_info.value.reason_code == reason


@pytest.mark.parametrize(
    ("metrics", "shape", "dimension", "time", "columns"),
    [
        (
            ("event_count", "unique_session_count"),
            "dimension_grouped",
            "event_type",
            TheLookQueryTime("absolute_range", "2020-01-01", "2021-01-01"),
            ("event_type", "event_count", "unique_session_count"),
        ),
        (
            ("sold_inventory_unit_count", "average_days_to_sale"),
            "dimension_grouped",
            "distribution_center",
            TheLookQueryTime("all_time"),
            (
                "distribution_center",
                "sold_inventory_unit_count",
                "average_days_to_sale",
            ),
        ),
        (
            ("average_order_value", "average_items_per_completed_order"),
            "time_series",
            None,
            TheLookQueryTime("series", "2019-01-01", "2024-01-01", "year"),
            (
                "average_order_value",
                "average_items_per_completed_order",
                "time",
            ),
        ),
    ],
)
def test_v2_queryspec_accepts_same_fact_contracts(
    metrics: tuple[str, ...],
    shape: str,
    dimension: str | None,
    time: TheLookQueryTime,
    columns: tuple[str, ...],
) -> None:
    spec = TheLookV2QuerySpec.create_validated(
        metric_ids=metrics,
        result_shape=shape,
        dimension=dimension,
        time=time,
    )

    assert dataclasses.is_dataclass(spec)
    assert spec.schema_version == QUERY_SPEC_SCHEMA_VERSION
    assert spec.query_spec_id.startswith("tqs2_")
    assert spec.query_spec_id == spec.expected_query_spec_id()
    assert spec.required_result_columns == columns


def test_v2_queryspec_rejects_cross_fact_and_snapshot_time_contracts() -> None:
    _assert_rejected(
        _spec(
            metric_ids=("completed_order_count", "event_count"),
            result_shape="scalar",
        ),
        "cross_fact_combination_not_permitted",
    )
    _assert_rejected(
        _spec(
            metric_ids=("current_unsold_inventory_unit_count",),
            result_shape="scalar",
            time=TheLookQueryTime("absolute_range", "2020-01-01", "2021-01-01"),
        ),
        "snapshot_time_not_permitted",
    )
    _assert_rejected(
        _spec(
            metric_ids=("current_unsold_inventory_unit_count",),
            result_shape="time_series",
            time=TheLookQueryTime("series", "2020-01-01", "2021-01-01", "month"),
        ),
        "snapshot_time_not_permitted",
    )


def test_v2_queryspec_rejects_invalid_dimension_and_tampered_contract() -> None:
    _assert_rejected(
        _spec(
            metric_ids=("event_count",),
            result_shape="dimension_grouped",
            dimension="customer_state",
        ),
        "coverage_shape_not_permitted",
    )
    _assert_rejected(
        _spec(
            metric_ids=("event_count",),
            result_shape="dimension_grouped",
            dimension="event_state",
        ),
        "coverage_shape_not_permitted",
    )

    original = TheLookV2QuerySpec.create_validated(
        metric_ids=("completed_order_count",), result_shape="scalar"
    )
    tampered = dataclasses.replace(
        original,
        required_result_columns=("wrong",),
    )
    _assert_rejected(tampered, "result_columns_do_not_match_contract")


def test_v2_queryspec_rejects_catalog_renderer_registry_drift(monkeypatch) -> None:
    spec = _spec(metric_ids=("completed_order_count",), result_shape="scalar")

    monkeypatch.delitem(METRIC_FACT_DOMAINS, "completed_order_count")

    _assert_rejected(spec, "invalid_metric_ids")
