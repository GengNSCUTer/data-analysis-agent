from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE
from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime, WorkspacePin
from data_analysis_agent.semantic_catalog import CatalogLoader
from scripts.post_training.data.build_olist_v3_balanced_release_candidates import (
    BUCKETS,
    CATEGORY_GROUPED_FAMILY_MINIMA,
    CATEGORY_GROUPED_ROW_MINIMA,
    DATE_WINDOWS,
    NEW_V3_METRIC_FAMILY_MINIMA,
    NEW_V3_METRIC_ROW_MINIMA,
    SPLIT_TARGETS,
    SPLITS,
    TARGETS,
    TIME_GRAIN_ROW_MINIMA,
    V3_1_SEED_SPLIT_OVERRIDES,
    _coverage_state,
    _instance_specs,
    _load_protected_fingerprints,
    balance_coverage_report,
    build_supplemental_candidates,
    classify_bucket,
    load_v2_reconstructed_candidates,
    load_v3_seed_candidates,
    select_release_candidates,
    _remove_source_conflicts,
)


ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "data" / "fixtures" / "olist_v3_coverage_family_seeds_v1.jsonl"
V2_QUERY_SPECS = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/"
    "queryspec-materialized-v2/query_specs.jsonl"
)
PROTECTED_SUMMARY = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/olist-protected-family-summary-v1/"
    "export-20260904/protected_family_summary.json"
)
PROTECTED_EVIDENCE = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/olist-protected-family-summary-v1/"
    "export-20260904/protected_family_summary_evidence.json"
)


def test_v3_release_targets_match_the_frozen_4500_row_contract() -> None:
    assert SPLIT_TARGETS == {"train": 3000, "validation": 750, "in_domain_test": 750}
    assert sum(SPLIT_TARGETS.values()) == 4500
    assert set(TARGETS) == set(BUCKETS)
    assert all(
        sum(TARGETS[bucket][split] for bucket in BUCKETS) == SPLIT_TARGETS[split]
        for split in SPLITS
    )
    # Finite state/category singleton capacity is 188 under the frozen split
    # pins.  Its 12 unavailable train rows are intentionally reallocated to
    # multi_dimension, not replaced by duplicate SQL or surface variants.
    assert TARGETS["single_dimension"] == {
        "train": 122,
        "validation": 33,
        "in_domain_test": 33,
    }
    assert TARGETS["multi_dimension"]["train"] == 478


def test_v3_release_date_windows_are_eight_unique_non_overlapping_ranges() -> None:
    assert len(DATE_WINDOWS) == 8
    assert len(set(DATE_WINDOWS)) == 8
    assert all(start < end for start, end in DATE_WINDOWS)
    assert all(
        DATE_WINDOWS[index][1] <= DATE_WINDOWS[index + 1][0] for index in range(7)
    )


def test_v3_seed_expansion_keeps_each_frozen_family_in_its_assigned_split() -> None:
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    candidates = load_v3_seed_candidates(SEEDS, catalog)

    required = [candidate for candidate in candidates if candidate["required_v3_seed"]]
    assert len(required) == 300
    assert len({candidate["family_id"] for candidate in required}) == 300
    assert Counter(candidate["fixed_split"] for candidate in required) == {
        "train": 200,
        "validation": 50,
        "in_domain_test": 50,
    }
    family_splits: dict[str, str] = {}
    for candidate in candidates:
        family_splits.setdefault(candidate["family_id"], candidate["fixed_split"])
        assert family_splits[candidate["family_id"]] == candidate["fixed_split"]


def test_supplemental_pool_is_v3_valid_and_keeps_high_risk_singletons_explicit() -> (
    None
):
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    candidates = build_supplemental_candidates(catalog)

    assert candidates
    assert {candidate["primary_bucket"] for candidate in candidates} == set(BUCKETS)
    assert all(candidate["fixed_split"] is None for candidate in candidates)
    assert all(
        candidate["query_spec"].workspace == WorkspacePin.current(OLIST_V3_WORKSPACE)
        for candidate in candidates
    )
    assert any(
        candidate["query_spec"].metric_ids == ("approval_latency_days",)
        and candidate["primary_bucket"] == "single_scalar"
        for candidate in candidates
    )

    singleton = QuerySpec.create_validated(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        metric_ids=("average_items_per_order",),
        result_shape="scalar",
    )
    assert classify_bucket(singleton) == "single_scalar"
    time_series = QuerySpec.create_validated(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        metric_ids=("review_count",),
        result_shape="time_series",
        time=QueryTime("series", "2017-01-01", "2018-01-01", "month"),
    )
    assert classify_bucket(time_series) == "single_review_series"
    assert len(_instance_specs(time_series, catalog)) == 8


@pytest.mark.integration
def test_v3_1_selection_enforces_metric_category_and_time_balance_contracts() -> None:
    """Exercise real candidate capacity, not just hard-coded target constants."""
    required_artifacts = (
        V2_QUERY_SPECS,
        PROTECTED_SUMMARY,
        PROTECTED_EVIDENCE,
    )
    if not all(path.is_file() for path in required_artifacts):
        pytest.skip("requires the external frozen Olist v2 source artifacts")
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    v3 = load_v3_seed_candidates(
        SEEDS, catalog, split_overrides=V3_1_SEED_SPLIT_OVERRIDES
    )
    v2, _ = load_v2_reconstructed_candidates(
        V2_QUERY_SPECS,
        catalog,
        _load_protected_fingerprints(PROTECTED_SUMMARY, PROTECTED_EVIDENCE),
    )
    v3, v2, _ = _remove_source_conflicts(v3, v2)
    selected = select_release_candidates(v3, v2, build_supplemental_candidates(catalog))
    report = balance_coverage_report(selected)

    assert report["status"] == "pass"
    assert len(selected) == 4500
    for split in SPLITS:
        assert report["category_grouped"][split] == {
            "rows": CATEGORY_GROUPED_ROW_MINIMA[split],
            "families": CATEGORY_GROUPED_FAMILY_MINIMA[split],
            "row_minimum": CATEGORY_GROUPED_ROW_MINIMA[split],
            "family_minimum": CATEGORY_GROUPED_FAMILY_MINIMA[split],
        }
        for metric, values in report["new_v3_metrics"][split].items():
            assert values["rows"] >= NEW_V3_METRIC_ROW_MINIMA[split][metric]
            assert values["families"] >= NEW_V3_METRIC_FAMILY_MINIMA[split][metric]
        for grain, values in report["time_grains"][split].items():
            if split in TIME_GRAIN_ROW_MINIMA:
                assert values["rows"] >= TIME_GRAIN_ROW_MINIMA[split][grain]


def test_balance_state_accepts_serialized_queryspec_rows_from_release_artifacts() -> (
    None
):
    spec = QuerySpec.create_validated(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        metric_ids=("review_count",),
        result_shape="time_series",
        time=QueryTime("series", "2017-01-01", "2017-04-01", "month"),
    )
    state = _coverage_state(
        [
            {
                "split": "validation",
                "family_id": "family-serialized",
                "query_spec": spec.as_dict(),
            }
        ]
    )
    assert state["metric_rows"]["validation"]["review_count"] == 1
    assert state["time_grain_rows"]["validation"]["month"] == 1
