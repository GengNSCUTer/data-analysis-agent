from __future__ import annotations

import pytest

from scripts.post_training.data.collect_multi_schema_dpo_candidates import (
    assess_pair_readiness,
    DpoCollectionError,
    select_best_rejected,
    select_generation_sources,
    select_generation_sources_with_audit,
    summarize_generation_selection,
)
from data_analysis_agent.dpo_pair import sha256_text


def _row(
    workspace: str,
    index: int,
    *,
    shape: str = "scalar",
    family: str | None = None,
    risk_tags: list[str] | None = None,
) -> dict[str, object]:
    return {
        "split": {"name": "train"}, "admission_status": "admitted",
        "prompt_format_version": "multi-schema-sql-only-v1",
        "sample_id": f"multi-schema-{workspace}-{index:04d}", "workspace_key": workspace,
        "rendered_prompt": f"### Task\n{workspace}-{index}\n### SQL",
        "scenario_family_id": family or f"{workspace}_family_{index}",
        "query_spec": {"result_shape": shape, "metric_ids": ["gmv"]},
        "risk_tags": risk_tags if risk_tags is not None else ["time_filter"],
        # Deliberately present: selection must never put it into GenerationSource.
        "candidate_sql": "SELECT secret_gold_sql;", "training_text": "not used",
    }


def test_selects_reproducible_workspace_balanced_prompt_only_sources() -> None:
    rows = [*[_row("olist", index) for index in range(3)], *[_row("thelook", index) for index in range(3)]]
    first = select_generation_sources(rows, per_workspace=2)
    second = select_generation_sources(rows, per_workspace=2)

    assert first == second
    assert [source.workspace_key for source in first].count("olist") == 2
    assert [source.workspace_key for source in first].count("thelook") == 2
    assert all(not hasattr(source, "candidate_sql") for source in first)
    assert all(source.prompt.endswith("### SQL") for source in first)


def test_stratifies_by_shape_family_and_risk_tag_without_reading_gold() -> None:
    olist = [
        _row("olist", 1, shape="scalar", risk_tags=["status_filter"]),
        _row("olist", 2, shape="state_grouped", risk_tags=["distinct"]),
        _row("olist", 3, shape="category_grouped", risk_tags=["two_stage_aggregate"]),
        _row("olist", 4, shape="time_series", risk_tags=["time_bucket"]),
    ]
    thelook = [
        _row("thelook", 1, shape="scalar", risk_tags=["status_filter"]),
        _row("thelook", 2, shape="scalar", risk_tags=["metric_formula"]),
        _row("thelook", 3, shape="dimension_grouped", risk_tags=["join_path"]),
        _row("thelook", 4, shape="time_series", risk_tags=["time_bucket"]),
    ]

    selected = select_generation_sources([*olist, *thelook], per_workspace=4)
    summary = summarize_generation_selection(selected, per_workspace=4)

    assert {source.scenario_family_id for source in selected if source.workspace_key == "olist"} == {
        "olist_family_1", "olist_family_2", "olist_family_3", "olist_family_4"
    }
    assert summary["workspaces"]["olist"]["result_shape_counts"] == {
        "category_grouped": 1, "scalar": 1, "state_grouped": 1, "time_series": 1,
    }
    assert summary["workspaces"]["thelook"]["result_shape_counts"] == {
        "dimension_grouped": 1, "scalar": 2, "time_series": 1,
    }
    assert "prompt" not in summary["workspaces"]["olist"]["sources"][0]
    assert "candidate_sql" not in summary["workspaces"]["olist"]["sources"][0]


def test_refuses_a_batch_too_small_to_cover_available_result_shapes() -> None:
    rows = [
        _row("olist", 1, shape="scalar"),
        _row("olist", 2, shape="state_grouped"),
        _row("olist", 3, shape="time_series"),
        _row("thelook", 1, shape="scalar"),
        _row("thelook", 2, shape="dimension_grouped"),
        _row("thelook", 3, shape="time_series"),
    ]
    with pytest.raises(DpoCollectionError, match="too small"):
        select_generation_sources(rows, per_workspace=2)


