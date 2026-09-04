from __future__ import annotations

from scripts.post_training.evaluation.admit_olist_gold_release import (
    SEMANTIC_REVIEW_SAMPLE_SIZE,
    semantic_review_seed_ids,
)


def _row(index: int, metric: str, shape: str, mode: str, grain: str | None = None) -> dict[str, object]:
    return {
        "seed_id": f"seed-{index:03d}",
        "query_spec": {
            "metric_ids": [metric],
            "result_shape": shape,
            "time": {"mode": mode, "grain": grain},
        },
    }


def test_semantic_review_sample_is_deterministic_bounded_and_covers_distinct_buckets() -> None:
    rows = [
        _row(1, "gmv", "scalar", "all_time"),
        _row(2, "gmv", "scalar", "all_time"),
        _row(3, "item_count", "state_grouped", "absolute_range"),
        _row(4, "positive_review_rate", "time_series", "series", "month"),
    ]
    selected = semantic_review_seed_ids(rows)
    assert selected == semantic_review_seed_ids(list(reversed(rows)))
    assert len(selected) == 4
    assert selected <= {str(row["seed_id"]) for row in rows}


def test_semantic_review_sample_never_exceeds_release_limit() -> None:
    rows = [_row(index, "gmv", "scalar", "all_time") for index in range(100)]
    assert len(semantic_review_seed_ids(rows)) == SEMANTIC_REVIEW_SAMPLE_SIZE
