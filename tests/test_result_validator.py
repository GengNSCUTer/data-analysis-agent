from __future__ import annotations

import pandas as pd

from data_analysis_agent.result_validator import ResultValidator


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
