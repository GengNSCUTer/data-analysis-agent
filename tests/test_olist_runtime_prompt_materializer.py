from __future__ import annotations

import json

import pytest

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE
from data_analysis_agent.olist_queryspec import QuerySpec, WorkspacePin

from scripts.post_training.data.materialize_olist_runtime_prompts import (
    RuntimePromptInputError,
    load_question_variants,
    load_question_variant_cases,
    load_question_variant_cases_v3,
    load_question_variant_cases_v4,
    workspace_for_admitted_records,
)


def _write_json(tmp_path, value):
    path = tmp_path / "variants.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_question_variants_require_exact_seed_set(tmp_path):
    path = _write_json(
        tmp_path,
        {
            "schema_version": "1",
            "language": "zh",
            "prompt_version": "olist-candidate-sql-v1",
            "cases": [{"seed_id": "seed-a", "question": "查询运费。"}],
        },
    )
    assert load_question_variants(path, {"seed-a"}) == {"seed-a": "查询运费。"}


def test_question_variants_reject_unknown_or_duplicate_seed(tmp_path):
    path = _write_json(
        tmp_path,
        {
            "schema_version": "1",
            "language": "zh",
            "prompt_version": "olist-candidate-sql-v1",
            "cases": [
                {"seed_id": "seed-a", "question": "查询运费。"},
                {"seed_id": "seed-a", "question": "再次查询运费。"},
            ],
        },
    )
    with pytest.raises(RuntimePromptInputError, match="duplicate or unknown"):
        load_question_variants(path, {"seed-a", "seed-b"})


def test_question_variants_reject_extra_fields(tmp_path):
    path = _write_json(
        tmp_path,
        {
            "schema_version": "1",
            "language": "zh",
            "prompt_version": "olist-candidate-sql-v1",
            "cases": [{"seed_id": "seed-a", "question": "查询运费。", "note": "x"}],
        },
    )
    with pytest.raises(RuntimePromptInputError, match="only seed_id and question"):
        load_question_variants(path, {"seed-a"})


def test_v2_question_variants_require_two_cases_per_seed(tmp_path):
    path = _write_json(
        tmp_path,
        {
            "schema_version": "2",
            "language": "zh",
            "prompt_version": "olist-candidate-sql-v1",
            "variant_policy": "reviewed paraphrases",
            "cases": [
                {"variant_id": "a-1", "seed_id": "seed-a", "question": "查询运费。"},
                {
                    "variant_id": "a-2",
                    "seed_id": "seed-a",
                    "question": "统计运费金额。",
                },
                {"variant_id": "b-1", "seed_id": "seed-b", "question": "查询订单数。"},
                {
                    "variant_id": "b-2",
                    "seed_id": "seed-b",
                    "question": "统计订单数量。",
                },
            ],
        },
    )
    cases = load_question_variant_cases(path, {"seed-a", "seed-b"})
    assert [case["variant_id"] for case in cases] == ["a-1", "a-2", "b-1", "b-2"]


def test_v2_question_variants_reject_uneven_seed_counts(tmp_path):
    path = _write_json(
        tmp_path,
        {
            "schema_version": "2",
            "language": "zh",
            "prompt_version": "olist-candidate-sql-v1",
            "variant_policy": "reviewed paraphrases",
            "cases": [
                {"variant_id": "a-1", "seed_id": "seed-a", "question": "查询运费。"},
                {
                    "variant_id": "a-2",
                    "seed_id": "seed-a",
                    "question": "统计运费金额。",
                },
                {"variant_id": "b-1", "seed_id": "seed-b", "question": "查询订单数。"},
                {
                    "variant_id": "b-2",
                    "seed_id": "seed-b",
                    "question": "统计订单数量。",
                },
                {"variant_id": "b-3", "seed_id": "seed-b", "question": "看订单量。"},
            ],
        },
    )
    with pytest.raises(RuntimePromptInputError, match="exactly two cases"):
        load_question_variant_cases(path, {"seed-a", "seed-b"})


def test_v3_question_variants_require_five_cases_per_seed(tmp_path):
    cases = [
        {"variant_id": f"a-{index}", "seed_id": "seed-a", "question": f"问题{index}"}
        for index in range(1, 6)
    ]
    path = _write_json(
        tmp_path,
        {
            "schema_version": "3",
            "language": "zh",
            "prompt_version": "olist-candidate-sql-v1",
            "variant_policy": "five controlled forms",
            "cases": cases,
        },
    )
    assert len(load_question_variant_cases_v3(path, {"seed-a"})) == 5


def test_v4_question_variants_require_eight_typed_pure_chinese_cases_per_seed(tmp_path):
    from data_analysis_agent.olist_surface_contract import (
        OLIST_V3_1_VARIANT_IDS,
        OLIST_V3_1_VARIANT_KIND_BY_ID,
        OLIST_V3_1_VARIANT_POLICY,
    )

    cases = [
        {
            "variant_id": f"seed-a-{form_id}",
            "variant_kind": OLIST_V3_1_VARIANT_KIND_BY_ID[form_id],
            "seed_id": "seed-a",
            "question": f"查询指标第{index}种问法。",
        }
        for index, form_id in enumerate(OLIST_V3_1_VARIANT_IDS, 1)
    ]
    path = _write_json(
        tmp_path,
        {
            "schema_version": "4",
            "language": "zh",
            "prompt_version": "olist-candidate-sql-v1",
            "variant_policy": OLIST_V3_1_VARIANT_POLICY,
            "cases": cases,
        },
    )
    assert len(load_question_variant_cases_v4(path, {"seed-a"})) == 8
    cases[0]["question"] = "查询 GMV。"
    path = _write_json(
        tmp_path,
        {
            "schema_version": "4",
            "language": "zh",
            "prompt_version": "olist-candidate-sql-v1",
            "variant_policy": OLIST_V3_1_VARIANT_POLICY,
            "cases": cases,
        },
    )
    with pytest.raises(RuntimePromptInputError, match="pure-Chinese"):
        load_question_variant_cases_v4(path, {"seed-a"})


def test_runtime_materializer_resolves_the_isolated_v3_catalog_from_admitted_queryspec() -> (
    None
):
    spec = QuerySpec.create(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        metric_ids=("review_count",),
        result_shape="scalar",
    )

    workspace = workspace_for_admitted_records([{"query_spec": spec.as_dict()}])

    assert workspace == OLIST_V3_WORKSPACE
