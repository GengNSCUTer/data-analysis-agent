from __future__ import annotations

import pandas as pd
import pytest

from data_analysis_agent.result_validator import ResultValidator, build_result_summary


@pytest.mark.parametrize("years", [[2017, 2018], [2017.0, 2018.0]])
def test_year_bucket_numbers_are_not_epoch_nanoseconds(years):
    validation = ResultValidator().validate(
        pd.DataFrame({"time": years, "gmv": [10, 20]}),
        required_columns=("time", "gmv"), metric_columns=("gmv",),
        time_column="time", time_bucket_grain="year", requested_start="2017-01-01",
        requested_end="2019-01-01", requested_end_exclusive=True,
    )
    assert validation.state == "valid"
    assert validation.time_start == "2017-01-01T00:00:00"
    assert validation.time_end == "2018-01-01T00:00:00"


def test_regional_gmv_without_time_column_is_a_trusted_table() -> None:
    result = ResultValidator().validate(
        pd.DataFrame({"customer_state": ["SP", "RJ"], "gmv": [10.0, 4.0]}),
        required_columns=("customer_state", "gmv"),
        metric_columns=("gmv",),
        exact_columns=True,
        metric_value_constraints={"gmv": {"minimum": 0}},
    )

    assert result.state == "valid"
    assert result.safe_to_answer is True
    assert result.safe_to_display is True


def test_missing_dimension_still_displays_the_metric() -> None:
    result = ResultValidator().validate(
        pd.DataFrame({"gmv": [10.0]}),
        required_columns=("customer_state", "gmv"),
        metric_columns=("gmv",),
        exact_columns=True,
    )

    assert result.state == "degraded"
    assert result.safe_to_display is True
    assert result.safe_to_answer is False
    assert result.missing_columns == ("customer_state",)


def test_validator_accepts_finite_non_empty_metric_result() -> None:
    result = ResultValidator().validate(
        pd.DataFrame({"month": ["2017-01-01"], "gmv": [123.4]}),
        required_columns=("month", "gmv"),
        metric_columns=("gmv",),
    )

    assert result.safe_to_answer is True
    assert result.state == "valid"


def test_validator_refuses_missing_metric_and_non_finite_values() -> None:
    missing = ResultValidator().validate(
        pd.DataFrame({"month": ["2017-01-01"]}),
        required_columns=("month", "gmv"),
        metric_columns=("gmv",),
    )
    invalid = ResultValidator().validate(
        pd.DataFrame({"gmv": [float("inf")]}), metric_columns=("gmv",)
    )

    assert missing.state == "refuse"
    assert missing.safe_to_display is False
    assert invalid.state == "refuse"
    assert invalid.safe_to_display is False


def test_validator_rejects_extra_columns_and_metric_range_violations() -> None:
    extra = ResultValidator().validate(
        pd.DataFrame({"gmv": [10.0], "order_status": ["delivered"]}),
        required_columns=("gmv",),
        metric_columns=("gmv",),
        exact_columns=True,
    )
    invalid_rate = ResultValidator().validate(
        pd.DataFrame({"cancellation_rate": [1.2]}),
        required_columns=("cancellation_rate",),
        metric_columns=("cancellation_rate",),
        exact_columns=True,
        metric_value_constraints={"cancellation_rate": {"minimum": 0, "maximum": 1}},
    )
    negative_amount = ResultValidator().validate(
        pd.DataFrame({"gmv": [-1.0]}),
        required_columns=("gmv",),
        metric_columns=("gmv",),
        exact_columns=True,
        metric_value_constraints={"gmv": {"minimum": 0}},
    )

    assert extra.state == "degraded"
    assert extra.safe_to_display is True
    assert extra.safe_to_answer is False
    assert extra.dropped_columns == ("order_status",)
    assert invalid_rate.state == "refuse"
    assert invalid_rate.safe_to_display is False
    assert negative_amount.state == "refuse"
    assert negative_amount.safe_to_display is False


def test_validator_does_not_turn_empty_or_truncated_result_into_numbers() -> None:
    empty = ResultValidator().validate(
        pd.DataFrame(columns=["gmv"]), metric_columns=("gmv",)
    )
    truncated = ResultValidator(max_rows=2).validate(
        pd.DataFrame({"gmv": [1, 2]}), metric_columns=("gmv",)
    )

    assert empty.state == "degraded"
    assert empty.safe_to_display is True
    assert empty.safe_to_answer is False
    assert truncated.state == "degraded"
    assert truncated.safe_to_display is True
    assert truncated.truncated is True


def test_validator_treats_null_scalar_aggregate_as_empty_data() -> None:
    validation = ResultValidator().validate(
        pd.DataFrame({"gmv": [None]}),
        required_columns=("gmv",),
        metric_columns=("gmv",),
    )

    assert validation.state == "degraded"
    assert validation.safe_to_display is True
    assert validation.safe_to_answer is False
    assert validation.null_metric_columns == ("gmv",)


def test_validator_distinguishes_complete_execution_from_display_preview_budget() -> None:
    frame = pd.DataFrame({"gmv": list(range(250))})

    validation = ResultValidator(max_rows=1_000).validate(
        frame,
        required_columns=("gmv",),
        metric_columns=("gmv",),
    )

    assert validation.state == "valid"
    assert validation.truncated is False
    assert validation.display_truncated is True
    assert validation.as_dict()["execution_truncated"] is False
    assert validation.as_dict()["display_truncated"] is True


