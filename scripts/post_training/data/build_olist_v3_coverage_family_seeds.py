#!/usr/bin/env python3
"""Freeze the reviewed Olist v3 structural family-seed manifest.

This is deliberately *before* Gold materialization and SFT construction.  One
row describes one semantic SQL family with a representative QuerySpec; it has
no natural-language question, Prompt, SQL string, query result, or model
output.  A later materializer may create legal date instances from a seed, but
must not change its family, split, bucket, workspace or risk tags.

The quotas reflect feasible family space, rather than pretending the small
fixed Olist metric vocabulary can provide equal numbers of every shape.  In
particular, there are only 18 new-metric single-scalar families and 20 new
single-dimension families under the frozen v3 contract.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import itertools
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE  # noqa: E402
from data_analysis_agent.olist_queryspec import (  # noqa: E402
    METRIC_SQL_REGISTRY,
    QuerySpec,
    QueryTime,
    WorkspacePin,
    validate_query_spec,
)
from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from scripts.post_training.data.materialize_olist_queryspecs import family_id  # noqa: E402


SEED_SCHEMA_VERSION = "olist-v3-coverage-family-seed-v1"
WINDOW_POLICY = "up_to_eight_legal_date_instances_within_one_split"
ALL_TIME_POLICY = "one_all_time_instance_within_one_split"

SPLITS = ("train", "validation", "in_domain_test")
BUCKETS = (
    "single_scalar",
    "multi_scalar",
    "single_dimension",
    "multi_dimension",
    "single_purchase_series",
    "single_review_series",
    "multi_time_series",
    "structural_hard",
)

# Exactly 300 v3-new semantic families.  The category shares are based on
# legal program capacity and error-risk coverage, not an artificial equality
# of raw rows.  The split is 200 / 50 / 50 at the *family* level.
BUCKET_SPLIT_TARGETS: Mapping[str, Mapping[str, int]] = {
    "single_scalar": {"train": 12, "validation": 3, "in_domain_test": 3},
    "multi_scalar": {"train": 38, "validation": 9, "in_domain_test": 7},
    "single_dimension": {"train": 12, "validation": 3, "in_domain_test": 5},
    "multi_dimension": {"train": 28, "validation": 7, "in_domain_test": 7},
    "single_purchase_series": {"train": 27, "validation": 6, "in_domain_test": 7},
    "single_review_series": {"train": 3, "validation": 1, "in_domain_test": 1},
    "multi_time_series": {"train": 50, "validation": 13, "in_domain_test": 12},
    "structural_hard": {"train": 30, "validation": 8, "in_domain_test": 8},
}
MIN_NEW_METRIC_FAMILY_COVERAGE = {
    "train": 10,
    "validation": 3,
    "in_domain_test": 3,
}
# Category grouping has a very small legal family space: item-grain metrics
# only, with all-time or absolute-range mode. Preserve five independent
# category families in test rather than repeating the old v2 gap where
# category_grouped had no test representation.
CATEGORY_DIMENSION_SPLIT_TARGETS = {
    "train": 2,
    "validation": 1,
    "in_domain_test": 5,
}

NEW_METRICS = frozenset(
    {
        "unique_customer_count",
        "review_count",
        "canceled_order_count",
        "delivered_order_count",
        "unavailable_order_count",
        "average_items_per_order",
        "average_item_price",
        "approval_latency_days",
        "carrier_handoff_days",
    }
)
PURCHASE_NEW_METRICS = tuple(
    sorted(metric for metric in NEW_METRICS if METRIC_SQL_REGISTRY[metric].time_family == "purchase")
)
REVIEW_NEW_METRICS = tuple(
    sorted(metric for metric in NEW_METRICS if METRIC_SQL_REGISTRY[metric].time_family == "review")
)
PURCHASE_METRICS = tuple(
    sorted(metric for metric, definition in METRIC_SQL_REGISTRY.items() if definition.time_family == "purchase")
)
REVIEW_METRICS = tuple(
    sorted(metric for metric, definition in METRIC_SQL_REGISTRY.items() if definition.time_family == "review")
)
ALL_METRICS = tuple(sorted(METRIC_SQL_REGISTRY))
HIGH_RISK_METRICS = frozenset(
    {
        "unique_customer_count",
        "canceled_order_count",
        "delivered_order_count",
        "unavailable_order_count",
        "average_items_per_order",
        "approval_latency_days",
        "carrier_handoff_days",
    }
)

ABSOLUTE_RANGE = QueryTime("absolute_range", "2017-01-01", "2018-01-01")
ALL_TIME = QueryTime("all_time")
SERIES_GRAINS = ("day", "week", "month", "quarter", "year")
_CATALOG = None


def _catalog():
    """Load the v3 Catalog once while enumerating the finite candidate space."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    return _CATALOG


