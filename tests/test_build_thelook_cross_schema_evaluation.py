from __future__ import annotations

from collections import Counter

from scripts.post_training.evaluation.build_thelook_cross_schema_evaluation import (
    EXPECTED_CASES,
    _question,
    build_cases,
)
from data_analysis_agent.thelook_queryspec import validate_thelook_query_spec
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.thelook_context import THELOOK_WORKSPACE
from data_analysis_agent.thelook_renderer import render_thelook_gold_sql


def test_thelook_final_test_has_unique_queryspecs_and_expected_coverage() -> None:
    cases = build_cases()
    specs = [item["query_spec"] for item in cases]

    assert len(cases) == EXPECTED_CASES == 206
    assert len({spec.query_spec_id for spec in specs}) == len(cases)
    assert len({item["family_id"] for item in cases}) == len(cases)
    assert Counter(spec.result_shape for spec in specs) == {
        "scalar": 60,
        "dimension_grouped": 66,
        "time_series": 80,
    }
    assert {metric for spec in specs for metric in spec.metric_ids} == {
        "completed_sale_amount",
        "completed_order_count",
        "average_order_value",
        "average_fulfillment_days",
        "return_rate",
        "completed_customer_count",
    }


def test_each_case_is_renderable_and_questions_do_not_embed_sql() -> None:
    catalog = CatalogLoader(THELOOK_WORKSPACE).load()
    for index, item in enumerate(build_cases(), 1):
        spec = validate_thelook_query_spec(item["query_spec"], catalog)
        question = _question(spec, index)
        artifact = render_thelook_gold_sql(spec, catalog)

        assert question
        assert "SELECT" not in question.upper()
        assert "analytics." not in question
        assert artifact.required_result_columns == spec.required_result_columns


def test_high_cardinality_dimensions_are_not_in_the_first_final_test_release() -> None:
    dimensions = {item["query_spec"].dimension for item in build_cases()}
    assert "city" not in dimensions
    assert "brand" not in dimensions
    assert {"state", "traffic_source", "category", "department"} <= dimensions


def test_state_grouped_cases_stay_below_reader_result_cap() -> None:
    state_cases = [item["query_spec"] for item in build_cases() if item["query_spec"].dimension == "state"]
    assert len(state_cases) == 18
    assert all(spec.time.end_exclusive != "2022-01-01" for spec in state_cases)
