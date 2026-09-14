from __future__ import annotations

from collections import Counter
from pathlib import Path

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE
from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime, WorkspacePin
from data_analysis_agent.semantic_catalog import CatalogLoader
from scripts.post_training.data.build_olist_v3_balanced_release_candidates import (
    BUCKETS,
    DATE_WINDOWS,
    SPLIT_TARGETS,
    SPLITS,
    TARGETS,
    _instance_specs,
    build_supplemental_candidates,
    classify_bucket,
    load_v3_seed_candidates,
)


ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "data" / "fixtures" / "olist_v3_coverage_family_seeds_v1.jsonl"


def test_v3_release_targets_match_the_frozen_4500_row_contract() -> None:
    assert SPLIT_TARGETS == {"train": 3000, "validation": 750, "in_domain_test": 750}
    assert sum(SPLIT_TARGETS.values()) == 4500
    assert set(TARGETS) == set(BUCKETS)
    assert all(sum(TARGETS[bucket][split] for bucket in BUCKETS) == SPLIT_TARGETS[split] for split in SPLITS)
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
    assert all(DATE_WINDOWS[index][1] <= DATE_WINDOWS[index + 1][0] for index in range(7))


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


def test_supplemental_pool_is_v3_valid_and_keeps_high_risk_singletons_explicit() -> None:
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    candidates = build_supplemental_candidates(catalog)

    assert candidates
    assert {candidate["primary_bucket"] for candidate in candidates} == set(BUCKETS)
    assert all(candidate["fixed_split"] is None for candidate in candidates)
    assert all(candidate["query_spec"].workspace == WorkspacePin.current(OLIST_V3_WORKSPACE) for candidate in candidates)
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
