#!/usr/bin/env python3
"""Build TheLook SQL-only SFT train/validation seeds.

The builder emits only static QuerySpec-backed seeds.  It deliberately does
not render SQL, connect to PostgreSQL, call an LLM, or read protected cases as
generation input.  The existing v3 family blueprint is used as a reviewed
coverage reference, while this command owns a separate SFT contract and uses
non-protected date-window candidates.
"""

from __future__ import annotations

import argparse
import calendar
from collections import Counter
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from data_analysis_agent.thelook_queryspec import TheLookQueryTime  # noqa: E402
from data_analysis_agent.thelook_sft_coverage import (  # noqa: E402
    THELOOK_SFT_SEED_VERSION,
    audit_thelook_sft_seeds,
    load_thelook_sft_contract,
    validate_thelook_sft_seed,
)
from data_analysis_agent.thelook_v2_queryspec import TheLookV2QuerySpec  # noqa: E402
from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_v3_coverage import (  # noqa: E402
    derive_thelook_v3_program_signature,
    derive_thelook_v3_risk_tags,
)

# This is a reviewed family/metric blueprint, not an input dataset.  Importing
# these static constants prevents a second, silently divergent copy of the
# coverage plan.  The protected v3 builder is never called and no protected
# cases are read here.
sys.path.insert(0, str(ROOT / "scripts" / "post_training" / "evaluation"))
from build_thelook_v3_coverage_seeds import (  # noqa: E402
    FamilyPlan,
    _FIXED_METRIC_SEQUENCES,
    _METRIC_OPTIONS,
    _FAMILY_PLANS as _REVIEWED_FAMILY_PLANS,
    _select_metric_ids,
    _weighted_sequence,
)


DEFAULT_OUTPUT = Path("/disk2/gengnan/data-analysis-agent-data/evals/thelook-sql-only-sft-v1-r4/seeds.jsonl")
DEFAULT_PROTECTED = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "thelook-cross-schema-final-test-v3-20260915-r3/cases.jsonl"
)


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _window_pool(count: int = 240) -> tuple[tuple[str, str], ...]:
    # The protected releases use first-of-month and 15th-of-month starts.  SFT
    # uses only the reviewed 17th-21st starts of each month, so the date family
    # is disjoint by construction while still providing five variants per
    # program across the frozen snapshot period.
    candidate = date(2019, 2, 1)
    windows: list[tuple[str, str]] = []
    while len(windows) < count:
        for day in (17, 18, 19, 20, 21):
            start = candidate.replace(day=day)
            windows.append((start.isoformat(), (start + timedelta(days=7)).isoformat()))
            if len(windows) >= count:
                break
        candidate = _add_months(candidate, 1).replace(day=1)
    return tuple(windows)


def _series_window_pool(grain: str, count: int = 240) -> tuple[tuple[str, str], ...]:
    first = date(2019, 2, 1)
    spans = {"day": 2, "week": 6, "month": 12, "quarter": 24, "year": 36}
    span = spans[grain]
    windows: list[tuple[str, str]] = []
    candidate = first
    while len(windows) < count:
        for day in (17, 18, 19, 20, 21):
            start = candidate.replace(day=day)
            windows.append((start.isoformat(), _add_months(start, span).isoformat()))
            if len(windows) >= count:
                break
        candidate = _add_months(candidate, 1).replace(day=1)
    return tuple(windows)


def _family_plans() -> tuple[FamilyPlan, ...]:
    plans: list[FamilyPlan] = []
    for plan in _REVIEWED_FAMILY_PLANS:
        if plan.family_id == "inventory_unsold_snapshot":
            continue
        if plan.family_id == "registration_time_series":
            # The protected release had 21 programs in this family.  SFT v1
            # adds four ordinary user-registration programs, never snapshot
            # rows, to reconcile the 750-program training contract.
            plans.append(
                FamilyPlan(
                    plan.family_id,
                    {"dimension_grouped": 18, "time_series": 7},
                    {1: 25},
                    {"user_country": 7, "user_traffic_source": 6, "user_state": 5},
                )
            )
        else:
            plans.append(plan)
    return tuple(plans)


def _window_for(
    *, result_shape: str, grain: str | None, dimension: str | None, cursor: int, variant: int
) -> tuple[TheLookQueryTime, str]:
    # A short fixed range keeps high-cardinality results displayable without
    # using a runtime failure to choose a new date.
    index = cursor * 5 + variant
    if result_shape == "time_series":
        start, end = _series_window_pool(grain or "month")[index % 240]
        return TheLookQueryTime("series", start, end, grain), f"sft-series:{grain}:{start}:{end}"
    if dimension in {"customer_city", "product_brand", "user_state"}:
        start, end = _window_pool()[index % 240]
        return TheLookQueryTime("absolute_range", start, end), f"sft-high-cardinality:{start}:{end}"
    start, _ = _window_pool()[index % 240]
    # Use a three-month interval for ordinary range queries.  The start pool is
    # fixed; no later admission failure may replace it with another date.
    end = _add_months(date.fromisoformat(start), 3).isoformat()
    return TheLookQueryTime("absolute_range", start, end), f"sft-range:{start}:{end}"


