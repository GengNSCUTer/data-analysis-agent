from __future__ import annotations

from copy import deepcopy
import json
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
    build_safe_locale_comparison,
    build_safe_report,
    validate_manifest_cases,
)
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from scripts.post_training.evaluation.run_olist_candidate_sql_evaluation import (
    EvaluationInputError,
    load_candidate_question_overrides,
    load_selected_cases,
    prepare_case_context,
)
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


def test_external_candidate_question_overlay_requires_exact_non_empty_case_set(
    tmp_path: Path,
) -> None:
    overlay_path = tmp_path / "english-overlay.json"
    overlay_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "language": "en",
                "cases": [
                    {"source_id": "data_001", "candidate_question": "Calculate GMV."},
                    {"source_id": "data_003", "candidate_question": "Calculate delivery days."},
                ],
            }
        ),
        encoding="utf-8",
    )

    overrides = load_candidate_question_overrides(
        overlay_path,
        source_ids=("data_001", "data_003"),
        overlay_contract={"language": "en"},
    )

    assert overrides["data_001"] == "Calculate GMV."
    with pytest.raises(EvaluationInputError, match="exactly match"):
        load_candidate_question_overrides(
            overlay_path,
            source_ids=("data_001", "data_005"),
            overlay_contract={"language": "en"},
        )


def test_prompt_only_overlay_preserves_chinese_server_grounding() -> None:
    manifest, selected = load_selected_cases(
        MANIFEST,
        candidate_question_overrides={
            source_id: f"English candidate question for {source_id}."
            for source_id in yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["source_ids"]
        },
    )
    assert manifest["manifest_id"] == "post_training_olist_business_adapter_evaluation_v1"
    case = next(item for item in selected if item.source_id == "data_001")
    assert case.grounding_question != case.candidate_question

    retriever = CatalogRetriever(CatalogLoader().load())
    candidate_context, tool_context, route_state = prepare_case_context(
        case,
        retriever=retriever,
        router=QuestionRouter(retriever),
        user=User(id="candidate-overlay-test", group_memberships=["analyst"]),
        run_label="base",
    )

    assert route_state == "answerable"
    assert candidate_context.question == case.candidate_question
    assert tool_context.metadata["question"] == case.grounding_question


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


def test_locale_comparison_requires_same_model_contract_but_allows_overlay_change() -> None:
    chinese = _report("base", [_record("data_001", valid=True)])
    english = _report("base", [_record("data_001", valid=False, failure="policy_rejected")])
    english["comparison_contract"] = {
        **english["comparison_contract"],
        "manifest_id": "english-overlay",
        "manifest_sha256": "different",
        "candidate_question_overlay": {
            "mode": "prompt_only",
            "language": "en",
            "overlay_sha256": "overlay-hash",
        },
    }

    comparison = build_safe_locale_comparison(chinese, english)

    assert comparison["run_label"] == "base"
    assert comparison["source_question_condition"]["language"] == "zh"
    assert comparison["target_question_condition"]["language"] == "en"
    assert comparison["target_minus_source"]["result_contract_valid"] == -1
    assert "candidate_sql" not in repr(comparison)


def test_safe_report_rejects_raw_sql_or_question_fields() -> None:
    report = _report("base", [_record("data_001", valid=True)])
    report["candidate_sql"] = "SELECT 1"

    with pytest.raises(OlistCandidateEvaluationError, match="unsafe report field"):
        build_safe_comparison(report, _report("adapter", [_record("data_001", valid=True)]))
