from __future__ import annotations

from pathlib import Path

from scripts.run_text_to_sql_evaluation import evaluate_suite


ROOT = Path(__file__).resolve().parents[1]


def test_historical_text_to_sql_v2_suite_exposes_the_frozen_v1_policy_delta() -> None:
    report = evaluate_suite(ROOT / "evals/cases/text_to_sql_v2.yaml")

    assert report["total_cases"] == 60
    assert report["unique_ids"] is True
    # The protected v1 suite remains unmodified. Catalog v2 correctly blocks
    # category-level order counts until an order attribution rule is frozen,
    # and preflight blocks a state-by-month shape above the analyst row budget.
    assert report["passed_cases"] == 57
    assert report["failed_cases"] == 3
    failure_reasons = {
        failure["id"]: failure["actual"]["reason_code"]
        for failure in report["failures"]
    }
    assert failure_reasons == {
        "data_014": "dimension_attribution_requires_clarification",
        "multi_005": "result_row_budget_exceeded",
        "multi_006": "dimension_attribution_requires_clarification",
    }
    assert report["mode"] == "deterministic_offline"


def test_text_to_sql_v2_report_does_not_copy_questions() -> None:
    report = evaluate_suite(ROOT / "evals/cases/text_to_sql_v2.yaml")

    assert all("question" not in result for result in report["results"])
    assert all("question" not in result for result in report["failures"])
