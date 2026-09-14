#!/usr/bin/env python3
"""Materialize the balanced Olist v3 structural/Gold candidate release.

This command is deliberately before Chinese surface generation and SFT JSONL
creation.  It rebuilds every selected legacy structure under the isolated v3
workspace, renders deterministic PostgreSQL Gold SQL, and writes hash-bound
external artifacts.  It never executes SQL, calls a model, reads protected
holdout content, or starts GPU work.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import itertools
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE  # noqa: E402
from data_analysis_agent.olist_queryspec import (  # noqa: E402
    QUERY_SPEC_SCHEMA_VERSION,
    RENDERER_VERSION,
    METRIC_SQL_REGISTRY,
    QuerySpec,
    QueryTime,
    WorkspacePin,
    render_gold_sql,
    validate_query_spec,
)
from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402
from scripts.post_training.data.materialize_olist_queryspecs import (  # noqa: E402
    family_fingerprint,
    family_id,
    sha256_file,
)


LEGACY_RELEASE_VERSION = "olist-v3-balanced-structural-release-v1"
RELEASE_VERSION = "olist-v3-balanced-structural-release-v1.1"
SEED_SCHEMA_VERSION = "olist-v3-coverage-family-seed-v1"
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
TARGETS: Mapping[str, Mapping[str, int]] = {
    "single_scalar": {"train": 110, "validation": 25, "in_domain_test": 25},
    "multi_scalar": {"train": 600, "validation": 150, "in_domain_test": 150},
    # The original 200-row single-dimension target exceeded its finite
    # capacity once v3/test split pins were enforced.  Reallocate only the
    # 12 unavailable train rows to the capacity-rich multi-dimension bucket;
    # never duplicate SQL or weaken family isolation to hit a round number.
    "single_dimension": {"train": 122, "validation": 33, "in_domain_test": 33},
    "multi_dimension": {"train": 478, "validation": 117, "in_domain_test": 117},
    "single_purchase_series": {"train": 400, "validation": 100, "in_domain_test": 100},
    "single_review_series": {"train": 72, "validation": 19, "in_domain_test": 19},
    "multi_time_series": {"train": 750, "validation": 188, "in_domain_test": 187},
    "structural_hard": {"train": 468, "validation": 118, "in_domain_test": 119},
}
SPLIT_TARGETS = {
    split: sum(TARGETS[bucket][split] for bucket in BUCKETS) for split in SPLITS
}

# These are absolute, non-overlapping Olist windows.  They are a release
# contract, rather than values guessed from the current calendar.  A bounded
# window is part of an instance but intentionally not part of family identity.
DATE_WINDOWS = (
    ("2016-10-01", "2017-01-01"),
    ("2017-01-01", "2017-04-01"),
    ("2017-04-01", "2017-07-01"),
    ("2017-07-01", "2017-10-01"),
    ("2017-10-01", "2018-01-01"),
    ("2018-01-01", "2018-04-01"),
    ("2018-04-01", "2018-07-01"),
    ("2018-07-01", "2018-10-01"),
)
DEFAULT_SEED_FIXTURE = (
    ROOT / "data" / "fixtures" / "olist_v3_coverage_family_seeds_v1.jsonl"
)

# v3.1 does not rewrite the frozen v1 seed fixture or its already-admitted
# release.  It makes one explicit new-release allocation: the finite
# freight/category absolute-range family (eight legal windows) moves from test
# to train.  This raises category-grouped train exposure from 9 to 17 while
# retaining four distinct category families and all four item metrics in the
# final test.  No order/review category attribution is introduced.
V3_1_SEED_SPLIT_OVERRIDES: Mapping[str, str] = {
    "olist-v3-coverage-in_domain_test-031": "train",
}

NEW_V3_METRICS = frozenset(
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

# Row exposure is not a substitute for independent programs, so both row and
# family floors are enforced.  The thresholds deliberately raise the nine new
# metrics above the old 151--180 train-row range without requiring equal metric
# frequency or duplicate SQL.  Validation/test retain meaningful but smaller
# coverage because they are model-selection/evaluation sets, not exposure pools.
NEW_V3_METRIC_ROW_MINIMA: Mapping[str, Mapping[str, int]] = {
    "train": dict.fromkeys(NEW_V3_METRICS, 230),
    "validation": dict.fromkeys(NEW_V3_METRICS, 50),
    "in_domain_test": dict.fromkeys(NEW_V3_METRICS, 50),
}
NEW_V3_METRIC_FAMILY_MINIMA: Mapping[str, Mapping[str, int]] = {
    "train": dict.fromkeys(NEW_V3_METRICS, 20),
    "validation": dict.fromkeys(NEW_V3_METRICS, 5),
    "in_domain_test": dict.fromkeys(NEW_V3_METRICS, 5),
}

# Category grouping is genuinely finite under the no-attribution contract.
# These are row/family targets that can be met by the eight legal item-grain
# families; they are not an excuse to group order/review metrics by category.
CATEGORY_GROUPED_ROW_MINIMA: Mapping[str, int] = {
    "train": 17,
    "validation": 8,
    "in_domain_test": 11,
}
CATEGORY_GROUPED_FAMILY_MINIMA: Mapping[str, int] = {
    "train": 3,
    "validation": 1,
    "in_domain_test": 4,
}

# Enforce a lower bound per time grain in the smaller splits.  The train split
# is already large and is still reported, but its quota is not made needlessly
# rigid.  This specifically repairs validation-month and test-day starvation.
TIME_GRAIN_ROW_MINIMA: Mapping[str, Mapping[str, int]] = {
    "validation": dict.fromkeys(("day", "week", "month", "quarter", "year"), 70),
    "in_domain_test": dict.fromkeys(("day", "week", "month", "quarter", "year"), 70),
}

_ITEM_METRICS = frozenset(
    metric_id
    for metric_id, definition in METRIC_SQL_REGISTRY.items()
    if "fact_order_items" in definition.base_tables
)
_PURCHASE_METRICS = tuple(
    sorted(
        metric_id
        for metric_id, definition in METRIC_SQL_REGISTRY.items()
        if definition.time_family == "purchase"
    )
)
_REVIEW_METRICS = tuple(
    sorted(
        metric_id
        for metric_id, definition in METRIC_SQL_REGISTRY.items()
        if definition.time_family == "review"
    )
)
_ALL_METRICS = tuple(sorted(METRIC_SQL_REGISTRY))
_STRUCTURAL_METRICS = frozenset(
    {
        "unique_customer_count",
        "canceled_order_count",
        "delivered_order_count",
        "unavailable_order_count",
        "average_items_per_order",
        "average_order_value",
        "approval_latency_days",
        "carrier_handoff_days",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v3-seeds-jsonl", type=Path, default=DEFAULT_SEED_FIXTURE)
    parser.add_argument("--v2-query-specs-jsonl", type=Path, required=True)
    parser.add_argument("--protected-summary-json", type=Path, required=True)
    parser.add_argument("--protected-evidence-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="Fixed ISO-8601 time for byte-reproducible manifest output.",
    )
    return parser.parse_args()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_key(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{label} has invalid JSON at {path}:{line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} row {line_number} must be an object")
        rows.append(row)
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows


def _external_existing_file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("release output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _load_protected_fingerprints(
    summary_path: Path, evidence_path: Path
) -> frozenset[str]:
    summary_path = _external_existing_file(summary_path, "protected family summary")
    evidence_path = _external_existing_file(evidence_path, "protected family evidence")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("protected family inputs must be valid JSON") from exc
    fingerprints = (
        summary.get("family_fingerprints") if isinstance(summary, dict) else None
    )
    if (
        not isinstance(fingerprints, list)
        or summary.get("summary_version") != "olist-protected-family-summary-v1"
        or any(not isinstance(item, str) or len(item) != 64 for item in fingerprints)
        or len(fingerprints) != len(set(fingerprints))
    ):
        raise ValueError("protected family summary violates its hash-only contract")
    if (
        not isinstance(evidence, dict)
        or evidence.get("protected_summary_sha256") != sha256_file(summary_path)
        or evidence.get("protected_summary_version") != summary["summary_version"]
    ):
        raise ValueError("protected family evidence does not bind the supplied summary")
    return frozenset(fingerprints)


def _risk_tags(spec: QuerySpec, *, structural_focus: bool = False) -> tuple[str, ...]:
    metrics = set(spec.metric_ids)
    tags: set[str] = set()
    if len(metrics) > 1:
        tags.add("multi_metric_cte_merge")
    if spec.result_shape == "time_series":
        tags.add("time_bucket")
    if spec.result_shape in {"state_grouped", "category_grouped"}:
        tags.add("direct_dimension_grouping")
    if "unique_customer_count" in metrics:
        tags.add("count_distinct_customer")
    if metrics & {
        "canceled_order_count",
        "delivered_order_count",
        "unavailable_order_count",
    }:
        tags.add("status_filter_and_distinct_order")
    if metrics & {"average_items_per_order", "average_order_value"}:
        tags.add("two_stage_order_aggregation")
    if metrics & {"approval_latency_days", "carrier_handoff_days"}:
        tags.add("nonnull_nonnegative_latency")
    if "review_count" in metrics:
        tags.add("review_fact_grain")
    if structural_focus:
        tags.add("structural_focus")
    return tuple(sorted(tags))


def classify_bucket(spec: QuerySpec) -> str:
    """Assign exactly one main learning objective without hiding singletons."""
    if len(spec.metric_ids) > 1 and set(spec.metric_ids) & _STRUCTURAL_METRICS:
        return "structural_hard"
    if spec.result_shape == "scalar":
        return "single_scalar" if len(spec.metric_ids) == 1 else "multi_scalar"
    if spec.result_shape in {"state_grouped", "category_grouped"}:
        return "single_dimension" if len(spec.metric_ids) == 1 else "multi_dimension"
    if spec.result_shape == "time_series":
        if len(spec.metric_ids) > 1:
            return "multi_time_series"
        metric = spec.metric_ids[0]
        return (
            "single_review_series"
            if metric in _REVIEW_METRICS
            else "single_purchase_series"
        )
    raise AssertionError(f"unsupported result shape: {spec.result_shape}")


def _make_v3_spec(
    *,
    metric_ids: tuple[str, ...],
    result_shape: str,
    time: QueryTime,
    catalog: Catalog,
    dimension: str | None = None,
) -> QuerySpec:
    return validate_query_spec(
        QuerySpec.create(
            workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
            metric_ids=metric_ids,
            result_shape=result_shape,
            time=time,
            dimension=dimension,
        ),
        catalog,
    )


def _with_window(
    spec: QuerySpec, start: str, end_exclusive: str, catalog: Catalog
) -> QuerySpec:
    return _make_v3_spec(
        metric_ids=spec.metric_ids,
        result_shape=spec.result_shape,
        dimension=spec.dimension,
        time=QueryTime(spec.time.mode, start, end_exclusive, spec.time.grain),
        catalog=catalog,
    )


def _instance_specs(spec: QuerySpec, catalog: Catalog) -> list[QuerySpec]:
    if spec.time.mode == "all_time":
        return [spec]
    return [_with_window(spec, start, end, catalog) for start, end in DATE_WINDOWS]


def _candidate(
    *,
    seed_id: str,
    source_seed_id: str,
    source: str,
    source_rank: int,
    split: str | None,
    bucket: str,
    risk_tags: Iterable[str],
    spec: QuerySpec,
    required_v3_seed: bool = False,
    allowed_splits: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if split is not None and split not in SPLITS:
        raise ValueError(f"unsupported candidate split: {split}")
    if bucket not in BUCKETS:
        raise ValueError(f"unsupported candidate bucket: {bucket}")
    candidate_splits = allowed_splits or ((split,) if split is not None else SPLITS)
    if not candidate_splits or any(item not in SPLITS for item in candidate_splits):
        raise ValueError("candidate allowed_splits is invalid")
    if split is not None and split not in candidate_splits:
        raise ValueError("fixed split must be one of candidate allowed_splits")
    return {
        "seed_id": seed_id,
        "source_seed_id": source_seed_id,
        "candidate_source": source,
        "source_rank": source_rank,
        "fixed_split": split,
        "allowed_splits": candidate_splits,
        "primary_bucket": bucket,
        "risk_tags": sorted(set(risk_tags)),
        "family_id": family_id(spec),
        "query_spec": spec,
        "required_v3_seed": required_v3_seed,
    }


def load_v3_seed_candidates(
    path: Path,
    catalog: Catalog,
    *,
    split_overrides: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Expand frozen v3 families, with explicit successor-release split overrides.

    ``split_overrides`` is intentionally an argument rather than a mutation of
    the v1 seed fixture.  It keeps old releases reproducible while making a
    v3.1 allocation change visible in the new release manifest and tests.
    """
    rows = _read_jsonl(path.resolve(), "v3 coverage seed fixture")
    overrides = dict(split_overrides or {})
    raw_seed_ids = {str(row.get("seed_id")) for row in rows}
    if set(overrides) - raw_seed_ids or any(
        split not in SPLITS for split in overrides.values()
    ):
        raise ValueError(
            "v3 seed split overrides reference an unknown seed or unsupported split"
        )
    expected_pin = WorkspacePin.current(OLIST_V3_WORKSPACE)
    output: list[dict[str, Any]] = []
    seen_seed_ids: set[str] = set()
    seen_family_ids: set[str] = set()
    for raw in rows:
        if set(raw) != {
            "seed_schema_version",
            "seed_id",
            "split",
            "primary_bucket",
            "family_id",
            "risk_tags",
            "instance_window_policy",
            "query_spec",
        }:
            raise ValueError("v3 coverage seed has unsupported fields")
        if raw["seed_schema_version"] != SEED_SCHEMA_VERSION:
            raise ValueError("v3 coverage seed schema version drifted")
        seed_id = raw["seed_id"]
        split = overrides.get(str(raw["seed_id"]), raw["split"])
        if (
            not isinstance(seed_id, str)
            or seed_id in seen_seed_ids
            or split not in SPLITS
        ):
            raise ValueError("v3 coverage seed identity is invalid")
        seen_seed_ids.add(seed_id)
        spec = QuerySpec.from_mapping(raw["query_spec"])
        if spec.workspace != expected_pin or validate_query_spec(spec, catalog) != spec:
            raise ValueError(
                f"v3 coverage seed {seed_id} has workspace or QuerySpec drift"
            )
        family = family_id(spec)
        if raw["family_id"] != family or family in seen_family_ids:
            raise ValueError(f"v3 coverage seed {seed_id} has family identity drift")
        seen_family_ids.add(family)
        instances = _instance_specs(spec, catalog)
        expected_count = 1 if spec.time.mode == "all_time" else len(DATE_WINDOWS)
        if len(instances) != expected_count:
            raise AssertionError("v3 seed instance-window contract drifted")
        for index, instance in enumerate(instances, 1):
            output.append(
                _candidate(
                    seed_id=f"olist-v3-release-{seed_id}-i{index:02d}",
                    source_seed_id=seed_id,
                    source="v3_frozen_seed",
                    source_rank=0,
                    split=split,
                    bucket=str(raw["primary_bucket"]),
                    risk_tags=raw["risk_tags"],
                    spec=instance,
                    required_v3_seed=index == 1,
                )
            )
    if len(seen_seed_ids) != 300:
        raise ValueError(
            "v3 coverage fixture no longer has its frozen 300-family budget"
        )
    return output


