from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from data_analysis_agent.olist_queryspec import WorkspacePin
from scripts.post_training.data import audit_olist_schema_aware_program_sft as audit_module
from scripts.post_training.data.audit_olist_schema_aware_program_sft import (
    SchemaAwareAuditError,
    audit,
    records_sha256,
    require_exact_records,
)
from scripts.post_training.data.materialize_olist_schema_aware_program_sft import (
    CONTRACT_VERSION,
    SchemaAwareMaterializationError,
    sha256_file,
)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _prepare_audit_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate_train_task_a: bool = False,
    raise_from_rederive: bool = False,
) -> dict[str, Path]:
    """Build a tiny external fixture and mock only Olist source decoding.

    The real audit's important behavior is the comparison of every external
    materialization row with a freshly re-derived canonical event sequence.
    A two-split, one-pair fixture exercises that orchestration without making
    repository tests depend on 200+ MB of protected training artifacts.
    """

    root = tmp_path / "olist-domain-sft-release-v2-fixture"
    root.mkdir()
    source_audit = root / "split_audit.json"
    source_train = root / "train.jsonl"
    source_validation = root / "validation.jsonl"
    source_audit.write_text("{}\n", encoding="utf-8")
    source_train.write_text("{}\n", encoding="utf-8")
    source_validation.write_text("{}\n", encoding="utf-8")

    admission_dir = root / "admission-v2"
    admission_dir.mkdir()
    admission_records = admission_dir / "admitted_records.jsonl"
    admission_records.write_text("{}\n", encoding="utf-8")

    materialization = tmp_path / "materialization"
    materialization.mkdir()
    exclusions = materialization / "exclusions"
    exclusions.mkdir()
    (exclusions / "length.jsonl").write_text("", encoding="utf-8")

    expected_events = {
        "train": {
            "sql": [{"event_id": "train:sql", "task_type": "sql"}],
            "schema_link_plan": [
                {"event_id": "train:schema", "task_type": "schema_link_plan"}
            ],
            "interleaved": [
                {"event_id": "train:sql", "task_type": "sql"},
                {"event_id": "train:schema", "task_type": "schema_link_plan"},
            ],
        },
        "validation": {
            "sql": [{"event_id": "validation:sql", "task_type": "sql"}],
            "schema_link_plan": [
                {
                    "event_id": "validation:schema",
                    "task_type": "schema_link_plan",
                }
            ],
            "interleaved": [
                {"event_id": "validation:sql", "task_type": "sql"},
                {
                    "event_id": "validation:schema",
                    "task_type": "schema_link_plan",
                },
            ],
        },
    }
    expected_pairing = [
        {"pair_id": "train", "split": "train"},
        {"pair_id": "validation", "split": "validation"},
    ]
    filenames = {
        "train_task_a.jsonl": expected_events["train"]["sql"],
        "train_task_b.jsonl": expected_events["train"]["schema_link_plan"],
        "train_events.jsonl": expected_events["train"]["interleaved"],
        "validation_task_a.jsonl": expected_events["validation"]["sql"],
        "validation_task_b.jsonl": expected_events["validation"]["schema_link_plan"],
        "validation_events.jsonl": expected_events["validation"]["interleaved"],
        "pairing.jsonl": expected_pairing,
    }
    for filename, rows in filenames.items():
        actual_rows = [dict(row) for row in rows]
        if mutate_train_task_a and filename == "train_task_a.jsonl":
            actual_rows[0]["task_type"] = "mutated"
        _write_jsonl(materialization / filename, actual_rows)

    splits = {}
    for split in ("train", "validation"):
        task_a = materialization / f"{split}_task_a.jsonl"
        task_b = materialization / f"{split}_task_b.jsonl"
        events = materialization / f"{split}_events.jsonl"
        splits[split] = {
            "query_instances": 1,
            "task_a_events": 1,
            "task_b_events": 1,
            "interleaved_events": 2,
            "task_a_sha256": sha256_file(task_a),
            "task_b_sha256": sha256_file(task_b),
            "events_sha256": sha256_file(events),
        }

    source_audit_payload = {
        "source": {"admission_assembly_manifest_sha256": "a" * 64}
    }
    materialization_audit = {
        "audit_version": CONTRACT_VERSION,
        "checks": {"status": "pass"},
        "policy": {
            "source_splits": ["train", "validation"],
            "in_domain_test_materialized": False,
            "thelook_read": False,
            "task_a_runtime_prompt_unchanged": True,
            "sql_and_plan_targets_separate": True,
        },
        "workspace": WorkspacePin.current().as_dict(),
        "source": {
            "source_split_audit_sha256": sha256_file(source_audit),
            "source_train_sha256": sha256_file(source_train),
            "source_validation_sha256": sha256_file(source_validation),
            "admission_manifest_sha256": "a" * 64,
            "admission_records_sha256": sha256_file(admission_records),
        },
        "tokenizer": {"eos_token_id": 7},
        "training_length_contract": {
            "max_seq_length": 3072,
            "silent_truncation": False,
        },
        "splits": splits,
        "pairing": {
            "rows": 2,
            "fully_eligible_pairs": 2,
            "sha256": sha256_file(materialization / "pairing.jsonl"),
        },
        "exclusions": {
            "rows": 0,
            "sha256": sha256_file(exclusions / "length.jsonl"),
            "contains_question_or_sql": False,
        },
    }
    (materialization / "materialization_audit.json").write_text(
        json.dumps(materialization_audit, sort_keys=True) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        audit_module,
        "_load_source_audit",
        lambda *_args, **_kwargs: source_audit_payload,
    )
    monkeypatch.setattr(
        audit_module,
        "load_source_split",
        lambda _path, split, _count: [{"split": split}],
    )
    monkeypatch.setattr(
        audit_module,
        "_load_admission_records",
        lambda *_args, **_kwargs: {"seed": {"seed_id": "seed"}},
    )
    monkeypatch.setattr(
        audit_module,
        "load_tokenizer",
        lambda _path: SimpleNamespace(eos_token_id=7),
    )
    monkeypatch.setattr(
        audit_module,
        "CatalogLoader",
        lambda: SimpleNamespace(load=lambda: object()),
    )
    if raise_from_rederive:
        monkeypatch.setattr(
            audit_module,
            "build_events",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                SchemaAwareMaterializationError(
                    "source QuerySpec IDs cross train and validation"
                )
            ),
        )
    else:
        monkeypatch.setattr(
            audit_module,
            "build_events",
            lambda *_args, **_kwargs: (expected_events, expected_pairing, []),
        )
    return {
        "materialization": materialization,
        "source_audit": source_audit,
        "source_train": source_train,
        "source_validation": source_validation,
        "admission_dir": admission_dir,
        "tokenizer_dir": root,
        "output": tmp_path / "audit-output",
    }


