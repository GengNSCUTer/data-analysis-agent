from __future__ import annotations

from collections import Counter

import pytest

from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.sql_policy import SqlPolicy
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE
from data_analysis_agent.thelook_v2_queryspec import METRIC_FACT_DOMAINS
from data_analysis_agent.thelook_v2_renderer import render_thelook_v2_gold_sql
from scripts.post_training.evaluation.build_thelook_cross_schema_evaluation_v2 import (
    EXPECTED_CASES,
    TheLookV2EvaluationBuildError,
    _external_new_dir,
    build_cases,
    primary_variant_id,
    question_variants,
)


def test_v2_builder_has_unique_600_case_cross_schema_coverage() -> None:
    cases = build_cases()
    specs = [case["query_spec"] for case in cases]

    assert len(cases) == EXPECTED_CASES == 600
    assert len({spec.query_spec_id for spec in specs}) == len(cases)
    assert len({case["family_id"] for case in cases}) == len(cases)
    assert Counter(spec.result_shape for spec in specs) == {
        "scalar": 163,
        "dimension_grouped": 303,
        "time_series": 134,
    }
    assert len({metric for spec in specs for metric in spec.metric_ids}) == 20
    assert {
        METRIC_FACT_DOMAINS[metric] for spec in specs for metric in spec.metric_ids
    } == {
        "order_items",
        "orders",
        "inventory_received",
        "inventory_sold",
        "inventory_snapshot",
        "events",
        "users",
    }
    assert {spec.time.grain for spec in specs if spec.time.grain} == {
        "day",
        "week",
        "month",
        "quarter",
        "year",
    }


def test_v2_builder_keeps_questions_varied_and_metric_combinations_safe() -> None:
    for case in build_cases():
        spec = case["query_spec"]
        variants = question_variants(spec)

        assert len(variants) == 5
        assert len(set(variants.values())) == 5
        assert primary_variant_id(spec.query_spec_id) in variants
        assert all("SELECT" not in question.upper() for question in variants.values())
        assert all("analytics." not in question for question in variants.values())
        assert len({METRIC_FACT_DOMAINS[metric] for metric in spec.metric_ids}) == 1

    multi_series = [
        case["query_spec"]
        for case in build_cases()
        if case["query_spec"].result_shape == "time_series"
        and len(case["query_spec"].metric_ids) > 1
    ]
    assert len(multi_series) == 29
    assert "event_state" not in {case["query_spec"].dimension for case in build_cases()}


def test_v2_builder_uses_admitted_state_windows_for_dispatch_metrics() -> None:
    dispatch_state_cases = [
        case["query_spec"]
        for case in build_cases()
        if case["query_spec"].dimension == "customer_state"
        and "average_dispatch_days" in case["query_spec"].metric_ids
    ]

    assert {
        (spec.time.start, spec.time.end_exclusive) for spec in dispatch_state_cases
    } == {
        ("2019-01-01", "2020-01-01"),
        ("2019-01-01", "2019-07-01"),
        ("2019-07-01", "2020-01-01"),
    }


def test_v2_builder_full_render_and_policy_preflight() -> None:
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)

    for case in build_cases():
        artifact = render_thelook_v2_gold_sql(case["query_spec"], catalog)
        assert policy.evaluate(artifact.sql).status == "allowed"
        assert (
            artifact.required_result_columns
            == case["query_spec"].required_result_columns
        )


def test_v2_builder_refuses_to_write_protected_cases_inside_the_repository(
    tmp_path,
) -> None:
    with pytest.raises(TheLookV2EvaluationBuildError):
        _external_new_dir(THELOOK_V2_WORKSPACE.catalog_path.parent / "generated-cases")

    allowed = _external_new_dir(tmp_path / "external-cases")
    assert allowed == (tmp_path / "external-cases").resolve()
