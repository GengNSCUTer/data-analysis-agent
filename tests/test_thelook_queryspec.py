from __future__ import annotations

import dataclasses

import pytest

from data_analysis_agent.thelook_queryspec import (
    QUERY_SPEC_SCHEMA_VERSION,
    TheLookQuerySpec,
    TheLookQuerySpecValidationError,
    TheLookQueryTime,
    validate_thelook_query_spec,
)


def test_scalar_queryspec_is_canonical_and_immutable() -> None:
    spec = TheLookQuerySpec.create_validated(
        metric_ids=("completed_order_count",), result_shape="scalar"
    )

    assert dataclasses.is_dataclass(spec)
    assert spec.schema_version == QUERY_SPEC_SCHEMA_VERSION
    assert spec.query_spec_id.startswith("tqs_")
    assert spec.query_spec_id == spec.expected_query_spec_id()
    assert validate_thelook_query_spec(spec) == spec
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.metric_ids = ("return_rate",)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("shape", "dimension", "metrics", "columns", "program"),
    [
        (
            "dimension_grouped",
            "category",
            ("completed_sale_amount",),
            ("category", "completed_sale_amount"),
            "TLJP02_group_category",
        ),
        (
            "dimension_grouped",
            "state",
            ("completed_order_count", "return_rate"),
            ("state", "completed_order_count", "return_rate"),
            "TLJP02_group_state",
        ),
        (
            "time_series",
            None,
            ("completed_order_count", "average_fulfillment_days"),
            ("completed_order_count", "average_fulfillment_days", "time"),
            "TLJP03_order_time_series",
        ),
    ],
)
def test_supported_shapes_have_stable_contract(
    shape: str,
    dimension: str | None,
    metrics: tuple[str, ...],
    columns: tuple[str, ...],
    program: str,
) -> None:
    time = (
        TheLookQueryTime("series", "2019-01-01", "2020-01-01", "month")
        if shape == "time_series"
        else TheLookQueryTime("all_time")
    )
    spec = TheLookQuerySpec.create_validated(
        metric_ids=metrics,
        result_shape=shape,
        dimension=dimension,
        time=time,
    )
    assert spec.required_result_columns == columns
    assert spec.join_program_id == program


def _assert_rejected(spec: TheLookQuerySpec, reason: str) -> None:
    with pytest.raises(TheLookQuerySpecValidationError) as exc_info:
        validate_thelook_query_spec(spec)
    assert exc_info.value.reason_code == reason


def test_queryspec_rejects_unsupported_dimension_and_mismatched_shape() -> None:
    unsupported = TheLookQuerySpec.create(
        metric_ids=("completed_order_count",),
        result_shape="dimension_grouped",
        dimension="user_id",
    )
    _assert_rejected(unsupported, "coverage_shape_not_permitted")

    scalar_with_dimension = TheLookQuerySpec.create(
        metric_ids=("completed_order_count",),
        result_shape="scalar",
        dimension="state",
    )
    _assert_rejected(scalar_with_dimension, "coverage_shape_not_permitted")


def test_queryspec_rejects_invalid_time_and_metric_contracts() -> None:
    missing_range = TheLookQuerySpec.create(
        metric_ids=("completed_order_count",),
        result_shape="time_series",
        time=TheLookQueryTime("series", None, None, "month"),
    )
    _assert_rejected(missing_range, "invalid_time_contract")

    duplicate_metrics = TheLookQuerySpec.create(
        metric_ids=("return_rate", "return_rate"), result_shape="scalar"
    )
    _assert_rejected(duplicate_metrics, "invalid_metric_ids")


def test_mapping_round_trip_and_tamper_detection() -> None:
    original = TheLookQuerySpec.create_validated(
        metric_ids=("completed_sale_amount",),
        result_shape="dimension_grouped",
        dimension="brand",
    )
    recovered = TheLookQuerySpec.from_mapping(original.as_dict())
    assert recovered == original

    payload = original.as_dict()
    payload["query_spec_id"] = "tqs_tampered"
    _assert_rejected(TheLookQuerySpec.from_mapping(payload), "invalid_query_spec")
