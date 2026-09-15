"""Build the deterministic, pre-admission TheLook v3 coverage seed fixture.

This command intentionally stops before Gold rendering, database access,
question generation, or model invocation.  It creates only QuerySpec-backed
coverage seeds, then verifies their frozen quota contract and their QuerySpec
identity does not reuse historical protected TheLook v2 cases.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[3]

from data_analysis_agent.thelook_queryspec import TheLookQueryTime  # noqa: E402
from data_analysis_agent.thelook_v2_queryspec import (  # noqa: E402
    METRIC_FACT_DOMAINS,
    TheLookV2QuerySpec,
)
from data_analysis_agent.thelook_v3_coverage import (  # noqa: E402
    THELOOK_V3_COVERAGE_SEED_VERSION,
    TheLookV3CoverageError,
    derive_thelook_v3_program_signature,
    derive_thelook_v3_risk_tags,
    load_thelook_v3_coverage_contract,
    validate_thelook_v3_coverage_seed,
)


DEFAULT_OUTPUT = ROOT / "data" / "fixtures" / "thelook_v3_coverage_seeds_v1.jsonl"
DEFAULT_V2_CASES = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "thelook-cross-schema-final-test-v2-20260910/cases.jsonl"
)


@dataclass(frozen=True)
class FamilyPlan:
    family_id: str
    shape_counts: Mapping[str, int]
    metric_count_counts: Mapping[int, int]
    dimension_counts: Mapping[str, int]


def _add_months(value: date, months: int) -> date:
    """Return the first day of the month exactly ``months`` after ``value``."""

    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero_based = divmod(month_index, 12)
    return date(year, month_zero_based + 1, 1)


def _fixed_offset_windows(*, span_months: int, count: int = 48) -> tuple[tuple[str, str], ...]:
    """Build a fixed, non-January-aligned candidate pool without random dates.

    The initial 16-window pool was too small: it caused identity collisions for
    repeated, legitimate metric programs before a 750-case fixture could be
    constructed.  These 48 candidates are static code constants at release
    construction time (not model output and not data-dependent sampling).  A
    later reader-role admission gate remains responsible for rejecting a window
    with an empty or over-budget result.
    """

    first_start = date(2019, 2, 1)
    windows: list[tuple[str, str]] = []
    offset = 0
    while len(windows) < count:
        start = _add_months(first_start, offset)
        offset += 1
        # v2's protected period candidates are January-aligned.  Keeping all
        # v3 candidates non-January prevents that known overlap pattern while
        # the separate protected-ID audit remains the authoritative gate.
        if start.month == 1:
            continue
        windows.append((start.isoformat(), _add_months(start, span_months).isoformat()))
    return tuple(windows)


# Deliberately offset from v2's January-aligned annual windows.  They are
# static candidates only; actual database admission occurs in the next stage.
_RANGE_WINDOWS = _fixed_offset_windows(span_months=3)
_SERIES_WINDOWS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "day": _fixed_offset_windows(span_months=2),
    "week": _fixed_offset_windows(span_months=6),
    "month": _fixed_offset_windows(span_months=12),
    "quarter": _fixed_offset_windows(span_months=24),
    "year": _fixed_offset_windows(span_months=36),
}


_FAMILY_PLANS: tuple[FamilyPlan, ...] = (
    FamilyPlan(
        "sales_item_scalar_customer_range",
        {"scalar": 21, "dimension_grouped": 24},
        {1: 25, 2: 20},
        {"customer_state": 8, "customer_city": 5, "customer_traffic_source": 11},
    ),
    FamilyPlan(
        "sales_item_dimension_group",
        {"dimension_grouped": 50},
        {1: 30, 2: 20},
        {
            "customer_traffic_source": 12,
            "product_category": 18,
            "product_brand": 7,
            "product_department": 13,
        },
    ),
    FamilyPlan("sales_item_series_pair", {"time_series": 40}, {1: 30, 2: 10}, {}),
    FamilyPlan(
        "completed_order_customer",
        {"scalar": 30, "dimension_grouped": 15},
        {1: 25, 2: 20},
        {"customer_state": 8, "customer_city": 4, "customer_traffic_source": 3},
    ),
    FamilyPlan(
        "order_level_two_stage_average",
        {"scalar": 20, "dimension_grouped": 6, "time_series": 19},
        {1: 25, 2: 20},
        {"customer_state": 3, "customer_city": 1, "customer_traffic_source": 2},
    ),
    FamilyPlan(
        "status_filtered_orders",
        {"scalar": 25, "dimension_grouped": 5, "time_series": 15},
        {1: 35, 2: 10},
        {"customer_state": 2, "customer_traffic_source": 3},
    ),
    FamilyPlan(
        "fulfillment_stage_duration",
        {"scalar": 25, "dimension_grouped": 10, "time_series": 25},
        {1: 25, 2: 10, 3: 25},
        {"customer_state": 5, "customer_traffic_source": 5},
    ),
    FamilyPlan(
        "return_rate_and_delay",
        {"scalar": 20, "dimension_grouped": 9, "time_series": 6},
        {1: 20, 2: 10, 3: 5},
        {"customer_state": 2, "customer_city": 2, "customer_traffic_source": 5},
    ),
    FamilyPlan("orders_series_multi_metric", {"time_series": 20}, {2: 10, 3: 5, 4: 5}, {}),
    FamilyPlan(
        "inventory_received_scalar_series",
        {"scalar": 15, "time_series": 10},
        {1: 25},
        {},
    ),
    FamilyPlan(
        "inventory_received_group_multi",
        {"dimension_grouped": 25, "time_series": 10},
        {1: 35},
        {
            "distribution_center": 12,
            "product_category": 6,
            "product_brand": 2,
            "product_department": 5,
        },
    ),
    FamilyPlan(
        "inventory_sold_scalar_series",
        {"scalar": 27, "time_series": 18},
        {1: 35, 2: 10},
        {},
    ),
    FamilyPlan(
        "inventory_sold_group",
        {"dimension_grouped": 35},
        {1: 25, 2: 10},
        {
            "distribution_center": 22,
            "product_category": 7,
            "product_brand": 3,
            "product_department": 3,
        },
    ),
    FamilyPlan(
        "inventory_sold_multi_metric",
        {"dimension_grouped": 1, "time_series": 19},
        {1: 10, 2: 10},
        {"distribution_center": 1},
    ),
    FamilyPlan(
        "event_session_scalar_series",
        {"scalar": 26, "time_series": 14},
        {1: 30, 2: 10},
        {},
    ),
    FamilyPlan(
        "event_low_cardinality_group",
        {"dimension_grouped": 45},
        {1: 30, 2: 15},
        {"event_type": 15, "event_browser": 15, "event_traffic_source": 15},
    ),
    FamilyPlan(
        "event_series_multi_metric",
        {"dimension_grouped": 9, "time_series": 41},
        {1: 25, 2: 25},
        {"event_type": 3, "event_browser": 3, "event_traffic_source": 3},
    ),
    FamilyPlan(
        "registration_foundation_range",
        {"scalar": 1, "dimension_grouped": 9, "time_series": 10},
        {1: 20},
        {"user_country": 3, "user_traffic_source": 3, "user_state": 3},
    ),
    FamilyPlan(
        "registration_geo_acquisition_group",
        {"dimension_grouped": 25},
        {1: 25},
        {"user_country": 10, "user_traffic_source": 10, "user_state": 5},
    ),
    FamilyPlan(
        "registration_time_series",
        {"dimension_grouped": 17, "time_series": 8},
        {1: 25},
        {"user_country": 6, "user_traffic_source": 6, "user_state": 5},
    ),
)


_METRIC_OPTIONS: Mapping[str, Mapping[int, tuple[tuple[str, ...], ...]]] = {
    "sales_item_scalar_customer_range": {
        1: (("completed_sale_amount",), ("completed_item_count",)),
        2: (("completed_sale_amount", "completed_item_count"),),
    },
    "sales_item_dimension_group": {
        1: (("completed_sale_amount",), ("completed_item_count",)),
        2: (("completed_sale_amount", "completed_item_count"),),
    },
    "sales_item_series_pair": {
        1: (("completed_sale_amount",), ("completed_item_count",)),
        2: (("completed_sale_amount", "completed_item_count"),),
    },
    "completed_order_customer": {
        1: (
            ("completed_order_count",),
            ("completed_customer_count",),
            ("average_order_value",),
        ),
        2: (
            ("completed_order_count", "completed_customer_count"),
            ("completed_customer_count", "average_order_value"),
        ),
    },
    "order_level_two_stage_average": {
        1: (("average_order_value",), ("average_items_per_completed_order",)),
        2: (("average_order_value", "average_items_per_completed_order"),),
    },
    "status_filtered_orders": {
        1: (("cancelled_order_count",), ("returned_order_count",), ("return_rate",)),
        2: (("cancelled_order_count", "completed_order_count"),),
    },
    "fulfillment_stage_duration": {
        1: (
            ("average_fulfillment_days",),
            ("average_dispatch_days",),
            ("average_transit_days",),
            ("average_post_delivery_return_days",),
        ),
        2: (
            ("average_fulfillment_days", "average_dispatch_days"),
            ("average_dispatch_days", "average_transit_days"),
            ("average_transit_days", "average_post_delivery_return_days"),
        ),
        3: (
            (
                "average_fulfillment_days",
                "average_dispatch_days",
                "average_transit_days",
            ),
            (
                "average_fulfillment_days",
                "average_transit_days",
                "average_post_delivery_return_days",
            ),
            (
                "average_dispatch_days",
                "average_transit_days",
                "average_post_delivery_return_days",
            ),
        ),
    },
    "return_rate_and_delay": {
        1: (
            ("returned_order_count",),
            ("return_rate",),
            ("average_post_delivery_return_days",),
        ),
        2: (
            ("returned_order_count", "return_rate"),
            ("return_rate", "average_post_delivery_return_days"),
        ),
        3: (("returned_order_count", "return_rate", "average_post_delivery_return_days"),),
    },
    "orders_series_multi_metric": {
        2: (
            ("completed_order_count", "completed_customer_count"),
            ("average_order_value", "average_items_per_completed_order"),
        ),
        3: (
            (
                "average_fulfillment_days",
                "average_dispatch_days",
                "average_transit_days",
            ),
            ("cancelled_order_count", "returned_order_count", "return_rate"),
        ),
        4: (
            (
                "completed_order_count",
                "completed_customer_count",
                "average_order_value",
                "average_items_per_completed_order",
            ),
        ),
    },
    "inventory_received_scalar_series": {1: (("received_inventory_unit_count",),)},
    "inventory_received_group_multi": {1: (("received_inventory_unit_count",),)},
    "inventory_sold_scalar_series": {
        1: (("sold_inventory_unit_count",), ("average_days_to_sale",)),
        2: (("sold_inventory_unit_count", "average_days_to_sale"),),
    },
    "inventory_sold_group": {
        1: (("sold_inventory_unit_count",), ("average_days_to_sale",)),
        2: (("sold_inventory_unit_count", "average_days_to_sale"),),
    },
    "inventory_sold_multi_metric": {
        1: (("sold_inventory_unit_count",), ("average_days_to_sale",)),
        2: (("sold_inventory_unit_count", "average_days_to_sale"),),
    },
    "event_session_scalar_series": {
        1: (("event_count",), ("unique_session_count",)),
        2: (("event_count", "unique_session_count"),),
    },
    "event_low_cardinality_group": {
        1: (("event_count",), ("unique_session_count",)),
        2: (("event_count", "unique_session_count"),),
    },
    "event_series_multi_metric": {
        1: (("event_count",), ("unique_session_count",)),
        2: (("event_count", "unique_session_count"),),
    },
    "registration_foundation_range": {1: (("registered_user_count",),)},
    "registration_geo_acquisition_group": {1: (("registered_user_count",),)},
    "registration_time_series": {1: (("registered_user_count",),)},
}


# Most families balance their metric options by current case exposure.  These
# two reviewed programs additionally need exact composition to avoid a hidden
# gap in completed-order two-stage aggregation and cancelled-order coverage.
# They remain ordinary frozen v2 metric combinations; this only makes the v3
# coverage allocation explicit and reproducible.
_FIXED_METRIC_SEQUENCES: Mapping[str, Mapping[int, tuple[tuple[str, ...], ...]]] = {
    "completed_order_customer": {
        1: (("average_order_value",),) * 20 + (("completed_order_count",),) * 5,
        2: (("average_order_value", "completed_customer_count"),) * 20,
    },
    "status_filtered_orders": {
        1: (
            (("cancelled_order_count",),) * 25
            + (("returned_order_count",),) * 5
            + (("return_rate",),) * 5
        ),
        2: (("cancelled_order_count", "completed_order_count"),) * 10,
    },
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--v2-cases", type=Path, default=DEFAULT_V2_CASES)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args(argv)


def build_seeds() -> list[dict[str, Any]]:
    """Build exactly the static 750-case v3 seed proposal in stable order."""

    contract = load_thelook_v3_coverage_contract()
    series_grains = _weighted_sequence(contract["case_quotas"]["time_series_grains"])
    series_cursors: Counter[str] = Counter()
    range_cursor = 0
    metric_exposure: Counter[str] = Counter()
    fixed_metric_cursors: Counter[tuple[str, int]] = Counter()
    seen_query_specs: set[str] = set()
    records: list[dict[str, Any]] = []

    for plan in _FAMILY_PLANS:
        shapes = _weighted_sequence(plan.shape_counts)
        metric_counts = _weighted_sequence(plan.metric_count_counts)
        dimensions = _weighted_sequence(plan.dimension_counts)
        if len(shapes) != len(metric_counts):
            raise AssertionError(f"family {plan.family_id} has incompatible shape/multiplicity counts")
        dimension_index = 0
        for shape, metric_count in zip(shapes, metric_counts, strict=True):
            if shape == "dimension_grouped":
                dimension = dimensions[dimension_index]
                dimension_index += 1
            else:
                dimension = None
            if shape == "time_series":
                grain = series_grains.pop(0)
                query_time, window_candidate = _series_time(grain, series_cursors[grain])
                series_cursors[grain] += 1
            else:
                query_time, window_candidate = _range_time(range_cursor)
                range_cursor += 1
            metric_ids = _select_metric_ids(
                family_id=plan.family_id,
                metric_count=metric_count,
                metric_exposure=metric_exposure,
                fixed_metric_cursors=fixed_metric_cursors,
            )
            spec, query_time, window_candidate = _unique_candidate_spec(
                metric_ids=metric_ids,
                result_shape=shape,
                dimension=dimension,
                query_time=query_time,
                window_candidate=window_candidate,
                seen_query_specs=seen_query_specs,
            )
            risk_tags = list(derive_thelook_v3_risk_tags(spec, contract))
            record = {
                "seed_schema_version": THELOOK_V3_COVERAGE_SEED_VERSION,
                "seed_id": f"thelook-v3-coverage-{len(records) + 1:03d}",
                "scenario_family_id": plan.family_id,
                "query_spec": spec.as_dict(),
                "program_signature": derive_thelook_v3_program_signature(spec),
                "risk_tags": risk_tags,
                "window_candidate": window_candidate,
            }
            validated = validate_thelook_v3_coverage_seed(record, contract)
            metric_exposure.update(validated.query_spec.metric_ids)
            seen_query_specs.add(validated.query_spec.query_spec_id)
            records.append(validated.as_dict())
        if dimension_index != len(dimensions):
            raise AssertionError(f"family {plan.family_id} did not consume every grouped dimension")
    for family_id, sequences in _FIXED_METRIC_SEQUENCES.items():
        for metric_count, sequence in sequences.items():
            if fixed_metric_cursors[family_id, metric_count] != len(sequence):
                raise AssertionError(f"family {family_id} did not consume its fixed metric sequence")
    if series_grains:
        raise AssertionError("time-series grain quota was not fully consumed")
    return records


def audit_seed_records(
    records: Iterable[Mapping[str, Any]],
    *,
    v2_cases: Path | None = None,
) -> dict[str, Any]:
    """Audit static seed identity and exact coverage quotas without SQL execution."""

    contract = load_thelook_v3_coverage_contract()
    seeds = [validate_thelook_v3_coverage_seed(record, contract) for record in records]
    expected = contract["release"]["target_cases"]
    if len(seeds) != expected:
        raise TheLookV3CoverageError(f"expected {expected} v3 seeds, found {len(seeds)}")
    seed_ids = [seed.seed_id for seed in seeds]
    query_spec_ids = [seed.query_spec.query_spec_id for seed in seeds]
    if len(set(seed_ids)) != len(seeds) or len(set(query_spec_ids)) != len(seeds):
        raise TheLookV3CoverageError("v3 seed and QuerySpec IDs must both be unique")

    fact_counts = Counter(METRIC_FACT_DOMAINS[seed.query_spec.metric_ids[0]] for seed in seeds)
    shape_counts = Counter(seed.query_spec.result_shape for seed in seeds)
    grain_counts = Counter(
        seed.query_spec.time.grain
        for seed in seeds
        if seed.query_spec.result_shape == "time_series"
    )
    dimension_counts = Counter(
        seed.query_spec.dimension
        for seed in seeds
        if seed.query_spec.result_shape == "dimension_grouped"
    )
    multiplicity_counts = Counter(len(seed.query_spec.metric_ids) for seed in seeds)
    family_counts = Counter(seed.scenario_family_id for seed in seeds)
    risk_counts = Counter(tag for seed in seeds for tag in seed.risk_tags)
    metric_counts = Counter(metric for seed in seeds for metric in seed.query_spec.metric_ids)

    _require_exact_counts(fact_counts, contract["case_quotas"]["fact_domains"], "fact domains")
    _require_exact_counts(shape_counts, contract["case_quotas"]["result_shapes"], "result shapes")
    _require_exact_counts(grain_counts, contract["case_quotas"]["time_series_grains"], "time grains")
    _require_exact_counts(
        dimension_counts, contract["case_quotas"]["dimension_grouped"], "dimensions"
    )
    _require_exact_counts(
        multiplicity_counts,
        {
            1: contract["case_quotas"]["metric_multiplicity"]["one_metric"],
            2: contract["case_quotas"]["metric_multiplicity"]["two_metrics"],
            3: contract["case_quotas"]["metric_multiplicity"]["three_metrics"],
            4: contract["case_quotas"]["metric_multiplicity"]["four_metrics"],
        },
        "metric multiplicity",
    )
    _require_exact_counts(
        family_counts,
        {
            family["id"]: family["target_cases"]
            for family in contract["scenario_families"]
        },
        "scenario families",
    )
    for tag, minimum in contract["risk_tag_minimum_case_counts"].items():
        if risk_counts[tag] < minimum:
            raise TheLookV3CoverageError(
                f"risk tag {tag} needs at least {minimum} cases, found {risk_counts[tag]}"
            )
    exposure_bounds = contract["case_quotas"]["metric_multiplicity"][
        "case_exposure_by_fact_domain"
    ]
    for metric in contract["semantic_baseline"]["allowed_metric_ids"]:
        count = metric_counts[metric]
        domain = METRIC_FACT_DOMAINS[metric]
        bounds = exposure_bounds[domain]
        if not bounds["minimum"] <= count <= bounds["maximum"]:
            raise TheLookV3CoverageError(
                f"metric {metric} exposure {count} is outside the frozen {domain} band"
            )

    v2_overlap = 0
    if v2_cases is not None:
        v2_ids = _read_v2_query_spec_ids(v2_cases)
        v2_overlap = len(set(query_spec_ids) & v2_ids)
        if v2_overlap:
            raise TheLookV3CoverageError(f"v3 seeds overlap {v2_overlap} protected v2 QuerySpecs")
    return {
        "seed_count": len(seeds),
        "fact_domains": dict(sorted(fact_counts.items())),
        "result_shapes": dict(sorted(shape_counts.items())),
        "time_series_grains": dict(sorted(grain_counts.items())),
        "dimensions": dict(sorted(dimension_counts.items())),
        "metric_multiplicity": dict(sorted(multiplicity_counts.items())),
        "metric_exposure": dict(sorted(metric_counts.items())),
        "scenario_families": dict(sorted(family_counts.items())),
        "risk_tags": dict(sorted(risk_counts.items())),
        "v2_query_spec_overlap": v2_overlap,
        "sql_rendered": False,
        "database_accessed": False,
        "model_called": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    records = build_seeds()
    audit = audit_seed_records(records, v2_cases=args.v2_cases)
    print(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2))
    if not args.check_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
    return 0


def _weighted_sequence(counts: Mapping[Any, int]) -> list[Any]:
    """Interleave exact quota counts in stable key order rather than block them."""

    remaining = {key: int(value) for key, value in counts.items()}
    result: list[Any] = []
    while any(remaining.values()):
        for key in sorted(remaining, key=str):
            if remaining[key] > 0:
                result.append(key)
                remaining[key] -= 1
    return result


def _least_exposed_metric_option(
    options: Sequence[tuple[str, ...]], metric_exposure: Counter[str]
) -> tuple[str, ...]:
    return min(options, key=lambda metrics: (sum(metric_exposure[metric] for metric in metrics), metrics))


def _select_metric_ids(
    *,
    family_id: str,
    metric_count: int,
    metric_exposure: Counter[str],
    fixed_metric_cursors: Counter[tuple[str, int]],
) -> tuple[str, ...]:
    fixed_sequence = _FIXED_METRIC_SEQUENCES.get(family_id, {}).get(metric_count)
    if fixed_sequence is not None:
        cursor_key = (family_id, metric_count)
        cursor = fixed_metric_cursors[cursor_key]
        if cursor >= len(fixed_sequence):
            raise AssertionError(f"family {family_id} exhausted its fixed metric sequence")
        fixed_metric_cursors[cursor_key] += 1
        return fixed_sequence[cursor]
    return _least_exposed_metric_option(_METRIC_OPTIONS[family_id][metric_count], metric_exposure)


def _range_time(cursor: int) -> tuple[TheLookQueryTime, str]:
    start, end = _RANGE_WINDOWS[cursor % len(_RANGE_WINDOWS)]
    return TheLookQueryTime("absolute_range", start, end), f"range:{start}:{end}"


def _series_time(grain: str, cursor: int) -> tuple[TheLookQueryTime, str]:
    start, end = _SERIES_WINDOWS[grain][cursor % len(_SERIES_WINDOWS[grain])]
    return TheLookQueryTime("series", start, end, grain), f"series:{grain}:{start}:{end}"


def _unique_candidate_spec(
    *,
    metric_ids: tuple[str, ...],
    result_shape: str,
    dimension: str | None,
    query_time: TheLookQueryTime,
    window_candidate: str,
    seen_query_specs: set[str],
) -> tuple[TheLookV2QuerySpec, TheLookQueryTime, str]:
    """Construct a unique candidate; the caller immediately validates its record.

    ``TheLookV2QuerySpec.create`` derives its canonical identifier without I/O.
    Calling ``create_validated`` for every candidate would re-parse the same
    Catalog YAML for every collision probe.  The following
    ``validate_thelook_v3_coverage_seed`` call is the single authoritative
    semantic validation step before the candidate can enter ``records``.
    """

    candidates: list[tuple[TheLookQueryTime, str]] = [(query_time, window_candidate)]
    if query_time.mode == "series":
        assert query_time.grain is not None
        candidates.extend(
            _series_time(query_time.grain, index)
            for index in range(len(_SERIES_WINDOWS[query_time.grain]))
        )
    else:
        candidates.extend(_range_time(index) for index in range(len(_RANGE_WINDOWS)))
    for candidate_time, candidate_window in candidates:
        spec = TheLookV2QuerySpec.create(
            metric_ids=metric_ids,
            result_shape=result_shape,
            dimension=dimension,
            time=candidate_time,
        )
        if spec.query_spec_id not in seen_query_specs:
            return spec, candidate_time, candidate_window
    raise TheLookV3CoverageError("unable to find a unique QuerySpec in the frozen window pool")


def _read_v2_query_spec_ids(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TheLookV3CoverageError("protected v2 cases are unavailable for overlap audit") from exc
    ids: set[str] = set()
    for line in lines:
        row = json.loads(line)
        query_spec = row.get("query_spec")
        query_spec_id = query_spec.get("query_spec_id") if isinstance(query_spec, Mapping) else None
        if not isinstance(query_spec_id, str):
            raise TheLookV3CoverageError("protected v2 case lacks a QuerySpec ID")
        ids.add(query_spec_id)
    return ids


def _require_exact_counts(
    actual: Mapping[Any, int], expected: Mapping[Any, int], label: str
) -> None:
    normalized_actual = {key: int(value) for key, value in actual.items()}
    normalized_expected = {key: int(value) for key, value in expected.items() if int(value) > 0}
    if normalized_actual != normalized_expected:
        raise TheLookV3CoverageError(
            f"{label} do not match contract: actual={normalized_actual}, expected={normalized_expected}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
