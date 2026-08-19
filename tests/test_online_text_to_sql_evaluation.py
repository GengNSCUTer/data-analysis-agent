from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "run_online_text_to_sql_evaluation.py"
_RUNNER_SPEC = importlib.util.spec_from_file_location("online_text_to_sql_runner", RUNNER_PATH)
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_RUNNER_SPEC)
sys.modules[_RUNNER_SPEC.name] = _RUNNER
_RUNNER_SPEC.loader.exec_module(_RUNNER)
_load_live_cases = _RUNNER._load_live_cases
_redacted_evidence = _RUNNER._redacted_evidence

SOURCE_CASES = ROOT / "evals/cases/text_to_sql_v2.yaml"
TARGETED_CASES = ROOT / "evals/cases/text_to_sql_online_targeted_v1.yaml"
PROJECTION_CASES = ROOT / "evals/cases/text_to_sql_online_projection_v1.yaml"


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


def test_projection_regression_manifest_keeps_the_source_contract_private() -> None:
    manifest, cases = _load_live_cases(
        PROJECTION_CASES, SOURCE_CASES, allow_small_sample=True
    )

    assert manifest["version"] == "1.0-sensitive-projection-regression"
    assert [case.case_id for case in cases] == ["data_005"]
    assert "sensitive join key" in cases[0].review_focus


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
