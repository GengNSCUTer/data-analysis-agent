from copy import deepcopy

import pytest
from sqlglot import parse_one

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.runtime_alignment_audit import replay_single_turn
from data_analysis_agent.runtime_candidate_materializer import (
    RuntimeMaterializationError, catalog_dependency_evidence, materialize_runtime_candidate_row,
    reorder_gold_projection, text_sha256,
)
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever


@pytest.fixture(scope="module")
def retriever():
    return CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load())


@pytest.fixture
def source():
    sql = (
        "WITH m AS (SELECT c.customer_state, COUNT(DISTINCT o.order_id) AS paid_order_count "
        "FROM analytics.fact_orders o JOIN analytics.dim_customers c ON o.customer_id=c.customer_id "
        "WHERE o.order_status NOT IN ('canceled','unavailable') "
        "AND o.order_purchase_timestamp >= TIMESTAMP '2017-01-01' "
        "AND o.order_purchase_timestamp < TIMESTAMP '2017-02-01' GROUP BY c.customer_state) "
        "SELECT m.paid_order_count AS paid_order_count, m.customer_state AS customer_state FROM m;"
    )
    return {
        "sample_id": "unit-source", "workspace_key": "olist", "split": {"name": "train"},
        "admission_status": "admitted", "prompt_format_version": "multi-schema-sql-only-v1",
        "question": "统计2017-01-01至2017-02-01各州有效订单数",
        "candidate_sql": sql, "canonical_gold_sql_sha256": text_sha256(sql), "rendered_prompt": "old prompt",
        "scenario_family_id": "unit-family", "program_id": "unit-program", "risk_tags": ["distinct"],
        "query_spec": {
            "metric_ids": ["paid_order_count"], "dimension": "customer_state",
            "time": {"grain": None, "start": "2017-01-01", "end_exclusive": "2017-02-01"},
            "required_result_columns": ["paid_order_count", "customer_state"],
            "workspace": {field: getattr(OLIST_RUNTIME_WORKSPACE, field) for field in (
                "catalog_version", "dataset_version", "metric_version", "policy_version",
            )},
        },
    }


def test_repaired_question_is_replayed_and_gold_body_keeps_parent_provenance(source, retriever):
    before = deepcopy(source)
    row = materialize_runtime_candidate_row(source, retriever, OLIST_RUNTIME_WORKSPACE)
    assert source == before
    assert "不含结束日" in row["question"]
    assert row["query_plan"]["time_range"]["end"] == "2017-02-01"
    assert row["query_plan"]["required_result_columns"] == ["customer_state", "paid_order_count"]
    assert row["provenance"]["parent_gold_sql_sha256"] == source["canonical_gold_sql_sha256"]
    assert row["provenance"]["question_changes"] == ["clarify_exclusive_end"]
    assert row["candidate_sql"] not in row["rendered_prompt"]
    assert "unit-family" not in row["rendered_prompt"]
    assert "query_spec" not in row["rendered_prompt"]
    replay = replay_single_turn(row["question"], retriever, workspace=OLIST_RUNTIME_WORKSPACE)
    assert row["rendered_prompt"] == replay["candidate_prompt"]
    assert row["provenance"]["candidate_prompt_sha256"] == replay["candidate_request"]["prompt_sha256"]
    assert row["admission_status"] == "pending_execution"
    old_body, new_body = (parse_one(sql, read="postgres") for sql in (source["candidate_sql"], row["candidate_sql"]))
    old_body.set("expressions", [])
    new_body.set("expressions", [])
    assert old_body == new_body  # CTEs, filters, grains and joins are unchanged.


def test_source_spec_cannot_inject_an_unasked_dimension(source, retriever):
    source["query_spec"]["dimension"] = "product_category_name"
    with pytest.raises(RuntimeMaterializationError, match="dimension_mismatch"):
        materialize_runtime_candidate_row(source, retriever, OLIST_RUNTIME_WORKSPACE)


