from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from data_analysis_agent.olist_queryspec import QuerySpec
from scripts.post_training.data.materialize_olist_queryspecs import (
    _SEED_FIELDS,
    canonicalize_seed,
    family_id,
)


_ROOT = Path(__file__).resolve().parents[1]
_SEEDS_PATH = _ROOT / "data" / "fixtures" / "olist_queryspec_coverage_seeds_v2.jsonl"


def _load_seeds() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in _SEEDS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _make_specs(seeds: list[dict[str, object]]) -> list[QuerySpec]:
    specs: list[QuerySpec] = []
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
    return specs


def test_v2_seed_manifest_is_a_40_family_structural_contract() -> None:
    seeds = _load_seeds()

    assert len(seeds) == 40
    assert len({seed["seed_id"] for seed in seeds}) == 40
    assert all(set(seed) == _SEED_FIELDS - {"attribution_rule_id"} for seed in seeds)
    assert all(
        not ({"question", "prompt", "sql", "result", "limit"} & set(seed))
        for seed in seeds
    )

    specs = _make_specs(seeds)
    assert len({family_id(spec) for spec in specs}) == 40
    assert len({spec.query_spec_id for spec in specs}) == 40


def test_v2_seed_manifest_split_and_coverage_contract() -> None:
    seeds = _load_seeds()
    specs = _make_specs(seeds)

    assert Counter(seed["split"] for seed in seeds) == {
        "train": 24,
        "validation": 8,
        "in_domain_test": 8,
    }
    assert {spec.result_shape for spec in specs} == {
        "scalar",
        "state_grouped",
        "category_grouped",
        "time_series",
    }
    assert {metric for spec in specs for metric in spec.metric_ids} == {
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


def test_v2_isolates_families_but_allows_shared_join_programs() -> None:
    seeds = _load_seeds()
    specs = _make_specs(seeds)
    family_splits: dict[str, set[str]] = {}
    program_splits: dict[str, set[str]] = {}
    for seed, spec in zip(seeds, specs):
        family_splits.setdefault(family_id(spec), set()).add(str(seed["split"]))
        program_splits.setdefault(spec.join_program_id, set()).add(str(seed["split"]))

    assert all(len(splits) == 1 for splits in family_splits.values())
    assert any(len(splits) > 1 for splits in program_splits.values())

