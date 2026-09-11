from __future__ import annotations

from data_analysis_agent.qwen35_4b_thelook_v2_matching import (
    DECODE,
    MODEL_ID,
    MODEL_REVISION,
    PROMPT_TOKEN_PREFLIGHT,
    build_comparison_contract,
)


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