def _protected_time_pairs(path: Path) -> set[tuple[str, str, str, str | None]]:
    """Read only protected identity keys for collision avoidance.

    Protected cases are never used as semantic/family/metric generation input;
    this narrow read prevents an accidental QuerySpec collision with holdout.
    """
    pairs: set[tuple[str, str, str, str | None]] = set()
    if not path.exists():
        return pairs
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        spec = row.get("query_spec")
        if not isinstance(spec, Mapping):
            continue
        t = spec.get("time")
        if isinstance(t, Mapping):
            pairs.add((str(spec.get("result_shape")), str(spec.get("dimension")), str(t.get("start")), t.get("end_exclusive")))
    return pairs


def build_seed_records(protected_cases: Path | None = None) -> list[dict[str, Any]]:
    contract = load_thelook_sft_contract()
    plans = _family_plans()
    if sum(sum(plan.shape_counts.values()) for plan in plans) != int(contract["targets"]["programs"]):
        raise AssertionError("family blueprint does not reconcile to SFT program target")
    split_boundary = int(contract["targets"]["train_programs"])
    metric_exposure: Counter[str] = Counter()
    fixed_cursors: Counter[tuple[str, int]] = Counter()
    records: list[dict[str, Any]] = []
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    protected_pairs = _protected_time_pairs(protected_cases) if protected_cases else set()
    seen_query_specs: set[str] = set()
    program_cursor = 0
    for plan in plans:
        shapes = _weighted_sequence(plan.shape_counts)
        metric_counts = _weighted_sequence(plan.metric_count_counts)
        dimensions = _weighted_sequence(plan.dimension_counts)
        if len(shapes) != len(metric_counts):
            raise AssertionError(f"shape/multiplicity mismatch in {plan.family_id}")
        dimension_index = 0
        for shape, metric_count in zip(shapes, metric_counts, strict=True):
            dimension = dimensions[dimension_index] if shape == "dimension_grouped" else None
            if shape == "dimension_grouped":
                dimension_index += 1
            metrics = _select_metric_ids(
                family_id=plan.family_id,
                metric_count=metric_count,
                metric_exposure=metric_exposure,
                fixed_metric_cursors=fixed_cursors,
            )
            split = "train" if program_cursor < split_boundary else "validation"
            program_id = f"thelook-sft-program-{program_cursor + 1:04d}"
            used_program_windows: set[str] = set()
            for variant in range(int(contract["targets"]["samples_per_program"])):
                # The semantic program is held constant while the fixed date
                # window changes.  All five variants stay in one split.
                grain = None
                if shape == "time_series":
                    grain = _weighted_grain(program_cursor, contract)
                # Probe only the predeclared static window pool until both the
                # canonical QuerySpec and window are new.  This is identity
                # collision handling, not retrying a database failure.
                probe = 0
                while True:
                    query_time, window = _window_for(
                        result_shape=shape,
                        grain=grain,
                        dimension=dimension,
                        cursor=program_cursor,
                        variant=variant + probe,
                    )
                    spec = TheLookV2QuerySpec.create(
                        metric_ids=metrics,
                        result_shape=shape,
                        dimension=dimension,
                        time=query_time,
                    )
                    time_key = (shape, str(dimension), query_time.start, query_time.end_exclusive)
                    if (
                        spec.query_spec_id not in seen_query_specs
                        and window not in used_program_windows
                        and time_key not in protected_pairs
                    ):
                        break
                    probe += 1
                    if probe >= 240:
                        raise RuntimeError("static TheLook SFT window pool exhausted")
                record = {
                    "seed_schema_version": THELOOK_SFT_SEED_VERSION,
                    "seed_id": f"thelook-sft-{split}-{len(records) + 1:04d}",
                    "split": split,
                    "program_id": program_id,
                    "variant_index": variant,
                    "scenario_family_id": plan.family_id,
                    "query_spec": spec.as_dict(),
                    "program_signature": derive_thelook_v3_program_signature(spec),
                    "risk_tags": list(derive_thelook_v3_risk_tags(spec)),
                    "window_candidate": window,
                }
                validated = validate_thelook_sft_seed(record, contract, catalog)
                metric_exposure.update(validated.query_spec.metric_ids)
                seen_query_specs.add(validated.query_spec.query_spec_id)
                used_program_windows.add(window)
                records.append(validated.as_dict())
            program_cursor += 1
        if dimension_index != len(dimensions):
            raise AssertionError(f"unused dimensions in {plan.family_id}")
    for family_id, sequence in _FIXED_METRIC_SEQUENCES.items():
        # Fixed sequences are only used by families retained in this builder.
        for metric_count, values in sequence.items():
            if family_id == "inventory_unsold_snapshot":
                continue
            if fixed_cursors[family_id, metric_count] != len(values):
                raise AssertionError(f"fixed metric sequence not consumed: {family_id}/{metric_count}")
    return records


def _weighted_grain(index: int, contract: Mapping[str, Any]) -> str:
    grains = tuple(contract["semantic_rules"]["allowed_time_series_grains"])
    return grains[index % len(grains)]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--protected-cases", type=Path, default=DEFAULT_PROTECTED)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    records = build_seed_records(args.protected_cases)
    audit = audit_thelook_sft_seeds(records, protected_cases=args.protected_cases)
    print(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2))
    if not args.check_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
        print(json.dumps({"output": str(args.output), "sha256": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
