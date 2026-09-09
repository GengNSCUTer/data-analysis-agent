from __future__ import annotations

from scripts.post_training.data.materialize_olist_pilot_v1_sft import _query_spec_id, build_rows


def test_query_spec_identity_supports_admission_and_runtime_record_shapes() -> None:
    assert _query_spec_id({"query_spec": {"query_spec_id": "qs-admission"}}) == "qs-admission"
    assert _query_spec_id({"query_spec_id": "qs-runtime"}) == "qs-runtime"
    assert _query_spec_id({"query_spec": {}}) is None


class _Tokenizer:
    def __call__(self, value: str, *, add_special_tokens: bool) -> dict[str, list[int]]:
        assert add_special_tokens is False
        return {"input_ids": list(range(len(value.split())))}


def test_build_rows_selects_v1_once_for_five_surface_forms() -> None:
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
    assert splits["train"][0]["primary_variant_id"] == "seed-a-v1"
    assert splits["train"][0]["surface_variant_count"] == 5
    assert splits["train"][0]["surface_form_policy"] == "five_forms_one_query_instance"
