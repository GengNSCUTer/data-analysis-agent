from __future__ import annotations

import hashlib
import json

import pytest

from data_analysis_agent.olist_queryspec import QuerySpec
from data_analysis_agent.semantic_catalog import CatalogLoader
from scripts.post_training.data.materialize_olist_schema_aware_program_sft import (
    SchemaAwareMaterializationError,
    TASK_A,
    TASK_B,
    build_events,
    render_program_prompt,
    serialize_schema_link_plan,
)


class _Tokenizer:
    eos_token_id = 151643

    def __call__(self, value: str, *, add_special_tokens: bool) -> dict[str, list[int]]:
        assert add_special_tokens is False
        return {"input_ids": list(range(len(value.split())))}


def _source_and_admission(
    *,
    split: str = "train",
    metric_id: str = "gmv",
    sql: str = "SELECT 1;",
) -> tuple[dict[str, object], dict[str, object]]:
    spec = QuerySpec.create_validated(metric_ids=(metric_id,), result_shape="scalar")
    prompt = (
        "### Task\nGenerate exactly one SQL query.\n"
        "### Server-provided Semantic Catalog\nCATALOG\n"
        "### Server-provided Query Plan\nPLAN\n"
        "### Question\nQUESTION\n### SQL"
    )
    seed_id = f"seed-{split}"
    source = {
        "sample_id": f"sample-{split}",
        "seed_id": seed_id,
        "query_spec_id": spec.query_spec_id,
        "family_id": f"family-{split}",
        "sql_program_id": spec.join_program_id,
        "split": {"name": split},
        "prompt_format_version": "olist-candidate-sql-v1",
        "rendered_prompt": prompt,
        "candidate_sql": sql,
        "training_text": prompt + "\n" + sql,
        "admission_status": "admitted",
        "execution_outcome": {"postgres_reader_result_contract": "pass"},
    }
    admission = {
        "seed_id": seed_id,
        "split": split,
        "family_id": f"family-{split}",
        "sql_program_id": spec.join_program_id,
        "admission_status": "admitted",
        "gold_sql": sql,
        "gold_sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "query_spec": spec.as_dict(),
    }
    return source, admission


def test_build_events_preserves_task_a_bytes_and_pairs_a_canonical_program() -> None:
    train_source, train_admission = _source_and_admission(split="train")
    validation_source, validation_admission = _source_and_admission(
        split="validation", metric_id="paid_order_count"
    )

    events, pairing, exclusions = build_events(
        {"train": [train_source], "validation": [validation_source]},
        {
            str(train_admission["seed_id"]): train_admission,
            str(validation_admission["seed_id"]): validation_admission,
        },
        _Tokenizer(),
        max_seq_length=4096,
        catalog=CatalogLoader().load(),
    )

    assert exclusions == []
    task_a = events["train"][TASK_A][0]
    task_b = events["train"][TASK_B][0]
    assert task_a["rendered_prompt"] == train_source["rendered_prompt"]
    assert task_a["target_text"] == train_source["candidate_sql"]
    assert task_a["training_text"] == train_source["training_text"]
    assert task_b["rendered_prompt"] == render_program_prompt(
        str(train_source["rendered_prompt"])
    )
    target = json.loads(str(task_b["target_text"]))
    assert target["schema_link_plan_id"] == task_b["schema_link_plan_id"]
    assert target["query_spec_id"] == train_source["query_spec_id"]
    assert "sql" not in target
    assert [event["task_type"] for event in events["train"]["interleaved"]] == [
        TASK_A,
        TASK_B,
    ]
    assert pairing[0]["task_a_eligible"] is True
    assert pairing[0]["task_b_eligible"] is True


def test_schema_link_plan_target_is_stable_and_includes_the_verified_id() -> None:
    spec = QuerySpec.create_validated(
        metric_ids=("average_order_value",), result_shape="scalar"
    )
    from data_analysis_agent.olist_schema_link_plan import derive_schema_link_plan

    first = serialize_schema_link_plan(derive_schema_link_plan(spec))
    second = serialize_schema_link_plan(derive_schema_link_plan(spec))

    assert first == second
    payload = json.loads(first)
    assert payload["schema_link_plan_id"].startswith("slp_")
    assert payload["metric_programs"][0]["dedup_rule_id"] == "preaggregate_order_price"


