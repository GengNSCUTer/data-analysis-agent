from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_online_text_to_sql_evaluation import _load_live_cases, _redacted_evidence


ROOT = Path(__file__).resolve().parents[1]
SOURCE_CASES = ROOT / "evals/cases/text_to_sql_v2.yaml"
TARGETED_CASES = ROOT / "evals/cases/text_to_sql_online_targeted_v1.yaml"


def test_targeted_online_manifest_requires_explicit_small_sample_opt_in() -> None:
    with pytest.raises(ValueError, match="20–30"):
        _load_live_cases(TARGETED_CASES, SOURCE_CASES)


def test_targeted_online_manifest_loads_six_source_contracts() -> None:
    _, cases = _load_live_cases(
        TARGETED_CASES, SOURCE_CASES, allow_small_sample=True
    )
    assert [case.case_id for case in cases] == [
        "data_010",
        "multi_003",
        "data_014",
        "data_016",
        "data_005",
        "multi_001",
    ]


def test_redacted_evidence_includes_finalization_without_content() -> None:
    evidence = _redacted_evidence(
        {
            "catalog_trace": {
                "deterministic_result_finalized": True,
                "result_summary": "must not appear in the report",
            },
            "repair_evidence": {},
        }
    )

    assert evidence["deterministic_result_finalized"] is True
    assert "result_summary" not in evidence