def _stable_key(value: Mapping[str, Any]) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _risk_tags(spec: QuerySpec, *, structural_focus: bool = False) -> tuple[str, ...]:
    tags: set[str] = set()
    metrics = set(spec.metric_ids)
    if len(metrics) > 1:
        tags.add("multi_metric_cte_merge")
    if spec.result_shape == "time_series":
        tags.add("time_bucket")
    if spec.result_shape in {"state_grouped", "category_grouped"}:
        tags.add("direct_dimension_grouping")
    if "unique_customer_count" in metrics:
        tags.add("count_distinct_customer")
    if metrics & {"canceled_order_count", "delivered_order_count", "unavailable_order_count"}:
        tags.add("status_filter_and_distinct_order")
    if "average_items_per_order" in metrics:
        tags.add("two_stage_order_aggregation")
    if metrics & {"approval_latency_days", "carrier_handoff_days"}:
        tags.add("nonnull_nonnegative_latency")
    if "review_count" in metrics:
        tags.add("review_fact_grain")
    if structural_focus:
        tags.add("structural_focus")
    return tuple(sorted(tags))


def _instance_policy(spec: QuerySpec) -> str:
    return ALL_TIME_POLICY if spec.time.mode == "all_time" else WINDOW_POLICY


def _make_spec(
    metric_ids: tuple[str, ...],
    result_shape: str,
    time: QueryTime,
    *,
    dimension: str | None = None,
) -> QuerySpec | None:
    try:
        return validate_query_spec(QuerySpec.create(
            workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
            metric_ids=metric_ids,
            result_shape=result_shape,
            dimension=dimension,
            time=time,
        ), _catalog())
    except ValueError:
        # Candidate enumeration intentionally proposes only known finite
        # combinations.  Validation remains the sole authority for whether a
        # combination is legal under the frozen Catalog/QuerySpec contract.
        return None


def _candidate(spec: QuerySpec, bucket: str) -> dict[str, Any]:
    return {
        "primary_bucket": bucket,
        "family_id": family_id(spec),
        "risk_tags": _risk_tags(spec, structural_focus=bucket == "structural_hard"),
        "instance_window_policy": _instance_policy(spec),
        "query_spec": spec,
    }


