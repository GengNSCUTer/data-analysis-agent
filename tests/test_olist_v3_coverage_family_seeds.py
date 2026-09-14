from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE
from data_analysis_agent.olist_queryspec import QuerySpec, WorkspacePin, validate_query_spec
from data_analysis_agent.semantic_catalog import CatalogLoader
from scripts.post_training.data.build_olist_v3_coverage_family_seeds import (
    BUCKET_SPLIT_TARGETS,
    CATEGORY_DIMENSION_SPLIT_TARGETS,
    MIN_NEW_METRIC_FAMILY_COVERAGE,
    NEW_METRICS,
    SEED_SCHEMA_VERSION,
)
from scripts.post_training.data.materialize_olist_queryspecs import family_id, family_payload


_ROOT = Path(__file__).resolve().parents[1]
_SEEDS_PATH = _ROOT / "data" / "fixtures" / "olist_v3_coverage_family_seeds_v1.jsonl"
_TOP_LEVEL_FIELDS = {
    "seed_schema_version",
    "seed_id",
    "split",
    "primary_bucket",
    "family_id",
    "risk_tags",
    "instance_window_policy",
    "query_spec",
}


def _load() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in _SEEDS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_v3_coverage_seed_manifest_has_the_frozen_family_budget() -> None:
    rows = _load()

    assert len(rows) == 300
    assert len({row["seed_id"] for row in rows}) == 300
    assert len({row["family_id"] for row in rows}) == 300
    assert all(set(row) == _TOP_LEVEL_FIELDS for row in rows)
    assert all(row["seed_schema_version"] == SEED_SCHEMA_VERSION for row in rows)
    assert all(
        not ({"question", "prompt", "sql", "result", "completion"} & set(row))
        for row in rows
    )

    assert Counter(row["split"] for row in rows) == {
        "train": 200,
        "validation": 50,
        "in_domain_test": 50,
    }
    assert Counter(row["primary_bucket"] for row in rows) == {
        bucket: sum(targets.values())
        for bucket, targets in BUCKET_SPLIT_TARGETS.items()
    }
    for bucket, targets in BUCKET_SPLIT_TARGETS.items():
        assert Counter(
            row["split"] for row in rows if row["primary_bucket"] == bucket
        ) == Counter(targets)


def test_v3_coverage_seed_manifest_is_validated_v3_queryspecs() -> None:
    rows = _load()
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    expected_pin = WorkspacePin.current(OLIST_V3_WORKSPACE)

    for row in rows:
        spec = QuerySpec.from_mapping(row["query_spec"])
        assert spec.workspace == expected_pin
        assert (
            set(spec.metric_ids) & NEW_METRICS
            or spec.result_shape == "category_grouped"
        )
        assert validate_query_spec(spec, catalog) == spec
        assert family_id(spec) == row["family_id"]
        assert row["instance_window_policy"] in {
            "one_all_time_instance_within_one_split",
            "up_to_eight_legal_date_instances_within_one_split",
        }


def test_v3_coverage_seed_manifest_has_metric_and_structural_coverage() -> None:
    rows = _load()
    for split, minimum in MIN_NEW_METRIC_FAMILY_COVERAGE.items():
        coverage = Counter(
            metric_id
            for row in rows
            if row["split"] == split
            for metric_id in row["query_spec"]["metric_ids"]
            if metric_id in NEW_METRICS
        )
        assert all(coverage[metric_id] >= minimum for metric_id in NEW_METRICS)

    structural = [row for row in rows if row["primary_bucket"] == "structural_hard"]
    assert structural
    assert all("structural_focus" in row["risk_tags"] for row in structural)
    assert all(
        {"count_distinct_customer", "status_filter_and_distinct_order", "two_stage_order_aggregation", "nonnull_nonnegative_latency"}
        & set(row["risk_tags"])
        for row in structural
    )
    assert Counter(
        row["split"]
        for row in rows
        if row["query_spec"]["result_shape"] == "category_grouped"
    ) == Counter(CATEGORY_DIMENSION_SPLIT_TARGETS)


def test_family_payload_keeps_v2_identity_stable_and_labels_v3_contract() -> None:
    """v3 construction must not rewrite protected historical v2 IDs."""
    v2_spec = QuerySpec.create(metric_ids=("gmv",), result_shape="scalar")
    v3_spec = QuerySpec.create(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        metric_ids=("average_item_price",),
        result_shape="scalar",
    )

    assert family_payload(v2_spec)["aggregation_contract"] == "olist-metrics-v2"
    assert family_id(v2_spec) == "family_400f815c189fe49ae02692f9"
    assert family_payload(v3_spec)["aggregation_contract"] == "olist-metrics-0.3-proposal"
    assert family_id(v3_spec) != family_id(v2_spec)