def _run_fixture_audit(paths: dict[str, Path]) -> dict[str, Any]:
    return audit(
        paths["materialization"],
        paths["source_audit"],
        paths["source_train"],
        paths["source_validation"],
        paths["admission_dir"],
        paths["tokenizer_dir"],
        paths["output"],
        expected_train=1,
        expected_validation=1,
        generated_at="2026-09-11T20:05:00+08:00",
    )


def test_records_sha256_is_canonical_across_mapping_insertion_order() -> None:
    assert records_sha256([{"a": 1, "b": 2}]) == records_sha256(
        [{"b": 2, "a": 1}]
    )


def test_require_exact_records_rejects_same_length_content_drift() -> None:
    with pytest.raises(SchemaAwareAuditError, match="record content differs"):
        require_exact_records(
            [{"event_id": "same", "target": "mutated"}],
            [{"event_id": "same", "target": "canonical"}],
            "Task A",
        )


def test_audit_accepts_a_complete_rederived_pair_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prepare_audit_fixture(tmp_path, monkeypatch)

    report = _run_fixture_audit(paths)

    assert report["checks"]["status"] == "pass"
    assert report["counts"]["pairs"] == 2
    assert (paths["output"] / "audit-report.json").is_file()


def test_audit_rejects_event_drift_even_if_file_hash_evidence_is_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prepare_audit_fixture(tmp_path, monkeypatch, mutate_train_task_a=True)

    with pytest.raises(SchemaAwareAuditError, match="train task_a record content differs"):
        _run_fixture_audit(paths)


def test_audit_surfaces_rederived_split_leakage_as_an_audit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prepare_audit_fixture(tmp_path, monkeypatch, raise_from_rederive=True)

    with pytest.raises(SchemaAwareAuditError, match="re-derived source split violates"):
        _run_fixture_audit(paths)
