from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE
from data_analysis_agent.olist_queryspec import QuerySpec, WorkspacePin
from data_analysis_agent.semantic_catalog import CatalogLoader
from scripts.post_training.evaluation.admit_olist_v3_gold_seed_batch import (
    MAX_BATCH_ROWS,
    make_context,
    select_admission_rows,
    sha256_file,
    load_validated_seed_fixture,
    _read_selection,
)


ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "data" / "fixtures" / "olist_v3_coverage_family_seeds_v1.jsonl"
SELECTION = ROOT / "data" / "fixtures" / "olist_v3_small_gold_admission_selection_v1.json"


def test_v3_small_admission_selection_is_hash_bound_and_covers_the_frozen_slice() -> None:
    rows = load_validated_seed_fixture(SEEDS)
    selection = _read_selection(SELECTION)

    selected = select_admission_rows(rows, selection, fixture_sha256=sha256_file(SEEDS))

    assert len(selected) == MAX_BATCH_ROWS == 12
    assert [row["seed_id"] for row in selected] == selection["seed_ids"]
    assert {row["primary_bucket"] for row in selected} == set(
        selection["required_primary_buckets"]
    )
    assert {row["split"] for row in selected} == {
        "train",
        "validation",
        "in_domain_test",
    }
    assert set(selection["required_new_metric_ids"]) <= {
        metric_id for row in selected for metric_id in row["query_spec"]["metric_ids"]
    }


def test_v3_small_admission_selection_fails_closed_on_fixture_hash_or_family_drift(
    tmp_path: Path,
) -> None:
    rows = load_validated_seed_fixture(SEEDS)
    selection = _read_selection(SELECTION)

    with pytest.raises(ValueError, match="fixture hash"):
        select_admission_rows(rows, selection, fixture_sha256="0" * 64)

    tampered = copy.deepcopy(rows)
    tampered[0]["family_id"] = "family_tampered"
    with pytest.raises(ValueError, match="family ID"):
        # Re-serialize so the fixture loader, rather than the selection helper,
        # sees the same raw JSON boundary used by the command line tool.
        temporary = tmp_path / "tampered-seed.jsonl"
        temporary.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in tampered),
            encoding="utf-8",
        )
        load_validated_seed_fixture(temporary)


def test_v3_admission_context_uses_catalog_constraints_not_a_handwritten_metric_list() -> None:
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    spec = QuerySpec.create_validated(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        metric_ids=("review_count",),
        result_shape="scalar",
    )

    context = make_context(spec, "seed-review-count", catalog)

    assert context.metadata["metric_value_constraints"] == {
        "review_count": {"minimum": 0, "integer_like": True}
    }
    assert context.metadata["metric_version"] == "0.3-proposal"
    assert "run_id" not in context.metadata
