from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from data_analysis_agent.candidate_sql_generator import (
    CandidateSqlContext,
    CandidateSqlGenerationError,
    OLIST_CANDIDATE_SQL_PROMPT_VERSION,
    render_candidate_sql_prompt,
    require_database_route,
    unwrap_sql_completion,
)
from data_analysis_agent.olist_candidate_sql_evaluation import (
    CandidateEvaluationRecord,
    OlistCandidateEvaluationError,
    build_safe_comparison,
    build_safe_report,
    validate_manifest_cases,
)
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from vanna.core.user import User


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "evals/manifests/post_training_olist_business_adapter_evaluation_v1.yaml"
HOLDOUT = ROOT / "evals/manifests/post_training_holdout_v1.yaml"
SOURCE = ROOT / "evals/cases/text_to_sql_v2.yaml"


def _record(source_id: str, *, valid: bool, failure: str | None = None) -> CandidateEvaluationRecord:
    return CandidateEvaluationRecord(
        source_id=source_id,
        route_state="answerable",
        generation_status="generated",
        generated_tokens=18,
        generation_elapsed_ms=24,
        policy_status="accepted" if valid else "rejected",
        execution_status="executed" if valid else "not_run",
        result_validation_state="valid" if valid else None,
        result_contract_satisfied=valid,
        failure_category=failure,
    )


def _report(run_label: str, records: list[CandidateEvaluationRecord]) -> dict[str, object]:
    return build_safe_report(
        report_metadata={
            "run_label": run_label,
            "comparison_contract": {
                "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
                "decode": {"seed": 42, "do_sample": False},
            },
        },
        records=records,
    )


def test_candidate_prompt_is_sql_only_and_uses_server_context() -> None:
    prompt = render_candidate_sql_prompt(
        CandidateSqlContext(
            question="统计一个指标",
            catalog_prompt="## 本次请求的受限语义 Catalog\n- 仅有服务器提供的表。",
            query_plan_prompt="### 服务器生成的查询计划\n- 仅允许 `gmv`。",
            required_result_columns=("gmv",),
        )
    )

    assert "PostgreSQL" in prompt
    assert "统计一个指标" in prompt
    assert "受限语义 Catalog" in prompt
    assert "服务器生成的查询计划" in prompt
    assert "`gmv`" in prompt
    assert "no Markdown" in prompt
    assert prompt.endswith("### SQL")


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        ("```sql\nSELECT 1 AS gmv\n```", "SELECT 1 AS gmv"),
        ("SQL: SELECT 1 AS gmv", "SELECT 1 AS gmv"),
        ("Here is SQL: SELECT 1 AS gmv", "Here is SQL: SELECT 1 AS gmv"),
        ("DELETE FROM fact_orders", "DELETE FROM fact_orders"),
    ],
)
def test_completion_unwrapper_only_removes_outer_presentation(
    completion: str, expected: str
) -> None:
    assert unwrap_sql_completion(completion) == expected


def test_completion_unwrapper_rejects_empty_wrapper() -> None:
    with pytest.raises(CandidateSqlGenerationError, match="empty SQL wrapper"):
        unwrap_sql_completion("```sql\n```")


def test_non_database_route_cannot_construct_candidate_request() -> None:
    router = QuestionRouter(CatalogRetriever(CatalogLoader().load()))
    route = router.classify(
        "如何提升 GMV",
        user=User(id="candidate-evaluation-test", group_memberships=["analyst"]),
    )

    assert route.should_generate_sql is False
    with pytest.raises(CandidateSqlGenerationError, match="forbidden"):
        require_database_route(route)


def test_olist_manifest_contains_only_protected_answerable_database_cases() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    source = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    holdout = yaml.safe_load(HOLDOUT.read_text(encoding="utf-8"))

    selected = validate_manifest_cases(manifest, source["cases"], holdout)

    assert selected == tuple(manifest["source_ids"])
    assert len(selected) == 12


def test_manifest_validation_rejects_non_database_source_case() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    source = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    holdout = yaml.safe_load(HOLDOUT.read_text(encoding="utf-8"))
    invalid = deepcopy(manifest)
    invalid["source_ids"] = ["general_001"]

    with pytest.raises(OlistCandidateEvaluationError, match="answerable database"):
        validate_manifest_cases(invalid, source["cases"], holdout)


def test_safe_pair_comparison_tracks_only_aggregate_status_transitions() -> None:
    base = _report(
        "base",
        [
            _record("data_001", valid=False, failure="policy_rejected"),
            _record("data_003", valid=True),
        ],
    )
    adapter = _report(
        "adapter",
        [
            _record("data_001", valid=True),
            _record("data_003", valid=False, failure="postgres_execution_error"),
        ],
    )

    comparison = build_safe_comparison(base, adapter)

    assert comparison["non_valid_to_valid"] == 1
    assert comparison["valid_to_non_valid"] == 1
    assert comparison["adapter_minus_base"]["result_contract_valid"] == 0
    assert "candidate_sql" not in repr(comparison)
    assert "result_rows" not in repr(comparison)


def test_safe_report_rejects_raw_sql_or_question_fields() -> None:
    report = _report("base", [_record("data_001", valid=True)])
    report["candidate_sql"] = "SELECT 1"

    with pytest.raises(OlistCandidateEvaluationError, match="unsafe report field"):
        build_safe_comparison(report, _report("adapter", [_record("data_001", valid=True)]))
