from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from scripts.audit_product_release_dev import compare_frames
from scripts.run_product_release_dev import _actual, _compare, load_suite


def test_product_release_dev_suite_has_six_balanced_categories() -> None:
    cases = load_suite(Path("evals/cases/product_release_dev_v1.yaml"))
    assert len(cases) == 60
    assert Counter(case["category"] for case in cases) == {
        "single_turn": 10,
        "clarification": 10,
        "comparison_trend": 10,
        "multi_turn": 10,
        "safety_scope": 10,
        "chart_download_empty": 10,
    }


def test_product_release_dev_cases_do_not_contain_sql_or_result_payloads() -> None:
    cases = load_suite(Path("evals/cases/product_release_dev_v1.yaml"))
    forbidden = ("select ", "insert ", "update ", "delete ", "result_rows", "gold_sql")
    for case in cases:
        assert not any(token in str(case).lower() for token in forbidden), case["case_id"]
        for step in case.get("steps", []):
            assert not any(token in str(step).lower() for token in forbidden), case["case_id"]


def test_product_release_dev_suite_has_no_protected_final_prefixes() -> None:
    cases = load_suite(Path("evals/cases/product_release_dev_v1.yaml"))
    for case in cases:
        assert not case["case_id"].startswith(("olist-v3-1-final-", "thelook-v3-final-"))


def test_comparison_accepts_year_groups_without_a_free_text_baseline() -> None:
    expected = {"metric_ids": ["gmv", "paid_order_count"], "comparison": "year_over_year", "requires_database": True}
    actual = {"requires_database": True, "metric_ids": ["paid_order_count", "gmv"], "time_grain": "year",
              "time_range": {"start": "2017-01-01", "end": "2019-01-01"}, "termination_reason": "completed"}
    assert _compare(expected, actual) == []
    actual["time_range"]["end"] = "2018-01-01"
    assert _compare(expected, actual) == ["comparison_shape"]


def test_followup_expectations_check_real_filter_and_result_reuse() -> None:
    expected = {"requires_database": False, "reuse_result": True, "requires_reference": "customer_state"}
    actual = {"requires_database": False, "sql_calls_used": 0, "filter_dimensions": [], "result_reused": False}
    assert _compare(expected, actual) == ["reference_filter_missing", "result_not_reused"]


def test_safe_projection_keeps_filter_names_and_not_entity_values() -> None:
    actual = _actual({}, {}, {"query_plan": {"filters": [{"dimension": "customer_state", "value": "PRIVATE_ENTITY"}]}})
    assert actual["filter_dimensions"] == ["customer_state"]
    assert "PRIVATE_ENTITY" not in str(actual)


@pytest.mark.parametrize("wrong_values", [[80, 90], [0.8, 0.8]])
def test_independent_numeric_comparison_rejects_wrong_units_or_values(wrong_values) -> None:
    expected = pd.DataFrame({"customer_state": ["SP", "RJ"], "rate": [0.8, 0.9]})
    actual = pd.DataFrame({"customer_state": ["SP", "RJ"], "rate": wrong_values})
    assert compare_frames(expected, actual, ("rate",)) == "denotation_mismatch"


def test_independent_comparison_keeps_null_group_and_ignores_display_order() -> None:
    expected = pd.DataFrame({"category": ["x", None], "orders": [2, 1]})
    actual = pd.DataFrame({"category": [None, "x"], "orders": [1, 2]})
    assert compare_frames(expected, actual, ("orders",)) == "match"
    assert compare_frames(expected, actual.iloc[1:], ("orders",)) == "denotation_mismatch"