def test_exact_cap_is_complete_only_when_the_sentinel_saw_no_extra_row() -> None:
    frame = pd.DataFrame({"gmv": list(range(1_000))})

    complete = ResultValidator(max_rows=1_000).validate(
        frame, metric_columns=("gmv",), execution_truncated=False
    )
    truncated = ResultValidator(max_rows=1_000).validate(
        frame, metric_columns=("gmv",), execution_truncated=True
    )

    assert complete.state == "valid"
    assert complete.truncated is False
    assert truncated.state == "degraded"
    assert truncated.truncated is True
    assert "未展示" in truncated.reason


def test_validator_still_rejects_results_reaching_execution_cap() -> None:
    validation = ResultValidator(max_rows=1_000).validate(
        pd.DataFrame({"gmv": list(range(1_000))}), metric_columns=("gmv",)
    )

    assert validation.state == "degraded"
    assert validation.safe_to_display is True
    assert validation.safe_to_answer is False
    assert validation.truncated is True
    assert validation.display_truncated is True
    assert "可能不完整" in validation.reason
    assert "多读" not in validation.reason


def test_validator_checks_time_coverage_and_join_amplification() -> None:
    outside = ResultValidator().validate(
        pd.DataFrame({"day": ["2018-01-01"], "gmv": [1]}),
        metric_columns=("gmv",),
        time_column="day",
        requested_start="2017-01-01",
        requested_end="2017-12-31",
    )
    amplified = ResultValidator().validate(
        pd.DataFrame({"gmv": [1]}),
        metric_columns=("gmv",),
        join_multiplicity={"order_id": 2},
    )

    assert outside.state == "degraded"
    assert outside.safe_to_display is True
    assert outside.safe_to_answer is False
    assert outside.time_start is not None
    assert amplified.state == "refuse"
    assert amplified.safe_to_display is False


def test_validator_accepts_server_owned_bucket_label_before_partial_range_start() -> None:
    result = ResultValidator().validate(
        pd.DataFrame({"time": ["2017-01-01"], "gmv": [123.4]}),
        metric_columns=("gmv",),
        time_column="time",
        time_bucket_grain="year",
        requested_start="2017-04-01",
        requested_end="2017-07-01",
    )

    assert result.state == "valid"


def test_validator_rejects_an_unknown_time_bucket_grain() -> None:
    with pytest.raises(ValueError, match="unsupported server-owned"):
        ResultValidator().validate(
            pd.DataFrame({"time": ["2017-01-01"], "gmv": [123.4]}),
            metric_columns=("gmv",),
            time_column="time",
            time_bucket_grain="fiscal_period",
            requested_start="2017-04-01",
            requested_end="2017-07-01",
        )


def test_validator_accepts_catalog_time_alias_for_temporal_result() -> None:
    result = ResultValidator().validate(
        pd.DataFrame({"month": ["2017-01-01"], "gmv": [123.4]}),
        required_columns=("gmv", "time"),
        required_column_aliases={"time": ("month",)},
        metric_columns=("gmv",),
        time_column="order_purchase_timestamp",
        time_column_aliases=("time", "month"),
        requested_start="2017-01-01",
        requested_end="2017-12-31",
    )

    assert result.state == "valid"


def test_validator_normalizes_timezone_aware_postgres_buckets() -> None:
    result = ResultValidator().validate(
        pd.DataFrame({"time": [pd.Timestamp("2019-01-01", tz="Asia/Shanghai")], "gmv": [123.4]}),
        metric_columns=("gmv",),
        time_column="time",
        requested_start="2019-01-01",
        requested_end="2020-01-01",
    )

    assert result.state == "valid"


def test_result_summary_is_bounded_and_uses_only_contract_columns() -> None:
    frame = pd.DataFrame(
        {
            "customer_state": ["SP", "RJ"],
            "gmv": [123.456789, 98.1],
            "paid_order_count": [4, 3],
            "uncontracted_column": ["do not persist", "still hidden"],
        }
    )
    validation = ResultValidator().validate(
        frame,
        required_columns=("customer_state", "gmv", "paid_order_count"),
        metric_columns=("gmv", "paid_order_count"),
    )

    summary = build_result_summary(
        frame,
        validation,
        metric_ids=("gmv", "paid_order_count"),
        required_columns=("customer_state", "gmv", "paid_order_count"),
        max_chars=500,
    )

    assert len(summary) <= 500
    assert "uncontracted_column" not in summary
    assert "do not persist" not in summary
    assert "gmv" in summary


def test_result_summary_uses_catalog_labels_for_casing_only_sql_aliases() -> None:
    frame = pd.DataFrame(
        {
            "productcategoryname": ["health_beauty"],
            "averagedeliverydays": [2.5],
            "positivereviewrate": [0.9187],
        }
    )
    validation = ResultValidator().validate(frame, metric_columns=("averagedeliverydays",))

    summary = build_result_summary(
        frame,
        validation,
        required_columns=tuple(frame.columns),
        column_labels={
            "product_category_name": "商品品类",
            "average_delivery_days": "平均履约天数",
            "positive_review_rate": "好评率",
        },
    )

    assert '"productcategoryname":"商品品类"' in summary
    assert '"averagedeliverydays":"平均履约天数"' in summary
    assert '"positivereviewrate":"好评率"' in summary
