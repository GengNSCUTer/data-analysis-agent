from __future__ import annotations

from pathlib import Path

from scripts.run_text_to_sql_evaluation import evaluate_suite


ROOT = Path(__file__).resolve().parents[1]


def test_historical_text_to_sql_v2_suite_exposes_the_frozen_v1_policy_delta() -> None:
    report = evaluate_suite(ROOT / "evals/cases/text_to_sql_v2.yaml")

    assert report["total_cases"] == 60
    assert report["unique_ids"] is True
    # The protected v1 suite remains unmodified.  Catalog v2 correctly blocks
    # category-level order counts until an order attribution rule is frozen.
    assert report["passed_cases"] == 58
    assert report["failed_cases"] == 2
    assert {failure["id"] for failure in report["failures"]} == {
        "data_014",
        "multi_006",
    }
    assert all(
        failure["actual"]["reason_code"]
        == "dimension_attribution_requires_clarification"
        for failure in report["failures"]
    )
    assert report["mode"] == "deterministic_offline"


def test_text_to_sql_v2_report_does_not_copy_questions() -> None:
    report = evaluate_suite(ROOT / "evals/cases/text_to_sql_v2.yaml")

    assert all("question" not in result for result in report["results"])
    assert all("question" not in result for result in report["failures"])
