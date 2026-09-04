from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.post_training.training.run_post_training_sft_smoke import (
    load_rows,
    required_max_seq_length,
    validate_split_audit,
)


def _row(sample_id: str, split: str) -> dict[str, object]:
    return {
        "sample_id": sample_id,
        "split": {"name": split},
        "prompt_format_version": "olist-candidate-sql-v1",
        "execution_outcome": {"postgres_reader_result_contract": "pass"},
        "rendered_prompt": "### SQL",
        "candidate_sql": "SELECT 1;",
        "training_text": "### SQL\nSELECT 1;",
    }


def _write(path: Path, row: dict[str, object]) -> None:
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_olist_rows_and_family_audit_are_accepted(tmp_path: Path) -> None:
    train_path, validation_path = tmp_path / "train.jsonl", tmp_path / "validation.jsonl"
    test_path = tmp_path / "final_evaluation_only" / "in_domain_test.jsonl"
    test_path.parent.mkdir()
    _write(train_path, _row("train-1", "train"))
    _write(validation_path, _row("validation-1", "validation"))
    _write(test_path, _row("test-1", "in_domain_test"))
    import hashlib

    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    audit = {
        "checks": {"status": "pass", "family_split_overlap": [], "query_spec_split_overlap": [], "all_gold_admitted": True, "runtime_contract_rebuilt": True, "in_domain_test_forbidden_for_training": True},
        "policy": {"split_strategy": "olist_pilot_v1_family_isolated", "primary_group": "family_id", "test_storage": "final_evaluation_only", "test_forbidden_for_training": True},
        "splits": {"train": {"rows": 1, "sha256": digest(train_path)}, "validation": {"rows": 1, "sha256": digest(validation_path)}},
        "outputs": {"train_jsonl": str(train_path), "validation_jsonl": str(validation_path), "in_domain_test_jsonl": str(test_path)},
        "training_length_contract": {
            "max_seq_length": 2304,
            "formula": "exact rendered runtime prompt + canonical SQL + EOS",
            "silent_truncation": False,
        },
    }
    assert load_rows(train_path, "train")[0]["sample_id"] == "train-1"
    validate_split_audit(audit, train_path, validation_path)
    assert required_max_seq_length(audit) == 2304


def test_olist_rows_reject_missing_postgres_contract_evidence(tmp_path: Path) -> None:
    path = tmp_path / "train.jsonl"
    row = _row("train-1", "train")
    row["execution_outcome"] = {}
    _write(path, row)
    with pytest.raises(ValueError, match="lacks execution evidence"):
        load_rows(path, "train")


def test_olist_audit_rejects_silent_truncation(tmp_path: Path) -> None:
    train_path, validation_path = tmp_path / "train.jsonl", tmp_path / "validation.jsonl"
    test_path = tmp_path / "final_evaluation_only" / "in_domain_test.jsonl"
    test_path.parent.mkdir()
    _write(train_path, _row("train-1", "train"))
    _write(validation_path, _row("validation-1", "validation"))
    _write(test_path, _row("test-1", "in_domain_test"))
    import hashlib

    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    audit = {
        "checks": {"status": "pass", "family_split_overlap": [], "query_spec_split_overlap": [], "all_gold_admitted": True, "runtime_contract_rebuilt": True, "in_domain_test_forbidden_for_training": True},
        "policy": {"split_strategy": "olist_pilot_v1_family_isolated", "primary_group": "family_id", "test_storage": "final_evaluation_only", "test_forbidden_for_training": True},
        "splits": {"train": {"rows": 1, "sha256": digest(train_path)}, "validation": {"rows": 1, "sha256": digest(validation_path)}},
        "outputs": {"train_jsonl": str(train_path), "validation_jsonl": str(validation_path), "in_domain_test_jsonl": str(test_path)},
        "training_length_contract": {"max_seq_length": 2304, "formula": "exact rendered runtime prompt + canonical SQL + EOS", "silent_truncation": True},
    }

    with pytest.raises(ValueError, match="silent truncation"):
        validate_split_audit(audit, train_path, validation_path)
