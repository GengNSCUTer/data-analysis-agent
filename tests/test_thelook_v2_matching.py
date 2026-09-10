from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from data_analysis_agent.olist_candidate_sql_evaluation import (
    CandidateEvaluationRecord,
    build_safe_report,
)
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.thelook_queryspec import TheLookQueryTime
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE
from data_analysis_agent.thelook_v2_matching import (
    EXPECTED_BASE_WEIGHT_MODE,
    EXPECTED_CASES,
    EXPECTED_MAX_INPUT_TOKENS,
    EXPECTED_MAX_NEW_TOKENS,
    EXPECTED_MODEL_ID,
    EXPECTED_MODEL_REVISION,
    EXPECTED_PROMPT_BUNDLE_SHA256,
    EXPECTED_PROMPT_TOKEN_MAX,
    EXPECTED_PROMPT_TOKEN_MIN,
    EXPECTED_SEED,
    MATCHING_CONTRACT_VERSION,
    PROMPT_VERSION,
    TheLookV2MatchingError,
    project_generation_case,
    render_generation_prompt,
    sha256_file,
    verify_matching_generation,
)
from data_analysis_agent.thelook_v2_queryspec import TheLookV2QuerySpec
from scripts.post_training.evaluation.evaluate_thelook_v2_matching_outputs import (
    _policy_cap_may_have_truncated_candidate,
    denotation_state,
)
from data_analysis_agent.sql_policy import SqlPolicy


class GoldForbiddenRow(dict[str, object]):
    """Fail the test if generation projection starts accessing Gold SQL."""

    def __getitem__(self, key: str) -> object:
        if key in {"gold_sql", "gold_sql_sha256"}:
            raise AssertionError("generation must not access Gold fields")
        return super().__getitem__(key)

    def get(self, key: str, default: object = None) -> object:
        if key in {"gold_sql", "gold_sql_sha256"}:
            raise AssertionError("generation must not access Gold fields")
        return super().get(key, default)


def _valid_row() -> GoldForbiddenRow:
    spec = TheLookV2QuerySpec.create_validated(
        metric_ids=("completed_order_count",),
        result_shape="time_series",
        time=TheLookQueryTime("series", "2019-01-01", "2020-01-01", "month"),
    )
    return GoldForbiddenRow(
        case_id="thelook-final-v2-001",
        question="按月统计已完成订单数",
        query_spec=spec.as_dict(),
        required_result_columns=list(spec.required_result_columns),
        gold_sql="SELECT hidden_gold_sql",
        gold_sql_sha256="hidden",
    )


def test_generation_projection_and_prompt_never_read_or_embed_gold_sql() -> None:
    case = project_generation_case(
        _valid_row(),
        row_number=1,
        catalog=CatalogLoader(THELOOK_V2_WORKSPACE).load(),
    )

    prompt = render_generation_prompt(case, catalog_prompt="CATALOG ONLY")

    assert case.case_id == "thelook-final-v2-001"
    assert prompt.endswith("### SQL")
    assert "hidden_gold_sql" not in prompt
    assert "CATALOG ONLY" in prompt
    assert '"exact_result_columns": true' in prompt


def _write_completions(path: Path, case_ids: list[str], label: str) -> None:
    path.write_text(
        "".join(
            json.dumps(
                {
                    "case_id": case_id,
                    "completion": f"SELECT {label}_protected_completion_{index}",
                }
            )
            + "\n"
            for index, case_id in enumerate(case_ids)
        ),
        encoding="utf-8",
    )