def _unique(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_family: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        by_family.setdefault(str(candidate["family_id"]), candidate)
    return sorted(
        by_family.values(),
        key=lambda candidate: _stable_key(
            {
                "bucket": candidate["primary_bucket"],
                "family_id": candidate["family_id"],
                "query_spec": candidate["query_spec"].as_dict(),
            }
        ),
    )


def _single_scalar_candidates() -> list[dict[str, Any]]:
    return _unique(
        _candidate(spec, "single_scalar")
        for metric_id in sorted(NEW_METRICS)
        for time in (ALL_TIME, ABSOLUTE_RANGE)
        if (spec := _make_spec((metric_id,), "scalar", time)) is not None
    )


def _single_dimension_candidates() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for metric_id in sorted(NEW_METRICS):
        for time in (ALL_TIME, ABSOLUTE_RANGE):
            spec = _make_spec(
                (metric_id,), "state_grouped", time, dimension="customer_state"
            )
            if spec is not None:
                candidates.append(_candidate(spec, "single_dimension"))
    # Product-category grouping is legal for all item-grain metrics in the
    # v3 workspace. Three formulas are inherited from v2 and
    # average_item_price is new. The inherited programs are needed here so
    # the rebalanced v3 test can cover category grouping itself.
    for time in (ALL_TIME, ABSOLUTE_RANGE):
        for metric_id in ("gmv", "item_count", "freight_amount", "average_item_price"):
            spec = _make_spec(
                (metric_id,),
                "category_grouped",
                time,
                dimension="product_category_name",
            )
            if spec is not None:
                candidates.append(_candidate(spec, "single_dimension"))
    return _unique(candidates)


def _single_series_candidates() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    purchase: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for metric_id in PURCHASE_NEW_METRICS:
        for grain in SERIES_GRAINS:
            spec = _make_spec(
                (metric_id,), "time_series", QueryTime("series", "2017-01-01", "2018-01-01", grain)
            )
            if spec is not None:
                purchase.append(_candidate(spec, "single_purchase_series"))
    for metric_id in REVIEW_NEW_METRICS:
        for grain in SERIES_GRAINS:
            spec = _make_spec(
                (metric_id,), "time_series", QueryTime("series", "2017-01-01", "2018-01-01", grain)
            )
            if spec is not None:
                review.append(_candidate(spec, "single_review_series"))
    return _unique(purchase), _unique(review)


def _multi_candidates(
    *,
    bucket: str,
    metric_pool: tuple[str, ...],
    result_shape: str,
    dimension: str | None = None,
    time_options: tuple[QueryTime, ...] = (),
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for size in range(2, 5):
        for metric_ids in itertools.combinations(metric_pool, size):
            if not set(metric_ids) & NEW_METRICS:
                continue
            for time in time_options:
                spec = _make_spec(metric_ids, result_shape, time, dimension=dimension)
                if spec is not None:
                    candidates.append(_candidate(spec, bucket))
    return _unique(candidates)


def _candidate_pools() -> dict[str, list[dict[str, Any]]]:
    purchase_series_times = tuple(
        QueryTime("series", "2017-01-01", "2018-01-01", grain)
        for grain in SERIES_GRAINS
    )
    scalar = _multi_candidates(
        bucket="multi_scalar",
        metric_pool=ALL_METRICS,
        result_shape="scalar",
        time_options=(ALL_TIME, ABSOLUTE_RANGE),
    )
    dimension = _multi_candidates(
        bucket="multi_dimension",
        metric_pool=ALL_METRICS,
        result_shape="state_grouped",
        dimension="customer_state",
        time_options=(ALL_TIME, ABSOLUTE_RANGE),
    )
    purchase_series = _multi_candidates(
        bucket="multi_time_series",
        metric_pool=PURCHASE_METRICS,
        result_shape="time_series",
        time_options=purchase_series_times,
    )
    review_series = _multi_candidates(
        bucket="multi_time_series",
        metric_pool=REVIEW_METRICS,
        result_shape="time_series",
        time_options=purchase_series_times,
    )
    single_purchase, single_review = _single_series_candidates()

    # Structural examples stay multi-metric so the scarce singleton family
    # space remains available as an explicit, separately auditable bucket.
    structural_source = [
        *scalar,
        *dimension,
        *purchase_series,
        *review_series,
    ]
    structural = []
    for candidate in structural_source:
        spec = candidate["query_spec"]
        if set(spec.metric_ids) & HIGH_RISK_METRICS:
            structural.append(
                {
                    **candidate,
                    "primary_bucket": "structural_hard",
                    "risk_tags": _risk_tags(spec, structural_focus=True),
                }
            )
    return {
        "single_scalar": _single_scalar_candidates(),
        "multi_scalar": scalar,
        "single_dimension": _single_dimension_candidates(),
        "multi_dimension": dimension,
        "single_purchase_series": single_purchase,
        "single_review_series": single_review,
        "multi_time_series": [*purchase_series, *review_series],
        "structural_hard": _unique(structural),
    }


def _allocate_bucket(
    candidates: list[dict[str, Any]],
    bucket: str,
    *,
    reserved: set[str],
    coverage: dict[str, Counter[str]],
) -> list[dict[str, Any]]:
    """Choose directly from a full bucket pool with split-aware coverage.

    Selection and split assignment cannot be two unrelated stages: doing so
    lets a hash-sorted global subset accidentally put every representative of
    one new metric into train.  This bounded greedy allocator gives each
    formal split first claim on its under-covered metrics while preserving
    exact bucket quotas and family isolation.
    """
    pending = [
        candidate for candidate in candidates if str(candidate["family_id"]) not in reserved
    ]
    assigned: list[dict[str, Any]] = []
    # Holdouts first: their smaller quota makes them the only place an
    # otherwise-valid metric could become untestable.
    for split in ("validation", "in_domain_test", "train"):
        forced_category_count = (
            CATEGORY_DIMENSION_SPLIT_TARGETS[split]
            if bucket == "single_dimension"
            else 0
        )
        for force_category in [True] * forced_category_count + [False] * (
            BUCKET_SPLIT_TARGETS[bucket][split] - forced_category_count
        ):
            if not pending:
                raise ValueError(f"{bucket} has insufficient eligible families for {split}")

            choices = (
                [
                    candidate
                    for candidate in pending
                    if candidate["query_spec"].result_shape == "category_grouped"
                ]
                if force_category
                else pending
            )
            if not choices:
                raise ValueError(
                    f"{bucket} cannot meet its category-family target for {split}"
                )

            def priority(
                candidate: dict[str, Any], *, current_split: str = split
            ) -> tuple[int, int, int, str]:
                metrics = set(candidate["query_spec"].metric_ids) & NEW_METRICS
                deficit = sum(
                    max(
                        0,
                        MIN_NEW_METRIC_FAMILY_COVERAGE[current_split]
                        - coverage[current_split][metric],
                    )
                    for metric in metrics
                )
                total_seen = sum(coverage[current_split][metric] for metric in metrics)
                return (
                    -deficit,
                    total_seen,
                    -len(metrics),
                    _stable_key({"family_id": candidate["family_id"]}),
                )

            chosen = min(choices, key=priority)
            pending.remove(chosen)
            reserved.add(str(chosen["family_id"]))
            for metric in set(chosen["query_spec"].metric_ids) & NEW_METRICS:
                coverage[split][metric] += 1
            assigned.append({**chosen, "split": split})
    return assigned


def build_rows() -> list[dict[str, Any]]:
    """Return the fixed 300-family manifest as JSON-serializable records."""
    pools = _candidate_pools()
    expected_totals = {
        bucket: sum(BUCKET_SPLIT_TARGETS[bucket].values()) for bucket in BUCKETS
    }
    # Finite singleton capacity is a design assertion, not a convenience
    # fallback. If a contract edit changes it, a reviewer must revisit quotas.
    assert len(pools["single_scalar"]) == expected_totals["single_scalar"] == 18
    assert len(pools["single_dimension"]) >= expected_totals["single_dimension"] == 20
    assert sum(
        candidate["query_spec"].result_shape == "category_grouped"
        for candidate in pools["single_dimension"]
    ) == sum(CATEGORY_DIMENSION_SPLIT_TARGETS.values()) == 8
    assert len(pools["single_purchase_series"]) == expected_totals["single_purchase_series"] == 40
    assert len(pools["single_review_series"]) == expected_totals["single_review_series"] == 5

    reserved: set[str] = set()
    coverage: dict[str, Counter[str]] = defaultdict(Counter)
    # Reserve structural multi-metric programs before normal multi buckets,
    # but let singleton buckets allocate first so their finite family space is
    # explicitly represented in every allowed split.
    allocation_order = (
        "single_scalar",
        "single_dimension",
        "single_purchase_series",
        "single_review_series",
        "structural_hard",
        "multi_scalar",
        "multi_dimension",
        "multi_time_series",
    )
    assigned = [
        row
        for bucket in allocation_order
        for row in _allocate_bucket(
            pools[bucket], bucket, reserved=reserved, coverage=coverage
        )
    ]
    if Counter(row["split"] for row in assigned) != Counter(
        {"train": 200, "validation": 50, "in_domain_test": 50}
    ):
        raise AssertionError("v3 family split targets drifted")
    if Counter(row["primary_bucket"] for row in assigned) != Counter(expected_totals):
        raise AssertionError("v3 family bucket targets drifted")
    if len({row["family_id"] for row in assigned}) != 300:
        raise AssertionError("v3 family IDs are not unique")
    for split in SPLITS:
        undercovered = {
            metric: coverage[split][metric]
            for metric in sorted(NEW_METRICS)
            if coverage[split][metric] < MIN_NEW_METRIC_FAMILY_COVERAGE[split]
        }
        if undercovered:
            raise AssertionError(f"new metric coverage is too low in {split}: {undercovered}")

    per_split_index: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for candidate in sorted(
        assigned,
        key=lambda row: (str(row["split"]), str(row["primary_bucket"]), str(row["family_id"])),
    ):
        split = str(candidate["split"])
        per_split_index[split] += 1
        spec = candidate["query_spec"]
        # Assert now, so a fixture cannot be created from a stale or default
        # v2 workspace even if a future caller changes candidate enumeration.
        if spec.workspace != WorkspacePin.current(OLIST_V3_WORKSPACE):
            raise AssertionError("v3 coverage seed has a workspace-pin drift")
        if (
            not set(spec.metric_ids) & NEW_METRICS
            and spec.result_shape != "category_grouped"
        ):
            raise AssertionError("non-category v3 coverage seed has no new v3 metric")
        validate_query_spec(QuerySpec.create(**{
            "workspace": spec.workspace,
            "metric_ids": spec.metric_ids,
            "result_shape": spec.result_shape,
            "dimension": spec.dimension,
            "time": spec.time,
            "join_program_id": spec.join_program_id,
            "required_result_columns": spec.required_result_columns,
            "attribution_rule_id": spec.attribution_rule_id,
        }), _catalog())
        rows.append(
            {
                "seed_schema_version": SEED_SCHEMA_VERSION,
                "seed_id": f"olist-v3-coverage-{split}-{per_split_index[split]:03d}",
                "split": split,
                "primary_bucket": candidate["primary_bucket"],
                "family_id": candidate["family_id"],
                "risk_tags": list(candidate["risk_tags"]),
                "instance_window_policy": candidate["instance_window_policy"],
                "query_spec": spec.as_dict(),
            }
        )
    return rows


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    args = parser.parse_args()
    rows = build_rows()
    write_rows(args.output_jsonl, rows)
    print(
        json.dumps(
            {
                "output": str(args.output_jsonl),
                "families": len(rows),
                "by_split": dict(Counter(row["split"] for row in rows)),
                "by_bucket": dict(Counter(row["primary_bucket"] for row in rows)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
