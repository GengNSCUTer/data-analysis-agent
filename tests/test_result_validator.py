from __future__ import annotations

import pandas as pd

from data_analysis_agent.result_validator import ResultValidator, build_result_summary


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
    assert invalid.state == "refuse"


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

    assert extra.state == "refuse"
    assert invalid_rate.state == "refuse"
    assert negative_amount.state == "refuse"


def test_validator_does_not_turn_empty_or_truncated_result_into_numbers() -> None:
    empty = ResultValidator().validate(
        pd.DataFrame(columns=["gmv"]), metric_columns=("gmv",)
    )
    truncated = ResultValidator(max_rows=2).validate(
        pd.DataFrame({"gmv": [1, 2]}), metric_columns=("gmv",)
    )

    assert empty.state == "needs_clarification"
    assert truncated.state == "needs_clarification"
    assert truncated.truncated is True


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

    assert outside.state == "refuse"
    assert amplified.state == "refuse"


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