def test_backfills_legacy_simple_scalar_rows_and_records_derivation() -> None:
    rows = [
        _row("olist", 1, shape="scalar"),
        _row("olist", 2, shape="state_grouped"),
        _row("olist", 3, shape="time_series"),
        _row("olist", 4, shape="scalar", risk_tags=[]),
        _row("thelook", 1, shape="scalar"),
        _row("thelook", 2, shape="dimension_grouped"),
        _row("thelook", 3, shape="time_series"),
    ]

    selected, exclusions = select_generation_sources_with_audit(rows, per_workspace=3)

    assert len(selected) == 6
    assert exclusions == {
        "derivation_version": "olist-dpo-risk-tag-backfill-v1",
        "derived_tag": "simple_scalar_aggregation",
        "count": 1,
        "by_workspace": {"olist": 1},
        "sample_ids": ["multi-schema-olist-0004"],
    }


def test_rejects_non_train_or_single_workspace_sources() -> None:
    rows = [_row("olist", 1), _row("thelook", 1)]
    rows[0]["split"] = {"name": "validation"}
    with pytest.raises(DpoCollectionError, match="train rows only"):
        select_generation_sources(rows, per_workspace=1)

    with pytest.raises(DpoCollectionError, match="both Olist and TheLook"):
        select_generation_sources([_row("olist", 1)], per_workspace=1)


def test_validation_sources_require_an_explicit_validation_selector() -> None:
    rows = [_row("olist", 1), _row("thelook", 1)]
    for row in rows:
        row["split"] = {"name": "validation"}

    with pytest.raises(DpoCollectionError, match="train rows only"):
        select_generation_sources(rows, per_workspace=1)

    selected = select_generation_sources(
        rows, per_workspace=1, expected_split="validation"
    )

    assert {source.workspace_key for source in selected} == {"olist", "thelook"}


def test_selects_best_tier_only_when_candidate_sql_hash_is_bound() -> None:
    sql_a, sql_b = "SELECT a;", "SELECT b;"
    selected = select_best_rejected([
        {"eligible": True, "tier": "B", "candidate_sql": sql_b, "candidate_sql_sha256": sha256_text(sql_b)},
        {"eligible": True, "tier": "A", "candidate_sql": sql_a, "candidate_sql_sha256": sha256_text(sql_a)},
    ])
    assert selected is not None and selected["tier"] == "A"

    with pytest.raises(DpoCollectionError, match="SQL hash"):
        select_best_rejected([{"eligible": True, "tier": "A", "candidate_sql": sql_a}])


def test_stops_dpo_when_pairs_do_not_cover_both_workspaces() -> None:
    sources = select_generation_sources(
        [
            _row("olist", 1, shape="scalar", risk_tags=["status_filter"]),
            _row("olist", 2, shape="state_grouped", risk_tags=["distinct"]),
            _row("olist", 3, shape="time_series", risk_tags=["time_bucket"]),
            _row("thelook", 1, shape="scalar", risk_tags=["status_filter"]),
            _row("thelook", 2, shape="dimension_grouped", risk_tags=["join_path"]),
            _row("thelook", 3, shape="time_series", risk_tags=["time_bucket"]),
        ],
        per_workspace=3,
    )
    only_one_pair = [{
        "source": {
            "sample_id": "multi-schema-olist-0001", "workspace_key": "olist",
            "scenario_family_id": "olist_family_1", "result_shape": "scalar",
            "risk_tags": ["status_filter"],
        },
        "rejected": {"tier": "A"},
    }]

    readiness = assess_pair_readiness(
        only_one_pair, sources, minimum_pairs_per_workspace=3
    )

    assert readiness["status"] == "stop_no_dpo"
    assert readiness["dpo_training_authorized"] is False
    assert "insufficient_A_B_C_pairs" in readiness["workspaces"]["olist"]["stop_reasons"]
    assert "insufficient_A_B_C_pairs" in readiness["workspaces"]["thelook"]["stop_reasons"]
