"""Keep TheLook SFT admission aligned with the 1,000-row runtime contract."""

import pandas as pd

from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.thelook_sft_coverage import load_thelook_sft_contract


def test_sft_contract_freezes_execution_and_preview_as_distinct_limits() -> None:
    contract = load_thelook_sft_contract()

    assert contract["execution_contract"] == {
        "analyst_execution_row_cap": 1_000,
        "display_preview_row_budget": 200,
        "statement_timeout_ms": 5_000,
        "semantics": "201_to_999_rows_are_complete_execution_with_display_truncated_true;_1000_rows_is_potential_execution_truncation",
    }


def test_runtime_sized_validation_accepts_complete_201_to_999_row_result() -> None:
    frame = pd.DataFrame({"product_brand": [f"brand-{index}" for index in range(250)], "received_inventory_unit_count": [1] * 250})

    validation = ResultValidator(max_rows=1_000, display_row_budget=200).validate(
        frame,
        required_columns=("product_brand", "received_inventory_unit_count"),
        metric_columns=("received_inventory_unit_count",),
        exact_columns=True,
        metric_value_constraints={"received_inventory_unit_count": {"minimum": 0, "integer_like": True}},
    )

    assert validation.safe_to_answer
    assert validation.display_truncated is True
    assert validation.truncated is False
