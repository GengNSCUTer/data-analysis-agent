"""Contracts for the Qwen3.5-2B Olist Adapter's protected TheLook v2 test.

The older :mod:`thelook_v2_matching` contract intentionally remains bound to
the historical Qwen2.5-Coder-1.5B experiment.  This module defines a separate
profile for Qwen3.5-2B so old measurements cannot be silently reinterpreted.

It contains no model, database, Gold SQL, question, or completion handling.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


MATCHING_CONTRACT_VERSION = "qwen35-2b-thelook-v2-base-adapter-matching-v1"
MODEL_ID = "Qwen/Qwen3.5-2B"
MODEL_REVISION = "15852e8c16360a2fea060d615a32b45270f8a8fc"
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


@dataclass(frozen=True)
class Qwen35TheLookV2Pair:
    """External files forming the matching Base/Adapter generation pair."""

    base_report: Mapping[str, Any]
    adapter_report: Mapping[str, Any]
    base_completions: Path
    adapter_completions: Path


def build_comparison_contract(
    *, cases_sha256: str, manifest_sha256: str, workspace: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the exact immutable contract both Qwen3.5 pair reports need."""

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


def _records(report: Mapping[str, Any], label: str) -> list[Mapping[str, Any]]:
    if report.get("run_label") != label:
        raise TheLookV2MatchingError(f"{label} report has mismatched run label")
    boundaries = report.get("boundaries")
    contract = report.get("comparison_contract")
    records = report.get("records")
    if (
        not isinstance(boundaries, Mapping)
        or boundaries.get("gold_sql_read_for_generation") is not False
        or boundaries.get("database_rows_read_for_generation") is not False
        or boundaries.get("production_default_unchanged") is not True
        or not isinstance(contract, Mapping)
        or not isinstance(records, list)
    ):
        raise TheLookV2MatchingError(
            f"{label} safe report violates generation isolation"
        )
    return records


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
    """Verify the Qwen3.5-2B pair before a caller may inspect Gold SQL."""

    base_records = _records(base_report, "base")
    adapter_records = _records(adapter_report, "adapter")
    contract = build_comparison_contract(
        cases_sha256=expected_cases_sha256,
        manifest_sha256=expected_manifest_sha256,
        workspace=workspace,
    )
    if (
        base_report.get("comparison_contract") != contract
        or adapter_report.get("comparison_contract") != contract
    ):
        raise TheLookV2MatchingError("Qwen3.5 Base and Adapter contracts differ")

    for report, label, adapter_enabled in (
        (base_report, "base", False),
        (adapter_report, "adapter", True),
    ):
        model = report.get("model")
        if not isinstance(model, Mapping):
            raise TheLookV2MatchingError(f"{label} report lacks model metadata")
        if (
            model.get("id") != MODEL_ID
            or model.get("revision") != MODEL_REVISION
            or model.get("weight_dtype") != WEIGHT_DTYPE
        ):
            raise TheLookV2MatchingError(f"{label} report model identity drifted")
        adapter = model.get("adapter")
        if (
            not isinstance(adapter, Mapping)
            or adapter.get("enabled") is not adapter_enabled
        ):
            raise TheLookV2MatchingError(f"{label} report adapter state drifted")

    expected = list(expected_case_ids)
    for records, label in ((base_records, "base"), (adapter_records, "adapter")):
        if [record.get("source_id") for record in records] != expected:
            raise TheLookV2MatchingError(
                f"{label} safe report case IDs differ from final test"
            )
        if any(record.get("generation_status") != "generated" for record in records):
            raise TheLookV2MatchingError(f"{label} generation is incomplete")

    for report, path, label in (
        (base_report, base_completions, "base"),
        (adapter_report, adapter_completions, "adapter"),
    ):
        raw = read_raw_completions(
            path, expected_case_ids=expected, label=f"{label} completions"
        )
        artifacts = report.get("raw_artifacts")
        if (
            not isinstance(artifacts, Mapping)
            or artifacts.get("raw_completions_sha256") != sha256_file(path)
            or artifacts.get("raw_completions_outside_repository") is not True
            or len(raw) != EXPECTED_CASES
        ):
            raise TheLookV2MatchingError(f"{label} raw completion evidence drifted")

    return {
        "marker_schema_version": MATCHING_MARKER_VERSION,
        "comparison_contract": contract,
        "case_count": EXPECTED_CASES,
        "base_safe_report_sha256": sha256_bytes(base_report),
        "adapter_safe_report_sha256": sha256_bytes(adapter_report),
        "base_raw_completions_sha256": sha256_file(base_completions),
        "adapter_raw_completions_sha256": sha256_file(adapter_completions),
        "matching_generation_verified_before_gold": True,
        "gold_sql_read_for_generation": False,
        "production_default_unchanged": True,
    }
