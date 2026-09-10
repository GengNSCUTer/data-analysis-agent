from __future__ import annotations

from scripts.post_training.data.materialize_olist_pilot_v1_sft import (
    PRIMARY_VARIANT_SELECTION_POLICY,
    _query_spec_id,
    build_rows,
    select_primary_variant,
)


def test_query_spec_identity_supports_admission_and_runtime_record_shapes() -> None:
    assert _query_spec_id({"query_spec": {"query_spec_id": "qs-admission"}}) == "qs-admission"
    assert _query_spec_id({"query_spec_id": "qs-runtime"}) == "qs-runtime"
    assert _query_spec_id({"query_spec": {}}) is None


class _Tokenizer:
    def __call__(self, value: str, *, add_special_tokens: bool) -> dict[str, list[int]]:
        assert add_special_tokens is False
        return {"input_ids": list(range(len(value.split())))}


def test_build_rows_selects_one_reproducible_form_for_five_surface_forms() -> None:
    admitted = [
        {
            "seed_id": "seed-a",
            "split": "train",
            "family_id": "family-a",
            "sql_program_id": "JP01_item_scalar",
            "query_spec": {"query_spec_id": "qs-a"},
            "gold_sql": "SELECT 1;",
        }
    ]
    runtime = [
        {
            "seed_id": "seed-a",
            "variant_id": f"seed-a-v{index}",
            "split": "train",
            "family_id": "family-a",
            "sql_program_id": "JP01_item_scalar",
            "query_spec_id": "qs-a",
            "prompt": "### SQL",
        }
        for index in range(1, 6)
    ]

    splits, exclusions = build_rows(
        admitted,
        runtime,
        _Tokenizer(),
        max_seq_length=100,
        expected_splits={"train": 1, "validation": 0, "in_domain_test": 0},
    )

    assert exclusions == []
    expected_variant = select_primary_variant("seed-a", runtime)["variant_id"]
    assert splits["train"][0]["primary_variant_id"] == expected_variant
    assert splits["train"][0]["surface_variant_count"] == 5
    assert splits["train"][0]["surface_form_policy"] == "five_forms_one_query_instance"
    assert splits["train"][0]["primary_variant_selection_policy"] == PRIMARY_VARIANT_SELECTION_POLICY


def test_primary_variant_selection_is_stable_and_uses_more_than_one_form() -> None:
    selected = []
    for seed_id in ("seed-a", "seed-b", "seed-c", "seed-d", "seed-e", "seed-f"):
        variants = [
            {"variant_id": f"{seed_id}-v{index}"}
            for index in range(1, 6)
        ]
        assert select_primary_variant(seed_id, variants) == select_primary_variant(seed_id, variants)
        selected.append(select_primary_variant(seed_id, variants)["variant_id"].rsplit("-v", 1)[1])
    assert len(set(selected)) > 1
