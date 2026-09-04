from __future__ import annotations

from collections import Counter

from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime
from scripts.post_training.data.generate_olist_medium_sft_seeds import (
    TARGET_SPLITS,
    _protected_fingerprint,
    generate_rows,
)
from scripts.post_training.data.materialize_olist_queryspecs import family_id


def _family(row: dict[str, object]) -> str:
    return family_id(
        QuerySpec.create(
            metric_ids=row["metric_ids"],
            result_shape=row["result_shape"],
            dimension=row["dimension"],
            time=QueryTime(**row["time"]),
            join_program_id=row["join_program_id"],
        )
    )


def test_medium_release_has_exact_splits_and_unique_semantic_families() -> None:
    rows = generate_rows()
    assert Counter(row["split"] for row in rows) == TARGET_SPLITS
    assert len(rows) == 1200
    assert len({row["seed_id"] for row in rows}) == 1200
    assert {row["result_shape"] for row in rows} == {"scalar", "state_grouped", "category_grouped", "time_series"}
    assert {metric for row in rows for metric in row["metric_ids"]} == {
        "gmv", "paid_order_count", "average_delivery_days", "positive_review_rate", "item_count",
        "average_order_value", "average_review_score", "on_time_delivery_rate", "cancellation_rate", "freight_amount",
    }


def test_medium_release_replaces_a_protected_family_instead_of_shrinking_a_split() -> None:
    baseline = generate_rows()
    protected_family = _family(baseline[0])
    protected = _protected_fingerprint(protected_family)
    rows = generate_rows(frozenset({protected}))
    assert Counter(row["split"] for row in rows) == TARGET_SPLITS
    assert protected_family not in {_family(row) for row in rows}
