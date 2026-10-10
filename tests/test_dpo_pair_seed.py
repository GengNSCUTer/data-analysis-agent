from __future__ import annotations

from copy import deepcopy

import pytest

from data_analysis_agent.dpo_pair import DpoPairValidationError, sha256_text
from data_analysis_agent.dpo_pair_seed import (
    DPO_PAIR_SEED_SCHEMA_VERSION,
    build_dpo_pair_seed,
)


def _row() -> dict[str, object]:
    prompt = "### Task\nGenerate exactly one query.\n### SQL"
    sql = "SELECT COUNT(*) AS completed_order_count FROM analytics.fact_orders AS o WHERE o.order_status = 'delivered';"
    return {
        "admission_status": "admitted",
        "split": {"name": "train"},
        "workspace_key": "olist",
        "sample_id": "multi-schema-olist-0001",
        "query_spec_id": "qs_0001",
        "scenario_family_id": "orders_scalar",
        "program_id": "program_0001",
        "risk_tags": ["status_filter", "time_filter"],
        "query_spec": {"result_shape": "scalar"},
        "prompt_format_version": "multi-schema-sql-only-v1",
        "rendered_prompt": prompt,
        "candidate_sql": sql,
        "canonical_gold_sql_sha256": sha256_text(sql),
        "training_text": f"{prompt}\n{sql}",
    }


def test_projects_traceable_chosen_side_seed() -> None:
    seed = build_dpo_pair_seed(_row(), release_version="multi-schema-ecommerce-sql-only-sft-v1-r1")
    result = seed.as_dict()

    assert result["schema_version"] == DPO_PAIR_SEED_SCHEMA_VERSION
    assert result["rejected"] is None
    assert result["source"]["release_split"] == "train"
    assert result["source"]["prompt_sha256"] == sha256_text(_row()["rendered_prompt"])  # type: ignore[arg-type]
    assert result["chosen"]["sql_sha256"] == _row()["canonical_gold_sql_sha256"]  # type: ignore[index]


def test_rejects_unadmitted_or_test_source_rows() -> None:
    row = _row()
    row["admission_status"] = "pending"
    with pytest.raises(DpoPairValidationError, match="admitted"):
        build_dpo_pair_seed(row, release_version="release")

    row = _row()
    row["split"] = {"name": "test"}
    with pytest.raises(DpoPairValidationError, match="train or validation"):
        build_dpo_pair_seed(row, release_version="release")


def test_rejects_gold_or_training_text_drift() -> None:
    row = deepcopy(_row())
    row["canonical_gold_sql_sha256"] = "a" * 64
    with pytest.raises(DpoPairValidationError, match="Gold hash"):
        build_dpo_pair_seed(row, release_version="release")

    row = deepcopy(_row())
    row["training_text"] = "unbound text"
    with pytest.raises(DpoPairValidationError, match="boundary"):
        build_dpo_pair_seed(row, release_version="release")
