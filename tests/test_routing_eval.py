from __future__ import annotations

from pathlib import Path

from scripts.run_text_to_sql_evaluation import evaluate_suite


ROOT = Path(__file__).resolve().parents[1]


def test_text_to_sql_v2_golden_suite_is_complete_and_deterministic() -> None:
    report = evaluate_suite(ROOT / "evals/cases/text_to_sql_v2.yaml")

    assert report["total_cases"] == 60
    assert report["unique_ids"] is True
    assert report["passed_cases"] == 60, report["failures"]
    assert report["failed_cases"] == 0
    assert report["mode"] == "deterministic_offline"


def test_text_to_sql_v2_report_does_not_copy_questions() -> None:
    report = evaluate_suite(ROOT / "evals/cases/text_to_sql_v2.yaml")

    assert all("question" not in result for result in report["results"])
    assert all("question" not in result for result in report["failures"])
