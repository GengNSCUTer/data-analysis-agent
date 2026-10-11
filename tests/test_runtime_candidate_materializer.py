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


@pytest.mark.parametrize("lengths,expected", [([1410, 3072], 3072), ([2137, 3463], 4096), ([4097], 5120)])
def test_length_contract_retains_every_sequence_without_truncation(lengths, expected):
    from scripts.post_training.data.materialize_runtime_candidate_release import freeze_max_seq_length
    assert freeze_max_seq_length(lengths) == expected


def test_unsupported_lengths_cannot_publish_a_release():
    from scripts.post_training.data.materialize_runtime_candidate_release import freeze_max_seq_length
    with pytest.raises(ValueError, match="remains draft"):
        freeze_max_seq_length([8193])
    with pytest.raises(ValueError, match="empty"):
        freeze_max_seq_length([])


def test_split_audit_distinguishes_scenario_category_from_thelook_program(source):
    from scripts.post_training.data.materialize_runtime_candidate_release import audit_identities
    rows = []
    for split in ("train", "validation"):
        row = deepcopy(source)
        row.update(sample_id=split, split={"name": split}, workspace_key="thelook",
                   query_spec_id=f"qs-{split}", scenario_family_id="shared-coverage-category",
                   program_id=f"independent-program-{split}", rendered_prompt=f"prompt-{split}",
                   candidate_sql=f"SELECT '{split}'", question=f"{split}问题")
        rows.append(row)
    report = audit_identities(rows)
    assert report["shared_scenario_category_count"] == 1
    assert report["split_family_id"]["cross_split_overlap"] == 0
    rows[1]["program_id"] = rows[0]["program_id"]
    with pytest.raises(ValueError, match="split_family_id"):
        audit_identities(rows)


@pytest.mark.parametrize("field", ["sample_id", "query_spec_id", "candidate_sql", "question", "rendered_prompt"])
def test_release_audit_rejects_duplicate_training_identity(source, field):
    from scripts.post_training.data.materialize_runtime_candidate_release import audit_identities
    first, second = deepcopy(source), deepcopy(source)
    first.update(query_spec_id="qs-first")
    second.update(sample_id="second", query_spec_id="qs-second", candidate_sql="SELECT 2",
                  question="不同的问题", rendered_prompt="different prompt", scenario_family_id="second-family")
    second[field] = first[field]
    with pytest.raises(ValueError, match="duplicate"):
        audit_identities([first, second])


def test_collector_timeout_is_an_exclusion_but_connection_loss_stops_batch(source, monkeypatch):
    import psycopg2
    from scripts.post_training.data import materialize_runtime_candidate_release as module
    monkeypatch.setattr(module, "materialize_runtime_candidate_row", lambda *args: {})
    def fail_timeout(*args):
        raise psycopg2.errors.QueryCanceled("timeout")
    monkeypatch.setattr(module, "admit_execution", fail_timeout)
    row, evidence = module.build_one(source, {"olist": object()}, None)
    assert row is None and evidence["reason"] == "statement_timeout"
    def fail_connection(*args):
        raise psycopg2.OperationalError("connection lost")
    monkeypatch.setattr(module, "admit_execution", fail_connection)
    with pytest.raises(psycopg2.OperationalError):
        module.build_one(source, {"olist": object()}, None)


def test_release_gate_rejects_tampered_or_incomplete_pilot(tmp_path):
    import hashlib
    import json
    from scripts.post_training.data.materialize_runtime_candidate_release import (
        SOURCE_HASHES, verify_completed_pilot,
    )
    records = [{"sample_id": str(i), "state": "admitted", "execution": {
        "transaction_read_only": True, "execution_truncated": False,
        "result_state": "valid", "denotation": "ordered_denotation_match",
    }} for i in range(32)]
    data = "\n".join(json.dumps(row) for row in records)
    (tmp_path / "evidence.jsonl").write_text(data)
    manifest = {"selected": 32, "admitted": 32, "excluded": 0,
                "source_files": {key: {"sha256": value} for key, value in SOURCE_HASHES.items()},
                "artifacts": {"evidence.jsonl": {"sha256": hashlib.sha256(data.encode()).hexdigest()}}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert verify_completed_pilot(tmp_path)["sha256"]
    (tmp_path / "evidence.jsonl").write_text(data + "\n{}")
    with pytest.raises(ValueError, match="artifact hash drift"):
        verify_completed_pilot(tmp_path)
    (tmp_path / "evidence.jsonl").write_text(data)
    manifest["admitted"] = 31
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="completed 32-case pilot"):
        verify_completed_pilot(tmp_path)