def test_build_events_rejects_a_source_target_that_drifted_from_admitted_gold() -> None:
    source, admission = _source_and_admission(sql="SELECT 1;")
    source["candidate_sql"] = "SELECT 2;"
    source["training_text"] = str(source["rendered_prompt"]) + "\nSELECT 2;"

    with pytest.raises(
        SchemaAwareMaterializationError, match="differs from admitted Gold SQL"
    ):
        build_events(
            {"train": [source], "validation": []},
            {str(admission["seed_id"]): admission},
            _Tokenizer(),
            max_seq_length=4096,
            catalog=CatalogLoader().load(),
        )


def test_build_events_records_an_overlength_program_without_silent_truncation() -> None:
    source, admission = _source_and_admission()

    events, pairing, exclusions = build_events(
        {"train": [source], "validation": []},
        {str(admission["seed_id"]): admission},
        _Tokenizer(),
        max_seq_length=1,
        catalog=CatalogLoader().load(),
    )

    assert events["train"][TASK_A] == []
    assert events["train"][TASK_B] == []
    assert {row["task_type"] for row in exclusions} == {TASK_A, TASK_B}
    assert all(
        row["reason"] == "sequence_exceeds_frozen_contract" for row in exclusions
    )
    assert pairing[0]["task_a_eligible"] is False
    assert pairing[0]["task_b_eligible"] is False


def test_build_events_rejects_any_non_train_validation_source_split() -> None:
    source, admission = _source_and_admission()

    with pytest.raises(
        SchemaAwareMaterializationError, match="only train and validation"
    ):
        build_events(
            {"train": [source], "in_domain_test": []},
            {str(admission["seed_id"]): admission},
            _Tokenizer(),
            max_seq_length=4096,
            catalog=CatalogLoader().load(),
        )


def test_build_events_rejects_family_or_query_spec_cross_split_leakage() -> None:
    train_source, train_admission = _source_and_admission(split="train")
    validation_source, validation_admission = _source_and_admission(
        split="validation", metric_id="paid_order_count"
    )
    validation_source["family_id"] = train_source["family_id"]

    with pytest.raises(SchemaAwareMaterializationError, match="family IDs cross"):
        build_events(
            {"train": [train_source], "validation": [validation_source]},
            {
                str(train_admission["seed_id"]): train_admission,
                str(validation_admission["seed_id"]): validation_admission,
            },
            _Tokenizer(),
            max_seq_length=4096,
            catalog=CatalogLoader().load(),
        )


def test_build_events_rejects_query_spec_cross_split_leakage() -> None:
    train_source, train_admission = _source_and_admission(split="train")
    validation_source, validation_admission = _source_and_admission(
        split="validation", metric_id="paid_order_count"
    )
    validation_source["query_spec_id"] = train_source["query_spec_id"]

    with pytest.raises(SchemaAwareMaterializationError, match="QuerySpec IDs cross"):
        build_events(
            {"train": [train_source], "validation": [validation_source]},
            {
                str(train_admission["seed_id"]): train_admission,
                str(validation_admission["seed_id"]): validation_admission,
            },
            _Tokenizer(),
            max_seq_length=4096,
            catalog=CatalogLoader().load(),
        )


def test_program_prompt_extracts_only_structural_runtime_sections() -> None:
    prompt = (
        "prefix\n### Server-provided Semantic Catalog\nCATALOG\n"
        "### Server-provided Query Plan\nPLAN\n### Question\nQUESTION\n### SQL"
    )
    rendered = render_program_prompt(prompt)
    assert "CATALOG" in rendered and "PLAN" in rendered and "QUESTION" in rendered
    assert "prefix" not in rendered
    assert rendered.count("### SQL") == 0