@pytest.mark.parametrize("change,reason", [
    ("test_split", "source_must_be_train_or_validation"),
    ("gold_drift", "source_gold_hash_drift"),
    ("inclusive", "explicit_inclusive_question_conflicts_with_gold"),
])
def test_protected_split_hash_drift_and_explicit_conflict_fail(source, retriever, change, reason):
    if change == "test_split":
        source["split"] = {"name": "test"}
    elif change == "gold_drift":
        source["candidate_sql"] += " SELECT 1;"
    else:
        source["question"] += "（包括结束日）"
    with pytest.raises(RuntimeMaterializationError, match=reason):
        materialize_runtime_candidate_row(source, retriever, OLIST_RUNTIME_WORKSPACE)


def test_missing_time_grain_is_an_explicit_question_edit(source, retriever):
    source["question"] = "统计2017-01-01至2017-02-01有效订单数的时间变化"
    source["query_spec"].update(dimension=None, required_result_columns=["time", "paid_order_count"])
    source["query_spec"]["time"]["grain"] = "month"
    # Repair is separate from Gold/semantic admission: this intentionally
    # keeps a mismatched Gold so the materializer must still reject it.
    from data_analysis_agent.runtime_candidate_materializer import repair_source_question
    question, edits = repair_source_question(source, retriever, OLIST_RUNTIME_WORKSPACE)
    assert "请按月汇总时间序列" in question
    assert edits == ["clarify_exclusive_end", "clarify_time_grain"]
    with pytest.raises(RuntimeMaterializationError, match="gold_projection_columns_disagree"):
        materialize_runtime_candidate_row(source, retriever, OLIST_RUNTIME_WORKSPACE)


def test_query_action_repair_does_not_add_an_unasked_dimension(source, retriever):
    from data_analysis_agent.runtime_candidate_materializer import repair_source_question
    source["question"] = "有效订单数的统计口径"
    question, edits = repair_source_question(source, retriever, OLIST_RUNTIME_WORKSPACE)
    assert edits == ["clarify_data_query_action"]
    assert question.startswith("数据查询：")
    assert "customer_state" not in question and "各州" not in question
    with pytest.raises(RuntimeMaterializationError, match="dimension_mismatch"):
        materialize_runtime_candidate_row(source, retriever, OLIST_RUNTIME_WORKSPACE)


def test_pilot_selection_is_balanced_unique_and_stable(source):
    from collections import Counter
    from scripts.post_training.data.materialize_runtime_candidate_release import select_pilot
    rows = []
    for workspace in ("olist", "thelook"):
        for split in ("train", "validation"):
            for index in range(12):
                row = deepcopy(source)
                row.update(sample_id=f"{workspace}-{split}-{index}", workspace_key=workspace,
                           split={"name": split}, primary_variant_id=f"v{index % 8}")
                row["query_spec"]["result_shape"] = ("scalar", "state_grouped", "time_series")[index % 3]
                rows.append(row)
    chosen = select_pilot(rows)
    assert len({row["sample_id"] for row in chosen}) == 32
    assert Counter((row["workspace_key"], row["split"]["name"]) for row in chosen) == {
        (workspace, split): 8 for workspace in ("olist", "thelook") for split in ("train", "validation")
    }
    assert [row["sample_id"] for row in chosen] == [row["sample_id"] for row in select_pilot(rows[::-1])]


def test_reordering_rejects_ordinal_order_by_and_multiple_statements():
    with pytest.raises(RuntimeMaterializationError, match="ordinal"):
        reorder_gold_projection("SELECT a AS a,b AS b FROM analytics.t ORDER BY 1", ["b", "a"])
    with pytest.raises(RuntimeMaterializationError, match="one_select"):
        reorder_gold_projection("SELECT a FROM analytics.t; DELETE FROM analytics.t", ["a"])


def test_dependency_check_does_not_borrow_a_hidden_column_from_the_workspace(source, retriever):
    replay = replay_single_turn("统计2017年各州有效订单数", retriever, workspace=OLIST_RUNTIME_WORKSPACE)
    assert "order_status" in replay["selected_columns"]["fact_orders"]
    replay["selected_columns"]["fact_orders"] = [
        name for name in replay["selected_columns"]["fact_orders"] if name != "order_status"
    ]
    with pytest.raises(RuntimeMaterializationError, match="gold_column_missing"):
        catalog_dependency_evidence(source["candidate_sql"], replay, retriever, OLIST_RUNTIME_WORKSPACE)
