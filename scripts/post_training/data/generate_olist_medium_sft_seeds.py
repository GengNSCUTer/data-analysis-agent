#!/usr/bin/env python3
"""Generate a deterministic medium-scale Olist SFT seed release.

The generator enumerates only QuerySpec shapes already accepted by the frozen
ten-metric contract. It writes structural seeds outside Git; Gold SQL, runtime
prompts, execution evidence, and SFT JSONL are separate later stages.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime, validate_query_spec  # noqa: E402
from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402
from scripts.post_training.data.materialize_olist_queryspecs import family_id  # noqa: E402


TARGET_SPLITS = {"train": 720, "validation": 240, "in_domain_test": 240}
METRICS = (
    "gmv",
    "paid_order_count",
    "average_delivery_days",
    "positive_review_rate",
    "item_count",
    "average_order_value",
    "average_review_score",
    "on_time_delivery_rate",
    "cancellation_rate",
    "freight_amount",
)
PURCHASE_METRICS = tuple(metric for metric in METRICS if metric not in {"positive_review_rate", "average_review_score"})
REVIEW_METRICS = ("positive_review_rate", "average_review_score")
ITEM_METRICS = ("gmv", "item_count", "freight_amount")
DATE_WINDOWS = (
    ("2016-10-01", "2017-01-01"),
    ("2017-01-01", "2017-04-01"),
    ("2017-04-01", "2017-07-01"),
    ("2017-07-01", "2017-10-01"),
    ("2017-10-01", "2018-01-01"),
    ("2018-01-01", "2018-04-01"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument(
        "--protected-summary-json",
        type=Path,
        default=None,
        help="External protected-family hash summary. Required for a publishable release.",
    )
    return parser.parse_args()


def _stable_key(item: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _combinations(metrics: tuple[str, ...]) -> Iterable[tuple[str, ...]]:
    for size in range(1, 5):
        yield from itertools.combinations(metrics, size)


def _seed(
    catalog: Catalog,
    metrics: tuple[str, ...],
    shape: str,
    time: QueryTime,
    dimension: str | None = None,
) -> dict[str, Any]:
    spec = validate_query_spec(QuerySpec.create(
        metric_ids=metrics,
        result_shape=shape,
        dimension=dimension,
        time=time,
    ), catalog)
    return {
        "metric_ids": list(metrics),
        "result_shape": shape,
        "dimension": dimension,
        "time": time.as_dict(),
        "join_program_id": spec.join_program_id,
        "family_id": family_id(spec),
    }


def _protected_fingerprint(family: str) -> str:
    return hashlib.sha256(
        f"olist-protected-family-summary-v1:{family}".encode("utf-8")
    ).hexdigest()


def load_protected_fingerprints(path: Path | None) -> frozenset[str]:
    """Read only protected family hashes, never protected QuerySpec content."""
    if path is None:
        return frozenset()
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("protected summary must stay outside the Git worktree")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    fingerprints = payload.get("family_fingerprints") if isinstance(payload, dict) else None
    if (
        payload.get("summary_version") != "olist-protected-family-summary-v1"
        or not isinstance(fingerprints, list)
        or any(not isinstance(value, str) or len(value) != 64 for value in fingerprints)
    ):
        raise ValueError("protected summary does not satisfy the v1 hash-only contract")
    return frozenset(fingerprints)


def _take_unique(
    rows: Iterable[dict[str, Any]],
    count: int,
    protected_fingerprints: frozenset[str],
    *,
    allow_short: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sorted(rows, key=_stable_key):
        if (
            row["family_id"] in seen
            or _protected_fingerprint(row["family_id"]) in protected_fingerprints
        ):
            continue
        seen.add(row["family_id"])
        selected.append(row)
        if len(selected) == count:
            return selected
    if allow_short:
        return selected
    raise ValueError(f"only generated {len(selected)} distinct families; expected {count}")


def generate_rows(protected_fingerprints: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    catalog = CatalogLoader().load()
    scalar = [
        _seed(catalog, metrics, "scalar", time)
        for metrics in _combinations(METRICS)
        for time in (
            QueryTime("all_time"),
            QueryTime("absolute_range", *DATE_WINDOWS[len(metrics) % len(DATE_WINDOWS)]),
        )
    ]
    state = [
        _seed(catalog, metrics, "state_grouped", time, "customer_state")
        for metrics in _combinations(METRICS)
        for time in (
            QueryTime("all_time"),
            QueryTime("absolute_range", *DATE_WINDOWS[(len(metrics) + 2) % len(DATE_WINDOWS)]),
        )
    ]
    purchase_series = [
        _seed(catalog, metrics, "time_series", QueryTime("series", *DATE_WINDOWS[(len(metrics) + len(grain)) % len(DATE_WINDOWS)], grain))
        for metrics in _combinations(PURCHASE_METRICS)
        for grain in ("day", "month", "quarter", "year", "week")
    ]
    review_series = [
        _seed(catalog, metrics, "time_series", QueryTime("series", *DATE_WINDOWS[(len(metrics) + len(grain)) % len(DATE_WINDOWS)], grain))
        for metrics in _combinations(REVIEW_METRICS)
        for grain in ("day", "month", "quarter", "year", "week")
    ]
    category = [
        _seed(catalog, (metric,), "category_grouped", time, "product_category_name")
        for metric in ITEM_METRICS
        for time in (QueryTime("all_time"), QueryTime("absolute_range", *DATE_WINDOWS[1]))
    ]
    # Review/category coverage has a small finite number of legal v1 families.
    # A protected family can therefore make a desired sub-bucket one row short.
    # Keep the surviving coverage, then fill only from unused legal scalar,
    # state, or purchase-series families; never reintroduce a protected family.
    rows = (
        _take_unique(scalar, 400, protected_fingerprints, allow_short=True)
        + _take_unique(state, 400, protected_fingerprints, allow_short=True)
        + _take_unique(purchase_series, 379, protected_fingerprints, allow_short=True)
        + _take_unique(review_series, 15, protected_fingerprints, allow_short=True)
        + _take_unique(category, 6, protected_fingerprints, allow_short=True)
    )
    deficit = sum(TARGET_SPLITS.values()) - len(rows)
    if deficit > 0:
        selected_families = {row["family_id"] for row in rows}
        fallback = _take_unique(
            (
                row
                for row in itertools.chain(scalar, state, purchase_series)
                if row["family_id"] not in selected_families
            ),
            deficit,
            protected_fingerprints,
        )
        rows += fallback
    if len(rows) != sum(TARGET_SPLITS.values()) or len({row["family_id"] for row in rows}) != len(rows):
        raise AssertionError("medium release must contain 1,200 unique semantic families")

    assigned: list[dict[str, Any]] = []
    slots = [split for split, count in TARGET_SPLITS.items() for _ in range(count)]
    for index, (row, split) in enumerate(zip(sorted(rows, key=_stable_key), slots), 1):
        assigned.append(
            {
                "seed_id": f"olist-medium-v1-{split}-{index:04d}",
                "split": split,
                "metric_ids": row["metric_ids"],
                "result_shape": row["result_shape"],
                "dimension": row["dimension"],
                "time": row["time"],
                "join_program_id": row["join_program_id"],
            }
        )
    return assigned


def write_rows(output: Path, rows: list[dict[str, Any]]) -> None:
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("medium seed release must stay outside the Git worktree")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    protected_fingerprints = load_protected_fingerprints(args.protected_summary_json)
    rows = generate_rows(protected_fingerprints)
    write_rows(args.output_jsonl, rows)
    print(json.dumps({"rows": len(rows), "splits": TARGET_SPLITS, "protected_family_hashes": len(protected_fingerprints), "output": str(args.output_jsonl.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
