from __future__ import annotations

from data_analysis_agent.multi_schema_sql_only_sft import (
    MULTI_SCHEMA_SQL_ONLY_PROMPT_VERSION,
    build_result_contract_payload,
    render_catalog_payload,
    render_multi_schema_prompt,
)
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE
from data_analysis_agent.thelook_v2_queryspec import TheLookV2QuerySpec

from scripts.post_training.data.materialize_multi_schema_sql_only_release import (
    _canonical_variant_id,
    select_thelook_primary_variants,
    thelook_question_variants,
)


def _scalar_spec(index: int = 0) -> dict[str, object]:
    return TheLookV2QuerySpec.create(
        metric_ids=["completed_order_count"],
        result_shape="scalar",
        time=None,
    ).as_dict() | {"query_spec_id": f"tqs2_test_{index:03d}"}


def test_multi_schema_prompt_has_all_server_owned_sections_without_target_sql() -> None:
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    spec = _scalar_spec()
    catalog_payload = render_catalog_payload(catalog, spec)
    result_contract = build_result_contract_payload(catalog, spec)
    prompt = render_multi_schema_prompt(
        catalog_payload=catalog_payload,
        query_spec=spec,
        result_contract=result_contract,
        question="数据查询：请汇总全部可用时间的已完成订单数。",
    )

    assert MULTI_SCHEMA_SQL_ONLY_PROMPT_VERSION == "multi-schema-sql-only-v1"
    assert prompt.endswith("### SQL")
    assert "### Server-provided Semantic Catalog" in prompt
    assert "### Server-provided QuerySpec" in prompt
    assert "### Server-provided Result Contract" in prompt
    assert "WITH m01" not in prompt
    assert result_contract["required_result_columns"] == ["completed_order_count"]


def test_thelook_eight_forms_select_one_balanced_primary_per_split() -> None:
    rows = [
        {
            "split": "train",
            "seed_id": f"seed-{index:02d}",
            "family_id": "orders_scalar",
            "query_spec": _scalar_spec(index),
        }
        for index in range(16)
    ]
    selected = select_thelook_primary_variants(rows)
    variants = thelook_question_variants(rows[0]["query_spec"])

    assert len(variants) == 8
    assert len(selected) == 16
    assert {variant: list(selected.values()).count(variant) for variant in variants} == dict.fromkeys(variants, 2)
    assert all(question.startswith("数据查询：") for question in variants.values())


def test_canonical_variant_id_reduces_source_suffix_to_shared_v1_v8_contract() -> None:
    assert _canonical_variant_id("olist-anything-v4") == "v4"
    assert _canonical_variant_id("v8") == "v8"


def test_canonical_variant_id_rejects_missing_or_out_of_range_variant() -> None:
    import pytest

    with pytest.raises(ValueError):
        _canonical_variant_id("olist-anything")
    with pytest.raises(ValueError):
        _canonical_variant_id("olist-anything-v9")
