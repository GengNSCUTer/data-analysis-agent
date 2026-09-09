from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime
from data_analysis_agent.semantic_catalog import CatalogLoader
from scripts.post_training.data.build_olist_domain_sft_release_v2 import (
    TARGETS,
    _instance,
    _time_variants,
)


def test_time_windows_change_query_spec_but_preserve_family() -> None:
    spec = QuerySpec.create(
        metric_ids=("gmv",),
        result_shape="scalar",
        time=QueryTime("absolute_range", "2016-10-01", "2017-01-01"),
    )
    variants = _time_variants(spec, CatalogLoader().load())
    assert len(variants) == 6
    assert len({item.query_spec_id for item in variants}) == 6
    assert len({item.join_program_id for item in variants}) == 1


def test_release_instance_counts_five_forms_as_one_instance() -> None:
    spec = QuerySpec.create(
        metric_ids=("gmv",),
        result_shape="scalar",
        time=QueryTime("all_time"),
    )
    row = _instance("train", 1, spec)
    assert TARGETS == {"train": 2400, "validation": 600, "in_domain_test": 600}
    assert row["surface_form_policy"] == "five_forms_one_query_instance"
    assert len(row["question_variants"]) == 5
    assert row["primary_variant_id"] == row["question_variants"][0]["variant_id"]
