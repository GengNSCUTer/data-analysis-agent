from __future__ import annotations

from scripts.post_training.data.materialize_olist_pilot_v1_sft import (
    PRIMARY_VARIANT_SELECTION_POLICY,
    _exact_variant_quotas,
    _query_spec_id,
    _workspace_for_pin,
    build_rows,
    primary_variant_selection_report,
    select_primary_variants,
)
from data_analysis_agent.olist_surface_contract import (
    OLIST_V3_1_VARIANT_IDS,
    OLIST_V3_1_VARIANT_KIND_BY_ID,
)
from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE
from data_analysis_agent.olist_queryspec import WorkspacePin


def test_query_spec_identity_supports_admission_and_runtime_record_shapes() -> None:
    assert (
        _query_spec_id({"query_spec": {"query_spec_id": "qs-admission"}})
        == "qs-admission"
    )
    assert _query_spec_id({"query_spec_id": "qs-runtime"}) == "qs-runtime"
    assert _query_spec_id({"query_spec": {}}) is None


class _Tokenizer:
    def __call__(self, value: str, *, add_special_tokens: bool) -> dict[str, list[int]]:
        assert add_special_tokens is False
        return {"input_ids": list(range(len(value.split())))}


def _variants(seed_id: str, split: str, bucket: str) -> list[dict[str, object]]:
    return [
        {
            "seed_id": seed_id,
            "variant_id": f"{seed_id}-{form_id}",
            "variant_kind": OLIST_V3_1_VARIANT_KIND_BY_ID[form_id],
            "split": split,
            "family_id": f"family-{seed_id}",
            "sql_program_id": "JP01_item_scalar",
            "primary_bucket": bucket,
            "query_spec_id": f"qs-{seed_id}",
            "prompt": "### SQL",
        }
        for form_id in OLIST_V3_1_VARIANT_IDS
    ]


def test_build_rows_selects_one_reproducible_form_for_eight_surface_forms() -> None:
    admitted = [
        {
            "seed_id": "seed-a",
            "split": "train",
            "family_id": "family-a",
            "sql_program_id": "JP01_item_scalar",
            "primary_bucket": "single_scalar",
            "query_spec": {"query_spec_id": "qs-a"},
            "gold_sql": "SELECT 1;",
        }
    ]
    runtime = _variants("seed-a", "train", "single_scalar")
    for row in runtime:
        row["family_id"] = "family-a"
        row["query_spec_id"] = "qs-a"

    splits, exclusions = build_rows(
        admitted,
        runtime,
        _Tokenizer(),
        max_seq_length=100,
        expected_splits={"train": 1, "validation": 0, "in_domain_test": 0},
    )

    assert exclusions == []
    assert splits["train"][0]["primary_variant_id"] == "seed-a-v1"
    assert splits["train"][0]["surface_variant_count"] == 8
    assert splits["train"][0]["surface_form_policy"] == "eight_forms_one_query_instance"
    assert (
        splits["train"][0]["primary_variant_selection_policy"]
        == PRIMARY_VARIANT_SELECTION_POLICY
    )
    report = primary_variant_selection_report({"train": splits["train"]})
    assert report["by_split"]["train"]["actual"] == _exact_variant_quotas(1)


def test_primary_variant_selection_is_stable_and_meets_split_and_bucket_quotas() -> (
    None
):
    admitted = {}
    runtime = {}
    for split, bucket, size in (
        ("train", "single_scalar", 17),
        ("train", "multi_scalar", 25),
        ("validation", "single_scalar", 10),
        ("in_domain_test", "single_scalar", 9),
    ):
        for index in range(size):
            seed_id = f"{split}-{bucket}-{index}"
            admitted[seed_id] = {"split": split, "primary_bucket": bucket}
            runtime[seed_id] = _variants(seed_id, split, bucket)

    first = select_primary_variants(admitted, runtime)
    second = select_primary_variants(admitted, runtime)
    assert first == second
    for split in ("train", "validation", "in_domain_test"):
        selected = [
            row for seed, row in first.items() if admitted[seed]["split"] == split
        ]
        forms = [row["variant_id"].rsplit("-", 1)[1] for row in selected]
        assert {
            form: forms.count(form) for form in OLIST_V3_1_VARIANT_IDS
        } == _exact_variant_quotas(len(selected))
    for split, bucket in {
        (row["split"], row["primary_bucket"]) for row in admitted.values()
    }:
        forms = [
            row["variant_id"].rsplit("-", 1)[1]
            for seed, row in first.items()
            if admitted[seed]["split"] == split
            and admitted[seed]["primary_bucket"] == bucket
        ]
        counts = [forms.count(form) for form in OLIST_V3_1_VARIANT_IDS]
        assert max(counts) - min(counts) <= 1


def test_sft_materializer_resolves_isolated_v3_admission_workspace() -> None:
    assert (
        _workspace_for_pin(WorkspacePin.current(OLIST_V3_WORKSPACE).as_dict())
        == OLIST_V3_WORKSPACE
    )