def _repin_v2_spec(old_spec: QuerySpec, catalog: Catalog) -> QuerySpec:
    return _make_v3_spec(
        metric_ids=old_spec.metric_ids,
        result_shape=old_spec.result_shape,
        dimension=old_spec.dimension,
        time=old_spec.time,
        catalog=catalog,
    )


def load_v2_reconstructed_candidates(
    path: Path,
    catalog: Catalog,
    protected_fingerprints: frozenset[str],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Use only v2 structural identity, keeping its original family split."""
    rows = _read_jsonl(
        _external_existing_file(path, "v2 QuerySpec source"), "v2 QuerySpec source"
    )
    output: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    seen_seed_ids: set[str] = set()
    old_family_splits: dict[str, str] = {}
    for raw in rows:
        seed_id = raw.get("seed_id")
        split = raw.get("split")
        if (
            not isinstance(seed_id, str)
            or seed_id in seen_seed_ids
            or split not in SPLITS
        ):
            raise ValueError("v2 QuerySpec source has invalid seed or split identity")
        seen_seed_ids.add(seed_id)
        old_spec = QuerySpec.from_mapping(raw.get("query_spec", {}))
        old_family = family_id(old_spec)
        prior_split = old_family_splits.get(old_family)
        if prior_split is not None and prior_split != split:
            raise ValueError("v2 source family crosses its historical split")
        old_family_splits[old_family] = split
        if family_fingerprint(old_family) in protected_fingerprints:
            skipped["protected_v2_family"] += 1
            continue
        spec = _repin_v2_spec(old_spec, catalog)
        bucket = classify_bucket(spec)
        # A legacy row establishes a same-split structural program, not a
        # privileged one-off date endpoint.  Re-expand only its bounded/series
        # form under the v3 eight-window contract.  This fills scarce singleton
        # exposure with distinct QuerySpec/SQL instances while preserving the
        # legacy family boundary; all-time structures still yield one row.
        for index, instance in enumerate(_instance_specs(spec, catalog), 1):
            output.append(
                _candidate(
                    seed_id=f"olist-v3-release-v2-{seed_id}-i{index:02d}",
                    source_seed_id=seed_id,
                    source="v2_reconstructed_structure",
                    source_rank=1,
                    # The old final-test split is immutable: it never leaks
                    # into a new train/validation row.  Old train and
                    # validation structures are *not* a new split contract,
                    # however.  They form one candidate pool which is
                    # reallocated below under the v3 family-isolation rule so
                    # sparse singleton exposure can meet the new balanced
                    # release targets without duplicating a program.
                    split=split if split == "in_domain_test" else None,
                    bucket=bucket,
                    risk_tags=_risk_tags(
                        instance, structural_focus=bucket == "structural_hard"
                    ),
                    spec=instance,
                    allowed_splits=("in_domain_test",)
                    if split == "in_domain_test"
                    else ("train", "validation"),
                )
            )
    if len(seen_seed_ids) != 3600:
        raise ValueError(
            "v2 source does not match the frozen 3,600-row structural release"
        )
    return output, skipped


def build_supplemental_candidates(catalog: Catalog) -> list[dict[str, Any]]:
    """Enumerate a finite v3-valid repair pool; it has no split assignment yet."""
    bases: dict[str, QuerySpec] = {}

    def add(
        metric_ids: tuple[str, ...],
        shape: str,
        time: QueryTime,
        dimension: str | None = None,
    ) -> None:
        try:
            spec = _make_v3_spec(
                metric_ids=metric_ids,
                result_shape=shape,
                dimension=dimension,
                time=time,
                catalog=catalog,
            )
        except ValueError:
            return
        bases.setdefault(spec.query_spec_id, spec)

    all_time = QueryTime("all_time")
    absolute = QueryTime("absolute_range", "2017-01-01", "2018-01-01")
    grains = ("day", "week", "month", "quarter", "year")

    for metric in _ALL_METRICS:
        for query_time in (all_time, absolute):
            add((metric,), "scalar", query_time)
            add((metric,), "state_grouped", query_time, "customer_state")
    for metric in sorted(_ITEM_METRICS):
        for query_time in (all_time, absolute):
            add((metric,), "category_grouped", query_time, "product_category_name")
    for metric in _PURCHASE_METRICS:
        for grain in grains:
            add(
                (metric,),
                "time_series",
                QueryTime("series", "2017-01-01", "2018-01-01", grain),
            )
    for metric in _REVIEW_METRICS:
        for grain in grains:
            add(
                (metric,),
                "time_series",
                QueryTime("series", "2017-01-01", "2018-01-01", grain),
            )

    # Two-metric programs are sufficient to repair the small residual in the
    # multi-scalar bucket.  The same finite legal grid also gives robust
    # fallback capacity without manufacturing arbitrary 3/4-metric mixtures.
    for metrics in itertools.combinations(_ALL_METRICS, 2):
        for query_time in (all_time, absolute):
            add(metrics, "scalar", query_time)
            add(metrics, "state_grouped", query_time, "customer_state")
    for metrics in itertools.combinations(_PURCHASE_METRICS, 2):
        for grain in grains:
            add(
                metrics,
                "time_series",
                QueryTime("series", "2017-01-01", "2018-01-01", grain),
            )
    for metrics in itertools.combinations(_REVIEW_METRICS, 2):
        for grain in grains:
            add(
                metrics,
                "time_series",
                QueryTime("series", "2017-01-01", "2018-01-01", grain),
            )

    output: list[dict[str, Any]] = []
    for spec in sorted(bases.values(), key=lambda value: _stable_key(value.as_dict())):
        bucket = classify_bucket(spec)
        for index, instance in enumerate(_instance_specs(spec, catalog), 1):
            output.append(
                _candidate(
                    seed_id=f"olist-v3-supplement-{family_id(spec)}-i{index:02d}",
                    source_seed_id=f"supplement-{family_id(spec)}",
                    source="v3_supplemental_template",
                    source_rank=2,
                    split=None,
                    bucket=bucket,
                    risk_tags=_risk_tags(
                        instance, structural_focus=bucket == "structural_hard"
                    ),
                    spec=instance,
                )
            )
    return output


def _candidate_key(candidate: Mapping[str, Any]) -> tuple[int, str]:
    spec = candidate["query_spec"]
    assert isinstance(spec, QuerySpec)
    return (
        int(candidate["source_rank"]),
        _stable_key(
            {
                "source_seed_id": candidate["source_seed_id"],
                "family_id": candidate["family_id"],
                "query_spec_id": spec.query_spec_id,
            }
        ),
    )


def _remove_source_conflicts(
    v3_candidates: list[dict[str, Any]],
    v2_candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    """Preserve frozen v3 family allocation when legacy v2 structure collides."""
    v3_family_splits = {
        str(candidate["family_id"]): str(candidate["fixed_split"])
        for candidate in v3_candidates
    }
    accepted_v2: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    for candidate in v2_candidates:
        family = str(candidate["family_id"])
        v3_split = v3_family_splits.get(family)
        if v3_split is None:
            accepted_v2.append(candidate)
        else:
            # The v3 fixture is the stronger, newer coverage contract even
            # when its split happens to match the historical structure.
            exclusions[
                "v2_family_duplicate_prefer_v3"
                if v3_split in candidate["allowed_splits"]
                else "v2_family_cross_split_prefer_v3"
            ] += 1
    return v3_candidates, accepted_v2, exclusions


def _empty_coverage_state() -> dict[str, Any]:
    return {
        "metric_rows": {split: Counter() for split in SPLITS},
        "metric_families": {
            split: {metric: set() for metric in NEW_V3_METRICS} for split in SPLITS
        },
        "category_rows": Counter(),
        "category_families": {split: set() for split in SPLITS},
        "time_grain_rows": {split: Counter() for split in SPLITS},
    }


def _record_coverage(state: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    """Mutate one preallocated coverage state after a candidate is selected."""
    split = str(row["split"])
    family = str(row["family_id"])
    spec = row["query_spec"]
    if isinstance(spec, Mapping):
        spec = QuerySpec.from_mapping(spec)
    if not isinstance(spec, QuerySpec):
        raise ValueError("coverage row has an invalid QuerySpec")
    for metric in spec.metric_ids:
        if metric in NEW_V3_METRICS:
            state["metric_rows"][split][metric] += 1
            state["metric_families"][split][metric].add(family)
    if spec.result_shape == "category_grouped":
        state["category_rows"][split] += 1
        state["category_families"][split].add(family)
    if spec.time.mode == "series" and spec.time.grain is not None:
        state["time_grain_rows"][split][spec.time.grain] += 1


def _coverage_state(selected: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate the v3.1 balance dimensions from selected rows."""
    state = _empty_coverage_state()
    for row in selected:
        _record_coverage(state, row)
    return state


def _coverage_priority(
    candidate: Mapping[str, Any], split: str, state: Mapping[str, Any]
) -> int:
    """Score a candidate by unsatisfied v3.1 balance requirements.

    Family coverage is weighted above row exposure so eight date windows from
    one family cannot satisfy a requirement intended to broaden programs.
    """
    spec = candidate["query_spec"]
    assert isinstance(spec, QuerySpec)
    family = str(candidate["family_id"])
    score = 0
    metric_rows = state["metric_rows"][split]
    metric_families = state["metric_families"][split]
    for metric in sorted(set(spec.metric_ids) & NEW_V3_METRICS):
        if (
            len(metric_families[metric]) < NEW_V3_METRIC_FAMILY_MINIMA[split][metric]
            and family not in metric_families[metric]
        ):
            score += 10_000
        if metric_rows[metric] < NEW_V3_METRIC_ROW_MINIMA[split][metric]:
            score += 1_000
    if spec.result_shape == "category_grouped":
        if (
            family not in state["category_families"][split]
            and len(state["category_families"][split])
            < CATEGORY_GROUPED_FAMILY_MINIMA[split]
        ):
            score += 500
        if state["category_rows"][split] < CATEGORY_GROUPED_ROW_MINIMA[split]:
            score += 100
    if (
        split in TIME_GRAIN_ROW_MINIMA
        and spec.time.mode == "series"
        and spec.time.grain is not None
    ):
        if (
            state["time_grain_rows"][split][spec.time.grain]
            < TIME_GRAIN_ROW_MINIMA[split][spec.time.grain]
        ):
            score += 25
    return score


def balance_coverage_report(selected: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a JSON-serializable report and fail closed on v3.1 minima."""
    rows = list(selected)
    state = _coverage_state(rows)
    report: dict[str, Any] = {
        "new_v3_metrics": {},
        "category_grouped": {},
        "time_grains": {},
    }
    failures: list[str] = []
    for split in SPLITS:
        metrics = {}
        for metric in sorted(NEW_V3_METRICS):
            row_count = state["metric_rows"][split][metric]
            family_count = len(state["metric_families"][split][metric])
            metrics[metric] = {
                "rows": row_count,
                "families": family_count,
                "row_minimum": NEW_V3_METRIC_ROW_MINIMA[split][metric],
                "family_minimum": NEW_V3_METRIC_FAMILY_MINIMA[split][metric],
            }
            if (
                row_count < NEW_V3_METRIC_ROW_MINIMA[split][metric]
                or family_count < NEW_V3_METRIC_FAMILY_MINIMA[split][metric]
            ):
                failures.append(f"{split}/{metric}")
        report["new_v3_metrics"][split] = metrics
        category = {
            "rows": state["category_rows"][split],
            "families": len(state["category_families"][split]),
            "row_minimum": CATEGORY_GROUPED_ROW_MINIMA[split],
            "family_minimum": CATEGORY_GROUPED_FAMILY_MINIMA[split],
        }
        report["category_grouped"][split] = category
        if (
            category["rows"] < category["row_minimum"]
            or category["families"] < category["family_minimum"]
        ):
            failures.append(f"{split}/category_grouped")
        grains = {
            grain: {
                "rows": state["time_grain_rows"][split][grain],
                "row_minimum": TIME_GRAIN_ROW_MINIMA.get(split, {}).get(grain),
            }
            for grain in ("day", "week", "month", "quarter", "year")
        }
        report["time_grains"][split] = grains
        for grain, detail in grains.items():
            if (
                detail["row_minimum"] is not None
                and detail["rows"] < detail["row_minimum"]
            ):
                failures.append(f"{split}/time_grain/{grain}")
    report["status"] = "pass" if not failures else "fail"
    report["failures"] = failures
    if failures:
        raise ValueError(
            "v3.1 balance contract cannot be satisfied: " + ", ".join(failures)
        )
    return report


def select_release_candidates(
    v3_candidates: list[dict[str, Any]],
    v2_candidates: list[dict[str, Any]],
    supplemental_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select exact bucket quotas while enforcing family and QuerySpec isolation."""
    v3_candidates, v2_candidates, _ = _remove_source_conflicts(
        v3_candidates, v2_candidates
    )
    selected: list[dict[str, Any]] = []
    selected_query_spec_ids: set[str] = set()
    selected_by_split_bucket: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    family_split: dict[str, str] = {}
    coverage_state = _empty_coverage_state()

    fixed_candidates = [*v3_candidates, *v2_candidates]
    for candidate in fixed_candidates:
        family = str(candidate["family_id"])
        split = candidate["fixed_split"]
        if split is None:
            continue
        split = str(split)
        prior = family_split.get(family)
        if prior is not None and prior != split:
            raise ValueError(f"fixed candidate family crosses split: {family}")
        family_split[family] = split

    def add(candidate: dict[str, Any], split: str) -> bool:
        spec = candidate["query_spec"]
        assert isinstance(spec, QuerySpec)
        family = str(candidate["family_id"])
        if split not in candidate["allowed_splits"]:
            return False
        prior = family_split.get(family)
        if prior is not None and prior != split:
            return False
        if spec.query_spec_id in selected_query_spec_ids:
            return False
        bucket = str(candidate["primary_bucket"])
        if len(selected_by_split_bucket[(split, bucket)]) >= TARGETS[bucket][split]:
            return False
        family_split[family] = split
        selected_query_spec_ids.add(spec.query_spec_id)
        selected_by_split_bucket[(split, bucket)].append(candidate)
        selected_row = {**candidate, "split": split}
        selected.append(selected_row)
        _record_coverage(coverage_state, selected_row)
        return True

    # Every frozen v3 family receives one release instance before row exposure
    # is filled.  The per-bucket targets were intentionally chosen above these
    # required coverage counts.
    mandatory = [
        candidate for candidate in v3_candidates if candidate["required_v3_seed"]
    ]
    if len(mandatory) != 300:
        raise ValueError(
            "all 300 frozen v3 families must contribute one required instance"
        )
    for candidate in sorted(mandatory, key=_candidate_key):
        if not add(candidate, str(candidate["fixed_split"])):
            raise ValueError(
                "a frozen v3 family cannot be admitted to its release quota"
            )

    def fill_bucket(
        candidates: Iterable[dict[str, Any]],
        *,
        split: str,
        bucket: str,
        require_target: bool,
    ) -> None:
        """Fill one split/bucket in family-sized queues, not repeated full scans.

        A date-instance pool can contain tens of thousands of v2-derived
        candidates.  Choosing one row by rescanning every candidate would be
        quadratic and has no quality benefit.  Grouping first keeps the same
        selection rule—prefer an already allocated same-split family—while
        making the operation proportional to candidate families plus selected
        rows.
        """
        by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            if split in candidate["allowed_splits"]:
                by_family[str(candidate["family_id"])].append(candidate)
        for rows in by_family.values():
            rows.sort(key=_candidate_key)
        cursors: dict[str, int] = defaultdict(int)

        def next_available(family: str) -> dict[str, Any] | None:
            rows = by_family[family]
            cursor = cursors[family]
            while cursor < len(rows):
                candidate = rows[cursor]
                if candidate["query_spec"].query_spec_id not in selected_query_spec_ids:
                    cursors[family] = cursor
                    return candidate
                cursor += 1
            cursors[family] = cursor
            return None

        while len(selected_by_split_bucket[(split, bucket)]) < TARGETS[bucket][split]:
            choices: list[tuple[int, int, tuple[int, str], str]] = []
            for family in by_family:
                prior = family_split.get(family)
                if prior is not None and prior != split:
                    continue
                candidate = next_available(family)
                if candidate is not None:
                    choices.append(
                        (
                            -_coverage_priority(candidate, split, coverage_state),
                            0 if prior == split else 1,
                            _candidate_key(candidate),
                            family,
                        )
                    )
            if not choices:
                if require_target:
                    raise ValueError(
                        f"candidate pool cannot meet {split}/{bucket} target "
                        f"({len(selected_by_split_bucket[(split, bucket)])}/"
                        f"{TARGETS[bucket][split]})"
                    )
                return
            _, _, _, family = min(choices)
            candidate = next_available(family)
            assert candidate is not None
            cursors[family] += 1
            # A duplicate QuerySpec can be present in independent historical
            # source rows.  It is skipped by add(), then the next queue item
            # remains eligible without assigning the family incorrectly.
            add(candidate, split)

    # Fill against one combined pool.  The old fixed-first ordering saturated
    # quotas with legacy v2 programs before the new v3 metrics or underused
    # time grains could compete.  The score above only changes selection while
    # a documented balance floor is unmet; after that, source rank/family reuse
    # retain the same deterministic low-risk preference.
    candidates_by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in [*fixed_candidates, *supplemental_candidates]:
        if candidate["required_v3_seed"]:
            continue
        family = str(candidate["family_id"])
        if (
            family in family_split
            and family_split[family] not in candidate["allowed_splits"]
        ):
            continue
        candidates_by_bucket[str(candidate["primary_bucket"])].append(candidate)
    # Single-dimension has a proven finite capacity (especially after the
    # explicit category split repair).  Let the pre-existing fixed family pool
    # claim its allowed splits first, then use supplemental templates only for
    # any residual.  Other buckets can safely let coverage requirements rank a
    # combined pool from the start.
    for split in ("in_domain_test", "validation", "train"):
        fill_bucket(
            (
                candidate
                for candidate in fixed_candidates
                if candidate["primary_bucket"] == "single_dimension"
                and not candidate["required_v3_seed"]
            ),
            split=split,
            bucket="single_dimension",
            require_target=False,
        )
    for split in ("in_domain_test", "validation", "train"):
        for bucket in BUCKETS:
            if bucket == "single_dimension":
                pool = [
                    candidate
                    for candidate in supplemental_candidates
                    if candidate["primary_bucket"] == bucket
                ]
            else:
                pool = candidates_by_bucket[bucket]
            fill_bucket(
                pool,
                split=split,
                bucket=bucket,
                require_target=True,
            )

    actual = {
        split: {
            bucket: len(selected_by_split_bucket[(split, bucket)]) for bucket in BUCKETS
        }
        for split in SPLITS
    }
    expected = {
        split: {bucket: TARGETS[bucket][split] for bucket in BUCKETS}
        for split in SPLITS
    }
    if actual != expected or Counter(row["split"] for row in selected) != Counter(
        SPLIT_TARGETS
    ):
        raise AssertionError("selected release rows do not match the frozen quota")
    if {
        row["source_seed_id"]
        for row in selected
        if row["candidate_source"] == "v3_frozen_seed"
    } != {row["source_seed_id"] for row in mandatory}:
        raise AssertionError("a frozen v3 family was lost after quota allocation")
    balance_coverage_report(selected)
    return sorted(selected, key=lambda row: (str(row["split"]), str(row["seed_id"])))


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _rows_for_output(
    selected: list[dict[str, Any]], catalog: Catalog
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    query_rows: list[dict[str, Any]] = []
    gold_rows: list[dict[str, Any]] = []
    seen_sql_hashes: set[str] = set()
    for candidate in selected:
        spec = candidate["query_spec"]
        assert isinstance(spec, QuerySpec)
        artifact = render_gold_sql(spec, catalog)
        if artifact.sql_sha256 in seen_sql_hashes:
            raise ValueError(
                "canonical Gold SQL hash is duplicated in selected release"
            )
        seen_sql_hashes.add(artifact.sql_sha256)
        common = {
            "seed_id": candidate["seed_id"],
            "split": candidate["split"],
            "family_id": candidate["family_id"],
            "sql_program_id": spec.join_program_id,
            "candidate_source": candidate["candidate_source"],
            "source_seed_id": candidate["source_seed_id"],
            "primary_bucket": candidate["primary_bucket"],
            "risk_tags": candidate["risk_tags"],
        }
        query_rows.append({**common, "query_spec": spec.as_dict()})
        gold_rows.append(
            {**common, "gold_artifact": {**artifact.as_dict(), "sql": artifact.sql}}
        )
    return query_rows, gold_rows


def _split_overlap(rows: list[dict[str, Any]], field: str) -> list[str]:
    def value(row: Mapping[str, Any]) -> str:
        direct = row.get(field)
        if direct is not None:
            return str(direct)
        if field == "query_spec_id":
            spec = row.get("query_spec")
            if isinstance(spec, Mapping) and isinstance(spec.get("query_spec_id"), str):
                return str(spec["query_spec_id"])
        raise ValueError(f"row has no {field} identity")

    values = {
        split: {value(row) for row in rows if row["split"] == split} for split in SPLITS
    }
    return sorted(
        (values["train"] & values["validation"])
        | (values["train"] & values["in_domain_test"])
        | (values["validation"] & values["in_domain_test"])
    )


def build_release(
    *,
    v3_seeds_jsonl: Path,
    v2_query_specs_jsonl: Path,
    protected_summary_json: Path,
    protected_evidence_json: Path,
    output_dir: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build an atomic, hash-bound 4,500-row v3 structural Gold release."""
    output_dir = _external_new_dir(output_dir)
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    protected = _load_protected_fingerprints(
        protected_summary_json, protected_evidence_json
    )
    v3_candidates = load_v3_seed_candidates(
        v3_seeds_jsonl,
        catalog,
        split_overrides=V3_1_SEED_SPLIT_OVERRIDES,
    )
    v2_candidates, v2_skips = load_v2_reconstructed_candidates(
        v2_query_specs_jsonl, catalog, protected
    )
    v3_candidates, v2_candidates, source_conflicts = _remove_source_conflicts(
        v3_candidates, v2_candidates
    )
    selected = select_release_candidates(
        v3_candidates, v2_candidates, build_supplemental_candidates(catalog)
    )
    balance_report = balance_coverage_report(selected)
    query_rows, gold_rows = _rows_for_output(selected, catalog)
    if len(query_rows) != len(gold_rows) != 4500:
        raise AssertionError(
            "release did not produce the expected 4,500 structural rows"
        )
    if _split_overlap(query_rows, "family_id"):
        raise AssertionError("family IDs cross formal splits")
    if _split_overlap(query_rows, "query_spec_id"):
        raise AssertionError("QuerySpec IDs cross formal splits")
    if _split_overlap(
        [
            {**row, "gold_sql_sha256": row["gold_artifact"]["sql_sha256"]}
            for row in gold_rows
        ],
        "gold_sql_sha256",
    ):
        raise AssertionError("canonical Gold SQL hashes cross formal splits")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        query_path = staging / "query_specs.jsonl"
        gold_path = staging / "gold_sql.jsonl"
        _write_jsonl(query_path, query_rows)
        _write_jsonl(gold_path, gold_rows)
        (staging / "query_specs").mkdir()
        (staging / "gold_sql").mkdir()
        split_outputs: dict[str, dict[str, str]] = {}
        for split in SPLITS:
            split_query_path = staging / "query_specs" / f"{split}.jsonl"
            split_gold_path = staging / "gold_sql" / f"{split}.jsonl"
            split_queries = [row for row in query_rows if row["split"] == split]
            split_gold = [row for row in gold_rows if row["split"] == split]
            _write_jsonl(split_query_path, split_queries)
            _write_jsonl(split_gold_path, split_gold)
            split_outputs[split] = {
                "query_specs_sha256": sha256_file(split_query_path),
                "gold_sql_sha256": sha256_file(split_gold_path),
            }

        bucket_counts = {
            split: {
                bucket: sum(
                    row["split"] == split and row["primary_bucket"] == bucket
                    for row in query_rows
                )
                for bucket in BUCKETS
            }
            for split in SPLITS
        }
        generated_at = generated_at or "unfixed-runtime-time"
        manifest = {
            "release_version": RELEASE_VERSION,
            "generated_at": generated_at,
            "query_spec_schema_version": QUERY_SPEC_SCHEMA_VERSION,
            "renderer_version": RENDERER_VERSION,
            "workspace": WorkspacePin.current(OLIST_V3_WORKSPACE).as_dict(),
            "source": {
                "v3_seeds_jsonl": str(v3_seeds_jsonl.resolve()),
                "v3_seeds_sha256": sha256_file(v3_seeds_jsonl.resolve()),
                "v2_query_specs_jsonl": str(v2_query_specs_jsonl.resolve()),
                "v2_query_specs_sha256": sha256_file(v2_query_specs_jsonl.resolve()),
                "protected_summary_json": str(protected_summary_json.resolve()),
                "protected_summary_sha256": sha256_file(
                    protected_summary_json.resolve()
                ),
                "protected_evidence_json": str(protected_evidence_json.resolve()),
                "protected_evidence_sha256": sha256_file(
                    protected_evidence_json.resolve()
                ),
                "v2_skips": dict(sorted(v2_skips.items())),
                "source_conflict_exclusions": dict(sorted(source_conflicts.items())),
                "v3_1_seed_split_overrides": dict(
                    sorted(V3_1_SEED_SPLIT_OVERRIDES.items())
                ),
            },
            "window_contract": {"windows": [list(window) for window in DATE_WINDOWS]},
            "targets": {
                split: {bucket: TARGETS[bucket][split] for bucket in BUCKETS}
                for split in SPLITS
            },
            "counts": {
                "rows": dict(Counter(row["split"] for row in query_rows)),
                "families": {
                    split: len(
                        {
                            row["family_id"]
                            for row in query_rows
                            if row["split"] == split
                        }
                    )
                    for split in SPLITS
                },
                "query_specs": {
                    split: len(
                        {
                            row["query_spec"]["query_spec_id"]
                            for row in query_rows
                            if row["split"] == split
                        }
                    )
                    for split in SPLITS
                },
                "canonical_gold_sql_hashes": len(
                    {row["gold_artifact"]["sql_sha256"] for row in gold_rows}
                ),
                "v3_required_families": 300,
                "by_bucket": bucket_counts,
                "by_source": dict(
                    Counter(row["candidate_source"] for row in query_rows)
                ),
                "v3_1_balance": balance_report,
            },
            "outputs": {
                "query_specs_jsonl": {
                    "rows": len(query_rows),
                    "sha256": sha256_file(query_path),
                },
                "gold_sql_jsonl": {
                    "rows": len(gold_rows),
                    "sha256": sha256_file(gold_path),
                },
                "split_outputs": split_outputs,
            },
            "checks": {
                "status": "pass",
                "bucket_targets_exact": bucket_counts
                == {
                    split: {bucket: TARGETS[bucket][split] for bucket in BUCKETS}
                    for split in SPLITS
                },
                "family_split_overlap": [],
                "query_spec_split_overlap": [],
                "canonical_gold_sql_hash_split_overlap": [],
                "all_v3_frozen_families_retained": True,
                "v3_1_balance_contract": "pass",
                "legacy_v2_reconstructed_under_v3": True,
                "sql_executed": False,
                "prompt_or_question_materialized": False,
                "model_called": False,
                "gpu_used": False,
                "protected_holdout_raw_read": False,
            },
        }
        (staging / "materialization_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    args = parse_args()
    manifest = build_release(
        v3_seeds_jsonl=args.v3_seeds_jsonl,
        v2_query_specs_jsonl=args.v2_query_specs_jsonl,
        protected_summary_json=args.protected_summary_json,
        protected_evidence_json=args.protected_evidence_json,
        output_dir=args.output_dir,
        generated_at=args.generated_at,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
