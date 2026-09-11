from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_analysis_agent.olist_candidate_sql_evaluation import (
    CandidateEvaluationRecord,
    build_safe_report,
)
from data_analysis_agent.qwen35_thelook_v2_matching import (
    MODEL_ID,
    MODEL_REVISION,
    WEIGHT_DTYPE,
    build_comparison_contract,
    verify_matching_generation,
)
from data_analysis_agent.thelook_v2_matching import TheLookV2MatchingError


def _write_completions(path: Path, case_ids: list[str], label: str) -> None:
    path.write_text(
        "".join(
            json.dumps({"case_id": case_id, "completion": f"SELECT {label}_{index}"})
            + "\n"
            for index, case_id in enumerate(case_ids)
        ),
        encoding="utf-8",
    )


def _report(
    *, label: str, completions: Path, case_ids: list[str], contract: dict[str, object]
) -> dict[str, object]:
    from data_analysis_agent.thelook_v2_matching import sha256_file

    return build_safe_report(
        report_metadata={
            "run_label": label,
            "comparison_contract": contract,
            "model": {
                "id": MODEL_ID,
                "revision": MODEL_REVISION,
                "weight_dtype": WEIGHT_DTYPE,
                "adapter": {"enabled": label == "adapter"},
            },
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(completions),
                "raw_completions_outside_repository": True,
            },
            "boundaries": {
                "gold_sql_read_for_generation": False,
                "database_rows_read_for_generation": False,
                "production_default_unchanged": True,
            },
        },
        records=[
            CandidateEvaluationRecord(
                case_id,
                "answerable",
                "generated",
                1,
                1,
                "not_run",
                "not_run",
                None,
                False,
                None,
            )
            for case_id in case_ids
        ],
    )


def test_qwen35_pair_verifier_rejects_contract_drift_without_reading_gold(
    tmp_path: Path,
) -> None:
    case_ids = [f"thelook-final-v2-{index:03d}" for index in range(1, 601)]
    workspace = {"workspace_id": "thelook-cross-schema-eval-v2"}
    contract = build_comparison_contract(
        cases_sha256="c" * 64, manifest_sha256="m" * 64, workspace=workspace
    )
    base_path = tmp_path / "base.jsonl"
    adapter_path = tmp_path / "adapter.jsonl"
    _write_completions(base_path, case_ids, "base")
    _write_completions(adapter_path, case_ids, "adapter")
    base = _report(
        label="base", completions=base_path, case_ids=case_ids, contract=contract
    )
    adapter = _report(
        label="adapter",
        completions=adapter_path,
        case_ids=case_ids,
        contract=contract,
    )

    marker = verify_matching_generation(
        base_report=base,
        adapter_report=adapter,
        base_completions=base_path,
        adapter_completions=adapter_path,
        expected_case_ids=case_ids,
        expected_cases_sha256="c" * 64,
        expected_manifest_sha256="m" * 64,
        workspace=workspace,
    )

    assert marker["matching_generation_verified_before_gold"] is True
    changed = dict(adapter)
    changed["comparison_contract"] = {**contract, "thinking_enabled": True}
    with pytest.raises(TheLookV2MatchingError, match="contracts differ"):
        verify_matching_generation(
            base_report=base,
            adapter_report=changed,
            base_completions=base_path,
            adapter_completions=adapter_path,
            expected_case_ids=case_ids,
            expected_cases_sha256="c" * 64,
            expected_manifest_sha256="m" * 64,
            workspace=workspace,
        )
