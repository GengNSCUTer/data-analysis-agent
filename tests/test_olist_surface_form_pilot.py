from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime
from scripts.post_training.data.build_olist_surface_form_pilot import render_variants


def test_surface_form_renderer_produces_five_distinct_forms() -> None:
    spec = QuerySpec.create(
        metric_ids=("gmv",),
        result_shape="scalar",
        time=QueryTime("all_time"),
    )
    variants = render_variants(spec, "family-test", "seed-test")
    assert len(variants) == 5
    assert len({row["question"] for row in variants}) == 5
    assert {row["seed_id"] for row in variants} == {"seed-test"}
