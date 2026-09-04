from __future__ import annotations

import json
from pathlib import Path

from data_analysis_agent.olist_queryspec import QuerySpec
from scripts.post_training.data.materialize_olist_queryspecs import (
    _SEED_FIELDS,
    canonicalize_seed,
    family_id,
)


_ROOT = Path(__file__).resolve().parents[1]
_SEEDS_PATH = _ROOT / "data" / "fixtures" / "olist_queryspec_coverage_seeds_v1.jsonl"


def _load_seeds() -> list[dict[str, object]]:
    return [json.loads(line) for line in _SEEDS_PATH.read_text(encoding="utf-8").splitlines() if line]


def test_static_coverage_seed_manifest_matches_the_structural_contract() -> None:
    seeds = _load_seeds()

    assert len(seeds) == 15
    assert len({seed["seed_id"] for seed in seeds}) == len(seeds)
    assert all(set(seed) == _SEED_FIELDS - {"attribution_rule_id"} for seed in seeds)
    assert all(not ({"question", "prompt", "sql", "result", "limit"} & set(seed)) for seed in seeds)

    specs = []
    for seed in seeds:
        canonical = canonicalize_seed(seed)
        specs.append(
            QuerySpec.create_validated(
                metric_ids=canonical["metric_ids"],
                result_shape=canonical["result_shape"],
                dimension=canonical["dimension"],
                time=canonical["time"],
                join_program_id=canonical["join_program_id"],
                attribution_rule_id=canonical["attribution_rule_id"],
            )
        )

    assert len({family_id(spec) for spec in specs}) == len(specs)


def test_static_coverage_seed_manifest_has_the_reviewed_split_and_coverage_distribution() -> None:
    seeds = _load_seeds()
    programs_by_split: dict[str, set[str]] = {}
    metrics = {metric for seed in seeds for metric in seed["metric_ids"]}
    shapes = {seed["result_shape"] for seed in seeds}

    for seed in seeds:
        programs_by_split.setdefault(seed["split"], set()).add(seed["join_program_id"])

    assert {split: len(programs) for split, programs in programs_by_split.items()} == {
        "train": 6,
        "validation": 3,
        "in_domain_test": 2,
    }
    assert not (programs_by_split["train"] & programs_by_split["validation"])
    assert not (programs_by_split["train"] & programs_by_split["in_domain_test"])
    assert not (programs_by_split["validation"] & programs_by_split["in_domain_test"])
    assert metrics == {
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
    }
    assert shapes == {"scalar", "state_grouped", "category_grouped", "time_series"}
