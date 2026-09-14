from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.post_training.evaluation.admit_olist_v3_balanced_gold_release import (
    EXPECTED_ROWS,
    SEMANTIC_REVIEW_SAMPLE_SIZE,
    load_full_v3_gold_rows,
    semantic_review_seed_ids,
)


STRUCTURAL_DIR = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/olist-v3-balanced-release-v1/structural-20260914"
)


def test_full_v3_release_loader_accepts_hash_bound_materialization() -> None:
    manifest, rows = load_full_v3_gold_rows(STRUCTURAL_DIR)

    assert manifest["checks"]["status"] == "pass"
    assert len(rows) == EXPECTED_ROWS
    assert len({row["seed_id"] for row in rows}) == EXPECTED_ROWS
    assert len({row["gold_artifact"]["sql_sha256"] for row in rows}) == EXPECTED_ROWS


def test_full_v3_release_loader_fails_closed_when_gold_hash_drifts(tmp_path: Path) -> None:
    source_manifest = json.loads((STRUCTURAL_DIR / "materialization_manifest.json").read_text())
    source_queries = (STRUCTURAL_DIR / "query_specs.jsonl").read_text()
    source_gold = (STRUCTURAL_DIR / "gold_sql.jsonl").read_text()
    (tmp_path / "materialization_manifest.json").write_text(json.dumps(source_manifest))
    (tmp_path / "query_specs.jsonl").write_text(source_queries)
    (tmp_path / "gold_sql.jsonl").write_text(source_gold + "\n")

    with pytest.raises(ValueError, match="hashes or row counts"):
        load_full_v3_gold_rows(tmp_path)


def test_semantic_review_selection_is_deterministic_and_bounded() -> None:
    _, rows = load_full_v3_gold_rows(STRUCTURAL_DIR)

    first = semantic_review_seed_ids(rows)
    second = semantic_review_seed_ids(rows)

    assert first == second
    assert len(first) == SEMANTIC_REVIEW_SAMPLE_SIZE
    assert first <= {row["seed_id"] for row in rows}
