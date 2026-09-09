#!/usr/bin/env python3
"""Build a 20-family, five-surface-form Olist review pilot.

Only structural QuerySpec rows are read from the external Medium release. Gold
SQL is rendered later by the existing materializer/admission gates. The output
is intentionally external and is not a train/validation/test release.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime, validate_query_spec  # noqa: E402
from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from scripts.post_training.data.materialize_olist_queryspecs import family_id  # noqa: E402


VERSION = "olist-surface-form-pilot-v1"
VARIANT_SCHEMA_VERSION = "3"
TARGET_FAMILIES = 20
VARIANTS_PER_FAMILY = 5
METRIC_LABELS = {
    "gmv": "成交额",
    "paid_order_count": "有效订单数",
    "average_delivery_days": "平均履约天数",
    "positive_review_rate": "好评率",
    "item_count": "商品件数",
    "average_order_value": "客单价",
    "average_review_score": "平均评价分",
    "on_time_delivery_rate": "准时送达率",
    "cancellation_rate": "取消率",
    "freight_amount": "运费金额",
}
DIMENSION_LABELS = {
    "customer_state": "客户州",
    "customer_city": "客户城市",
    "product_category_name": "商品品类",
}
GRAIN_LABELS = {"day": "按日", "week": "按周", "month": "按月", "quarter": "按季度", "year": "按年"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row is not an object: {path}")
            rows.append(value)
    return rows


def _family_fingerprint(family: str) -> str:
    return hashlib.sha256(f"olist-protected-family-summary-v1:{family}".encode()).hexdigest()


def _load_protected(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("summary_version") != "olist-protected-family-summary-v1":
        raise ValueError("unsupported protected summary")
    values = payload.get("family_fingerprints")
    if not isinstance(values, list) or any(not isinstance(v, str) or len(v) != 64 for v in values):
        raise ValueError("invalid protected family fingerprints")
    return set(values)


def _spec_from_row(row: dict[str, Any]) -> QuerySpec:
    raw = row.get("query_spec")
    if isinstance(raw, dict):
        return QuerySpec.from_mapping(raw)
    return QuerySpec.create(
        metric_ids=tuple(row["metric_ids"]),
        result_shape=row["result_shape"],
        dimension=row.get("dimension"),
        time=QueryTime(**row["time"]),
        join_program_id=row.get("join_program_id"),
    )


def _candidate_key(spec: QuerySpec) -> tuple[str, str, str, str]:
    return (
        spec.result_shape,
        spec.join_program_id,
        "+".join(spec.metric_ids),
        spec.query_spec_id,
    )


def select_specs(source_rows: list[dict[str, Any]], existing_families: set[str], protected: set[str]) -> list[QuerySpec]:
    catalog = CatalogLoader().load()
    candidates: dict[str, QuerySpec] = {}
    for row in source_rows:
        spec = validate_query_spec(_spec_from_row(row), catalog)
        family = family_id(spec)
        if family not in existing_families and _family_fingerprint(family) not in protected:
            candidates.setdefault(family, spec)
    all_candidates = sorted(candidates.values(), key=_candidate_key)
    selected: list[QuerySpec] = []
    selected_families: set[str] = set()

    def choose(label: str, predicate, rank) -> None:
        eligible = [spec for spec in all_candidates if family_id(spec) not in selected_families and predicate(spec)]
        if not eligible:
            raise ValueError(f"no candidate satisfies pilot coverage requirement: {label}")
        selected_spec = min(eligible, key=lambda spec: (rank(spec), _candidate_key(spec)))
        selected.append(selected_spec)
        selected_families.add(family_id(selected_spec))

    # First guarantee that every frozen business metric appears at least once.
    # A single-metric scalar is preferred, then any legal shape. This prevents
    # a deterministic sort from silently over-sampling a convenient metric.
    for metric in METRIC_LABELS:
        choose(
            f"metric:{metric}",
            lambda spec, metric=metric: metric in spec.metric_ids,
            lambda spec, metric=metric: (
                len(spec.metric_ids) != 1,
                spec.result_shape != "scalar",
                spec.time.mode != "all_time",
                spec.metric_ids.index(metric),
            ),
        )
    # Category grouping is important schema-linking coverage. If all legal
    # category families already exist in Medium v1, record that limitation and
    # continue with a new family instead of duplicating an old one.
    category_candidates = [spec for spec in all_candidates if spec.result_shape == "category_grouped"]
    if category_candidates:
        choose("category_grouped", lambda spec: spec.result_shape == "category_grouped", lambda spec: (len(spec.metric_ids),))
    # Cover every frozen purchase time grain, including the fields that exposed
    # time-bucketing mistakes in the cross-schema evaluation.
    for grain in ("day", "week", "month", "quarter", "year"):
        choose(
            f"time_grain:{grain}",
            lambda spec, grain=grain: spec.result_shape == "time_series" and spec.time.grain == grain,
            lambda spec: ("average_order_value" not in spec.metric_ids, len(spec.metric_ids)),
        )
    # Reserve every remaining slot for difficult multi-metric contracts:
    # independent CTEs, order de-duplication and AOV's two-stage aggregation.
    # A new category family is not currently available because every legal
    # category grouping is already in Medium v1.  Keep this selection generic:
    # if one becomes available later, it consumes one of the multi-metric
    # reserve slots rather than making the 20-family pilot silently grow to 21.
    multi_metric_shapes = ("scalar", "state_grouped", "time_series", "state_grouped", "scalar")
    slots = TARGET_FAMILIES - len(selected)
    if slots < 0:
        raise AssertionError("required coverage exceeded the pilot size")
    for shape in itertools.islice(itertools.cycle(multi_metric_shapes), slots):
        choose(
            f"multi_metric:{shape}",
            lambda spec, shape=shape: shape == spec.result_shape and len(spec.metric_ids) >= 2,
            lambda spec: (
                "average_order_value" not in spec.metric_ids,
                "paid_order_count" not in spec.metric_ids,
                len(spec.metric_ids),
            ),
        )
    if len(selected) != TARGET_FAMILIES:
        raise ValueError(f"only selected {len(selected)} new families; expected {TARGET_FAMILIES}")
    if len({family_id(spec) for spec in selected}) != TARGET_FAMILIES:
        raise AssertionError("pilot families are not unique")
    return selected


def _period(spec: QuerySpec) -> str:
    if spec.time.mode == "all_time":
        return "全部可用数据范围"
    if spec.time.mode == "series":
        return f"{spec.time.start}至{spec.time.end_exclusive}"
    return f"{spec.time.start}至{spec.time.end_exclusive}"


def _metrics(spec: QuerySpec) -> str:
    return "、".join(METRIC_LABELS[m] for m in spec.metric_ids)


def _dimension(spec: QuerySpec) -> str:
    return DIMENSION_LABELS.get(spec.dimension or "", "")


def render_variants(spec: QuerySpec, family: str, seed_id: str) -> list[dict[str, str]]:
    metrics = _metrics(spec)
    period = _period(spec)
    dimension = _dimension(spec)
    if spec.time.mode == "series":
        prefix = GRAIN_LABELS[spec.time.grain]
        formal = f"请{prefix}统计{period}的{metrics}。"
        oral = f"看一下{period}{prefix}的{metrics}。"
        manager = f"我想了解{period}{prefix}的{metrics}情况。"
        compact = f"{period}{prefix}{metrics}。"
        result = f"请按时间列出{period}{prefix}的{metrics}，用于经营分析。"
    elif spec.result_shape in {"state_grouped", "category_grouped"}:
        formal = f"请统计{period}各{dimension}的{metrics}。"
        oral = f"看一下{period}不同{dimension}的{metrics}。"
        manager = f"我想了解{period}各{dimension}的{metrics}表现。"
        compact = f"{period}各{dimension}{metrics}。"
        result = f"请按{dimension}列出{period}的{metrics}，用于经营分析。"
    else:
        formal = f"请统计{period}的{metrics}。"
        oral = f"看一下{period}的{metrics}。"
        manager = f"我想了解{period}整体的{metrics}情况。"
        compact = f"{period}{metrics}。"
        result = f"请给出{period}的{metrics}，结果用于经营概览。"
    values = [formal, oral, manager, compact, result]
    if len(set(values)) != VARIANTS_PER_FAMILY:
        raise AssertionError(f"duplicate surface form for {family}")
    return [
        {"variant_id": f"{seed_id}-v{i}", "seed_id": seed_id, "question": question}
        for i, question in enumerate(values, 1)
    ]


def build(source_queryspecs: Path, existing_queryspecs: Path, protected_summary: Path, output_dir: Path) -> dict[str, Any]:
    for path in (source_queryspecs, existing_queryspecs, protected_summary):
        if path.resolve().is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f"external input missing or inside Git worktree: {path}")
    if output_dir.resolve().is_relative_to(ROOT) or output_dir.exists():
        raise ValueError("pilot output must be a new directory outside the Git worktree")
    source_rows = _read_jsonl(source_queryspecs)
    existing_rows = _read_jsonl(existing_queryspecs)
    existing_families = {str(row.get("family_id")) for row in existing_rows}
    protected = _load_protected(protected_summary)
    specs = select_specs(source_rows, existing_families, protected)
    seeds = []
    variants = []
    for index, spec in enumerate(specs, 1):
        family = family_id(spec)
        seed_id = f"olist-surface-form-pilot-v1-{index:02d}"
        seeds.append({
            "seed_id": seed_id,
            "split": "review_pilot",
            "metric_ids": list(spec.metric_ids),
            "result_shape": spec.result_shape,
            "dimension": spec.dimension,
            "time": spec.time.as_dict(),
            "join_program_id": spec.join_program_id,
            "family_id": family,
            "query_spec": spec.as_dict(),
        })
        variants.extend(render_variants(spec, family, seed_id))
    output_dir.mkdir(parents=True)
    (output_dir / "structural_seeds.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in seeds), encoding="utf-8")
    materializer_seeds = [
        {key: row[key] for key in ("seed_id", "split", "metric_ids", "result_shape", "dimension", "time", "join_program_id")}
        for row in seeds
    ]
    (output_dir / "materializer_seeds.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in materializer_seeds), encoding="utf-8")
    (output_dir / "question_variants.json").write_text(json.dumps({
        "schema_version": VARIANT_SCHEMA_VERSION,
        "language": "zh",
        "prompt_version": "olist-candidate-sql-v1",
        "variant_policy": "five controlled surface forms; QuerySpec identity must remain unchanged",
        "cases": variants,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": VERSION,
        "source": {
            "queryspec_jsonl": str(source_queryspecs.resolve()),
            "queryspec_sha256": _sha256(source_queryspecs),
            "existing_queryspec_jsonl": str(existing_queryspecs.resolve()),
            "existing_queryspec_sha256": _sha256(existing_queryspecs),
            "protected_summary_sha256": _sha256(protected_summary),
        },
        "counts": {
            "families": len(seeds),
            "variants": len(variants),
            "variants_per_family": VARIANTS_PER_FAMILY,
            "shape_distribution": dict(Counter(row["result_shape"] for row in seeds)),
            "program_distribution": dict(Counter(row["join_program_id"] for row in seeds)),
        },
        "checks": {
            "new_family_disjoint_from_medium_v1": True,
            "protected_summary_checked": True,
            "category_grouped_new_family_available": any(row["result_shape"] == "category_grouped" for row in seeds),
            "gold_rendered": False,
            "sql_policy_reader_result_contract": False,
            "runtime_prompt_rebuilt": False,
            "model_called": False,
        },
    }
    (output_dir / "pilot_selection_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-queryspecs", type=Path, required=True)
    parser.add_argument("--existing-queryspecs", type=Path, required=True)
    parser.add_argument("--protected-summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source_queryspecs, args.existing_queryspecs, args.protected_summary, args.output_dir), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
