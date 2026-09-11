"""Frozen generation profile for Qwen3.5-4B on protected TheLook v2.

This is intentionally separate from the existing 2B profile: model identity,
token preflight, and pair evidence must not be silently reused across base
models.  The module has no model, database, Gold SQL, question, or completion
handling; it only freezes safe metadata used before later evaluation gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .thelook_v2_matching import (
    EVALUATION_VERSION,
    EXPECTED_CASES,
    EXPECTED_MAX_INPUT_TOKENS,
    EXPECTED_MAX_NEW_TOKENS,
    EXPECTED_SEED,
    MATCHING_MARKER_VERSION,
    TheLookV2MatchingError,
    read_raw_completions,
    sha256_bytes,
    sha256_file,
)


MATCHING_CONTRACT_VERSION = "qwen35-4b-thelook-v2-base-adapter-matching-v1"
MODEL_ID = "Qwen/Qwen3.5-4B"
MODEL_REVISION = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"
WEIGHT_DTYPE = "bf16"
SERVER_PROMPT_BUNDLE_SHA256 = (
    "48bdc6763f8a7a5ead870d1ae9b25ba4dede6c99cf39fea0c5294cd4223ab123"
)
PROMPT_TOKEN_PREFLIGHT = {
    "count": 600,
    "min": 1934,
    "max": 2058,
    "at_input_limit": 0,
}
DECODE = {
    "do_sample": False,
    "num_beams": 1,
    "max_input_tokens": EXPECTED_MAX_INPUT_TOKENS,
    "max_new_tokens": EXPECTED_MAX_NEW_TOKENS,
    "seed": EXPECTED_SEED,
}


def build_comparison_contract(
    *, cases_sha256: str, manifest_sha256: str, workspace: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the immutable no-Gold contract for the 4B Base/Adapter pair."""

    return {
        "matching_contract_version": MATCHING_CONTRACT_VERSION,
        "dataset": EVALUATION_VERSION,
        "case_count": EXPECTED_CASES,
        "cases_jsonl_sha256": cases_sha256,
        "manifest_sha256": manifest_sha256,
        "workspace": dict(workspace),
        "server_prompt_bundle_sha256": SERVER_PROMPT_BUNDLE_SHA256,
        "qwen35_chat_template": True,
        "thinking_enabled": False,
        "prompt_token_preflight": dict(PROMPT_TOKEN_PREFLIGHT),
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "weight_dtype": WEIGHT_DTYPE,
        "decode": dict(DECODE),
        "gold_sql_read_for_generation": False,
        "database_rows_read_for_generation": False,
    }


def _require_records(
    report: Mapping[str, Any], *, label: str, expected_run_label: str
) -> list[Mapping[str, Any]]:
    if report.get("run_label") != expected_run_label:
        raise TheLookV2MatchingError(
            f"{label} report has mismatched run label"
        )
    boundaries = report.get("boundaries")
    records = report.get("records")
    if (
        not isinstance(boundaries, Mapping)
        or boundaries.get("gold_sql_read_for_generation") is not False
        or boundaries.get("database_rows_read_for_generation") is not False
        or boundaries.get("production_default_unchanged") is not True
        or not isinstance(records, list)
    ):
        raise TheLookV2MatchingError(
            f"{label} safe report violates generation isolation"
        )
    return records


