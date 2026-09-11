from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_analysis_agent.qwen35_4b_thelook_v2_matching import (
    DECODE,
    MODEL_ID,
    MODEL_REVISION,
    PROMPT_TOKEN_PREFLIGHT,
    build_comparison_contract,
    verify_matching_generation,
)
from data_analysis_agent.olist_candidate_sql_evaluation import (
    CandidateEvaluationRecord,
    build_safe_report,
)
from data_analysis_agent.thelook_v2_matching import MATCHING_MARKER_VERSION, sha256_file
from data_analysis_agent.thelook_v2_matching import TheLookV2MatchingError


def _write_completions(path: Path, case_ids: list[str], label: str) -> None:
    path.write_text(
        "".join(
            json.dumps({"case_id": case_id, "completion": f"SELECT {label}_{i}"})
            + "\n"
            for i, case_id in enumerate(case_ids)
        ),
        encoding="utf-8",
    )


def _records(case_ids: list[str]) -> list[CandidateEvaluationRecord]:
    return [
        CandidateEvaluationRecord(
            source_id=case_id,
            route_state="answerable",
            generation_status="generated",
            generated_tokens=1,
            generation_elapsed_ms=1,
            policy_status="not_run",
            execution_status="not_run",
            result_validation_state=None,
            result_contract_satisfied=False,
            failure_category=None,
        )
        for case_id in case_ids
    ]


def test_qwen35_4b_profile_freezes_the_completed_training_base_and_decode() -> None:
    contract = build_comparison_contract(
        cases_sha256="c" * 64,
        manifest_sha256="m" * 64,
        workspace={"workspace_id": "thelook-cross-schema-eval-v2"},
    )

    assert MODEL_ID == "Qwen/Qwen3.5-4B"
    assert MODEL_REVISION == "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"
    assert contract["model_id"] == MODEL_ID
    assert contract["model_revision"] == MODEL_REVISION
    assert contract["decode"] == DECODE
    assert contract["prompt_token_preflight"] == PROMPT_TOKEN_PREFLIGHT
    assert contract["gold_sql_read_for_generation"] is False
    assert contract["database_rows_read_for_generation"] is False


def test_qwen35_4b_matching_verifier_accepts_completed_base_adapter_pair(
    tmp_path: Path,
) -> None:
    case_ids = [f"thelook-final-v2-{i:03d}" for i in range(1, 601)]
    base_path = tmp_path / "base.jsonl"
    adapter_path = tmp_path / "adapter.jsonl"
    _write_completions(base_path, case_ids, "base")
    _write_completions(adapter_path, case_ids, "adapter")
    contract = build_comparison_contract(
        cases_sha256="c" * 64,
        manifest_sha256="m" * 64,
        workspace={"workspace_id": "thelook-cross-schema-eval-v2"},
    )
    common = {
        "comparison_contract": contract,
        "decode": DECODE,
        "raw_artifacts_outside_repository": True,
    }
    base = build_safe_report(
        report_metadata={
            **common,
            "run_label": "qwen35_4b_instruct",
            "model": {
                "id": MODEL_ID,
                "revision": MODEL_REVISION,
                "weight_dtype": "bf16",
                "download_manifest_sha256": "d" * 64,
                "adapter": {"enabled": False},
            },
            "evaluation": {
                "dataset": "thelook-cross-schema-final-test-v2",
                "case_count": 600,
                "cases_jsonl_sha256": "c" * 64,
                "manifest_sha256": "m" * 64,
                "server_prompt_bundle_sha256": contract[
                    "server_prompt_bundle_sha256"
                ],
                "qwen35_chat_template": True,
                "thinking_enabled": False,
            },
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(base_path),
                "raw_completions_outside_repository": True,
            },
            "boundaries": {
                "gold_sql_read_for_generation": False,
                "database_rows_read_for_generation": False,
                "production_default_unchanged": True,
            },
        },
        records=_records(case_ids),
    )
    adapter = build_safe_report(
        report_metadata={
            **common,
            "run_label": "adapter",
            "model": {
                "id": MODEL_ID,
                "revision": MODEL_REVISION,
                "weight_dtype": "bf16",
                "download_manifest_sha256": "d" * 64,
                "adapter": {
                    "enabled": True,
                    "adapter_model_sha256": "a" * 64,
                },
            },
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(adapter_path),
                "raw_completions_outside_repository": True,
            },
            "boundaries": {
                "gold_sql_read_for_generation": False,
                "database_rows_read_for_generation": False,
                "production_default_unchanged": True,
            },
        },
        records=_records(case_ids),
    )
    marker = verify_matching_generation(
        base_report=base,
        adapter_report=adapter,
        base_completions=base_path,
        adapter_completions=adapter_path,
        expected_case_ids=case_ids,
        expected_cases_sha256="c" * 64,
        expected_manifest_sha256="m" * 64,
        workspace={"workspace_id": "thelook-cross-schema-eval-v2"},
    )

    assert marker["marker_schema_version"] == MATCHING_MARKER_VERSION
    assert marker["matching_generation_verified_before_gold"] is True


