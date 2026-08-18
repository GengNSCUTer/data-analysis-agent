from __future__ import annotations

from pathlib import Path

from scripts.run_online_text_to_sql_evaluation import (
    _load_live_cases,
    _load_manual_labels,
)


ROOT = Path(__file__).resolve().parents[1]


def test_online_suite_selects_a_bounded_unique_representative_sample() -> None:
    manifest, cases = _load_live_cases(
        ROOT / "evals/cases/text_to_sql_online_v1.yaml",
        ROOT / "evals/cases/text_to_sql_v2.yaml",
    )

    assert manifest["model"] == "deepseek-ai/DeepSeek-V4-Flash"
    assert 20 <= len(cases) <= 30
    assert len({case.case_id for case in cases}) == len(cases)
    assert any(case.requires_database for case in cases)
    assert any(not case.requires_database for case in cases)
    assert all(case.review_focus for case in cases)


def test_online_suite_keeps_live_questions_out_of_the_manifest() -> None:
    manifest, _ = _load_live_cases(
        ROOT / "evals/cases/text_to_sql_online_v1.yaml",
        ROOT / "evals/cases/text_to_sql_v2.yaml",
    )

    assert all("question" not in case for case in manifest["cases"])


def test_manual_labels_are_case_scoped_and_validate_values(tmp_path: Path) -> None:
    labels_path = tmp_path / "manual-labels.yaml"
    labels_path.write_text(
        """cases:
  data_001:
    metric_semantics_correct: pass
    answer_grounded: fail
""",
        encoding="utf-8",
    )

    labels = _load_manual_labels(labels_path, {"data_001"})

    assert labels == {
        "data_001": {
            "metric_semantics_correct": "pass",
            "answer_grounded": "fail",
        }
    }