def _safe_report(
    *,
    label: str,
    completion_path: Path,
    case_ids: list[str],
) -> dict[str, object]:
    contract = {
        "matching_contract_version": MATCHING_CONTRACT_VERSION,
        "dataset": "thelook-cross-schema-final-test-v2",
        "case_count": EXPECTED_CASES,
        "cases_jsonl_sha256": "c" * 64,
        "manifest_sha256": "m" * 64,
        "prompt_version": PROMPT_VERSION,
        "prompt_bundle_sha256": EXPECTED_PROMPT_BUNDLE_SHA256,
        "prompt_token_preflight": {
            "count": EXPECTED_CASES,
            "min": EXPECTED_PROMPT_TOKEN_MIN,
            "max": EXPECTED_PROMPT_TOKEN_MAX,
            "at_input_limit": 0,
        },
        "model_id": EXPECTED_MODEL_ID,
        "model_revision": EXPECTED_MODEL_REVISION,
        "base_weight_mode": EXPECTED_BASE_WEIGHT_MODE,
        "decode": {
            "do_sample": False,
            "num_beams": 1,
            "max_input_tokens": EXPECTED_MAX_INPUT_TOKENS,
            "max_new_tokens": EXPECTED_MAX_NEW_TOKENS,
            "seed": EXPECTED_SEED,
        },
        "gold_sql_read_for_generation": False,
        "database_rows_read_for_generation": False,
    }
    adapter: dict[str, object] = {"enabled": label == "adapter"}
    return build_safe_report(
        report_metadata={
            "run_label": label,
            "comparison_contract": contract,
            "model": {"adapter": adapter},
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(completion_path),
                "raw_completions_outside_repository": True,
            },
            "boundaries": {
                "gold_sql_read_for_generation": False,
                "production_default_unchanged": True,
            },
        },
        records=[
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
        ],
    )


def test_matching_verifier_binds_exact_600_case_pair_without_leaking_completions(
    tmp_path: Path,
) -> None:
    case_ids = [
        f"thelook-final-v2-{index:03d}" for index in range(1, EXPECTED_CASES + 1)
    ]
    base_path = tmp_path / "base.jsonl"
    adapter_path = tmp_path / "adapter.jsonl"
    _write_completions(base_path, case_ids, "base")
    _write_completions(adapter_path, case_ids, "adapter")
    base = _safe_report(label="base", completion_path=base_path, case_ids=case_ids)
    adapter = _safe_report(
        label="adapter", completion_path=adapter_path, case_ids=case_ids
    )

    marker = verify_matching_generation(
        base_report=base,
        adapter_report=adapter,
        base_completions=base_path,
        adapter_completions=adapter_path,
        expected_case_ids=case_ids,
        expected_cases_sha256="c" * 64,
        expected_manifest_sha256="m" * 64,
    )

    serialized = json.dumps(marker, ensure_ascii=False)
    assert marker["matching_generation_verified_before_gold"] is True
    assert marker["case_count"] == EXPECTED_CASES
    assert "base_protected_completion" not in serialized
    assert "adapter_protected_completion" not in serialized

    changed = copy.deepcopy(adapter)
    changed["comparison_contract"]["prompt_version"] = "drift"  # type: ignore[index]
    with pytest.raises(
        TheLookV2MatchingError, match="Base and Adapter comparison contracts differ"
    ):
        verify_matching_generation(
            base_report=base,
            adapter_report=changed,
            base_completions=base_path,
            adapter_completions=adapter_path,
            expected_case_ids=case_ids,
            expected_cases_sha256="c" * 64,
            expected_manifest_sha256="m" * 64,
        )


def test_denotation_state_distinguishes_ordered_bag_and_column_mismatches() -> None:
    gold = pd.DataFrame({"metric": [1.0, 2.0], "time": ["2020-01", "2020-02"]})
    assert denotation_state(gold.copy(), gold) == "ordered_denotation_match"
    reordered = gold.iloc[::-1].reset_index(drop=True)
    assert denotation_state(reordered, gold) == "bag_denotation_match"
    assert denotation_state(pd.DataFrame({"other": [1.0]}), gold) == "column_mismatch"


def test_v2_evaluator_distinguishes_default_and_explicit_policy_limits() -> None:
    """A default guard is not truncation, but a capped requested limit is."""

    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)

    assert not _policy_cap_may_have_truncated_candidate(
        "SELECT COUNT(*) AS completed_order_count FROM order_items",
        policy=policy,
        policy_limit_applied=True,
    )
    assert _policy_cap_may_have_truncated_candidate(
        "SELECT COUNT(*) AS completed_order_count FROM order_items LIMIT 201",
        policy=policy,
        policy_limit_applied=True,
    )
    assert not _policy_cap_may_have_truncated_candidate(
        "SELECT COUNT(*) AS completed_order_count FROM order_items LIMIT 20",
        policy=policy,
        policy_limit_applied=False,
    )