def test_qwen35_4b_matching_verifier_rejects_adapter_model_revision_drift(
    tmp_path: Path,
) -> None:
    case_ids = [f"thelook-final-v2-{i:03d}" for i in range(1, 601)]
    base_path = tmp_path / "base.jsonl"
    adapter_path = tmp_path / "adapter.jsonl"
    _write_completions(base_path, case_ids, "base")
    _write_completions(adapter_path, case_ids, "adapter")
    contract = build_comparison_contract(
        cases_sha256="c" * 64,
        manifest_sha256="m" * 64,
        workspace={"workspace_id": "thelook-cross-schema-eval-v2"},
    )
    metadata = {
        "comparison_contract": contract,
        "decode": DECODE,
        "raw_artifacts_outside_repository": True,
        "boundaries": {
            "gold_sql_read_for_generation": False,
            "database_rows_read_for_generation": False,
            "production_default_unchanged": True,
        },
        "evaluation": {
            "dataset": "thelook-cross-schema-final-test-v2",
            "case_count": 600,
            "cases_jsonl_sha256": "c" * 64,
            "manifest_sha256": "m" * 64,
            "server_prompt_bundle_sha256": contract[
                "server_prompt_bundle_sha256"
            ],
            "qwen35_chat_template": True,
            "thinking_enabled": False,
        },
    }
    base = build_safe_report(
        report_metadata={
            **metadata,
            "run_label": "qwen35_4b_instruct",
            "model": {
                "id": MODEL_ID,
                "revision": MODEL_REVISION,
                "weight_dtype": "bf16",
                "download_manifest_sha256": "d" * 64,
                "adapter": {"enabled": False},
            },
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(base_path),
                "raw_completions_outside_repository": True,
            },
        },
        records=_records(case_ids),
    )
    adapter_meta = dict(metadata)
    adapter_meta.update(
        {
            "run_label": "adapter",
            "model": {
                "id": MODEL_ID,
                "revision": "drifted",
                "weight_dtype": "bf16",
                "download_manifest_sha256": "d" * 64,
                "adapter": {"enabled": True},
            },
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(adapter_path),
                "raw_completions_outside_repository": True,
            },
        }
    )
    adapter = build_safe_report(report_metadata=adapter_meta, records=_records(case_ids))

    with pytest.raises(TheLookV2MatchingError, match="generation metadata drifted"):
        verify_matching_generation(
            base_report=base,
            adapter_report=adapter,
            base_completions=base_path,
            adapter_completions=adapter_path,
            expected_case_ids=case_ids,
            expected_cases_sha256="c" * 64,
            expected_manifest_sha256="m" * 64,
            workspace={"workspace_id": "thelook-cross-schema-eval-v2"},
        )
