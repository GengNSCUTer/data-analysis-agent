"""Frozen generation profile for Qwen3.5-4B on protected TheLook v2.

This is intentionally separate from the existing 2B profile: model identity,
token preflight, and pair evidence must not be silently reused across base
models.  The module has no model, database, Gold SQL, question, or completion
handling; it only freezes safe metadata used before later evaluation gates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .thelook_v2_matching import (
    EVALUATION_VERSION,
    EXPECTED_CASES,
    EXPECTED_MAX_INPUT_TOKENS,
    EXPECTED_MAX_NEW_TOKENS,
    EXPECTED_SEED,
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