def _verify_report_contract(
    report: Mapping[str, Any],
    *,
    label: str,
    expected_run_label: str,
    adapter_enabled: bool,
    expected_case_ids: Sequence[str],
    expected_contract: Mapping[str, Any],
    completions: Path,
) -> None:
    records = _require_records(
        report, label=label, expected_run_label=expected_run_label
    )
    if [record.get("source_id") for record in records] != list(expected_case_ids):
        raise TheLookV2MatchingError(f"{label} safe report case IDs differ")
    if len(records) != EXPECTED_CASES or any(
        record.get("generation_status") != "generated" for record in records
    ):
        raise TheLookV2MatchingError(f"{label} generation is incomplete")

    model = report.get("model")
    decode = report.get("decode")
    evaluation = report.get("evaluation")
    raw = report.get("raw_artifacts")
    if label == "adapter":
        report_contract = report.get("comparison_contract")
        if report_contract != expected_contract:
            raise TheLookV2MatchingError(
                "4B Adapter comparison contract differs from frozen inputs"
            )
        # The adapter-only runner stores the matching decode metadata inside
        # its no-Gold comparison contract, rather than duplicating Base's
        # top-level generation fields.
        decode = expected_contract["decode"]
    if not all(isinstance(value, Mapping) for value in (model, decode, raw)):
        raise TheLookV2MatchingError(f"{label} safe report lacks frozen metadata")
    if label != "adapter":
        if not isinstance(evaluation, Mapping):
            raise TheLookV2MatchingError("4B Base report lacks evaluation metadata")
        expected_evaluation = {
            "dataset": expected_contract["dataset"],
            "case_count": EXPECTED_CASES,
            "cases_jsonl_sha256": expected_contract["cases_jsonl_sha256"],
            "manifest_sha256": expected_contract["manifest_sha256"],
            "server_prompt_bundle_sha256": expected_contract[
                "server_prompt_bundle_sha256"
            ],
            "qwen35_chat_template": True,
            "thinking_enabled": False,
        }
        if dict(evaluation) != expected_evaluation:
            raise TheLookV2MatchingError(
                "4B Base evaluation metadata differs from frozen inputs"
            )
    if (
        model.get("id") != MODEL_ID
        or model.get("revision") != MODEL_REVISION
        or model.get("weight_dtype") != WEIGHT_DTYPE
        or not isinstance(model.get("adapter"), Mapping)
        or model["adapter"].get("enabled") is not adapter_enabled
        or dict(decode) != DECODE
        or raw.get("raw_completions_sha256") != sha256_file(completions)
        or raw.get("raw_completions_outside_repository") is not True
    ):
        raise TheLookV2MatchingError(f"{label} generation metadata drifted")
    read_raw_completions(
        completions,
        expected_case_ids=expected_case_ids,
        label=f"{label} completions",
    )


def verify_matching_generation(
    *,
    base_report: Mapping[str, Any],
    adapter_report: Mapping[str, Any],
    base_completions: Path,
    adapter_completions: Path,
    expected_case_ids: Sequence[str],
    expected_cases_sha256: str,
    expected_manifest_sha256: str,
    workspace: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the 4B Base/Adapter pair before protected Gold access."""

    expected_contract = build_comparison_contract(
        cases_sha256=expected_cases_sha256,
        manifest_sha256=expected_manifest_sha256,
        workspace=workspace,
    )
    if len(expected_case_ids) != EXPECTED_CASES:
        raise TheLookV2MatchingError("4B matching pair must contain 600 cases")
    _verify_report_contract(
        base_report,
        label="base",
        expected_run_label="qwen35_4b_instruct",
        adapter_enabled=False,
        expected_case_ids=expected_case_ids,
        expected_contract=expected_contract,
        completions=base_completions,
    )
    _verify_report_contract(
        adapter_report,
        label="adapter",
        expected_run_label="adapter",
        adapter_enabled=True,
        expected_case_ids=expected_case_ids,
        expected_contract=expected_contract,
        completions=adapter_completions,
    )
    base_model = base_report["model"]
    adapter_model = adapter_report["model"]
    if (
        base_model.get("download_manifest_sha256")
        != adapter_model.get("download_manifest_sha256")
    ):
        raise TheLookV2MatchingError("4B Base and Adapter model manifest differs")
    return {
        "marker_schema_version": MATCHING_MARKER_VERSION,
        "comparison_contract": expected_contract,
        "case_count": EXPECTED_CASES,
        "base_safe_report_sha256": sha256_bytes(base_report),
        "adapter_safe_report_sha256": sha256_bytes(adapter_report),
        "base_raw_completions_sha256": sha256_file(base_completions),
        "adapter_raw_completions_sha256": sha256_file(adapter_completions),
        "matching_generation_verified_before_gold": True,
        "gold_sql_read_for_generation": False,
        "production_default_unchanged": True,
    }
