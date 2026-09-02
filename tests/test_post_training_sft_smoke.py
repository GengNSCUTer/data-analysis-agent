from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("torch", reason="SFT dataset tests run in the isolated QLoRA environment")

from scripts.run_post_training_sft_smoke import (
    CausalSqlCollator,
    CausalSqlDataset,
    sha256_file,
    validate_split_audit,
)


class TinyTokenizer:
    eos_token_id = 99
    pad_token_id = 99

    def __call__(self, text: str, add_special_tokens: bool = False):
        del add_special_tokens
        return {"input_ids": [ord(char) % 37 + 1 for char in text]}


def row(sql: str = "SELECT name FROM products") -> dict[str, object]:
    prompt = "### SQLite schema\nTABLE products: name\n\n### Question\nList names."
    return {
        "sample_id": "spider_train:00000",
        "split": {"name": "train"},
        "execution_outcome": {"sqlite_readonly_explain": "pass"},
        "training_text": prompt + "\n\n### SQL\n" + sql,
        "candidate_sql": sql,
    }


def test_sft_dataset_masks_prompt_and_supervises_only_sql_target() -> None:
    tokenizer = TinyTokenizer()
    dataset = CausalSqlDataset([row()], tokenizer, max_seq_length=512)

    example = dataset[0]
    supervised = example["labels"][example["labels"] != -100].tolist()
    assert supervised[-1] == tokenizer.eos_token_id
    assert len(supervised) == dataset.stats["max_target_tokens"]
    assert dataset.stats == {
        "samples": 1,
        "max_sequence_tokens": example["input_ids"].size(0),
        "max_target_tokens": len(supervised),
    }

    batch = CausalSqlCollator(tokenizer.pad_token_id)([example, example])
    assert batch["input_ids"].shape == batch["labels"].shape == (2, example["input_ids"].size(0))


def test_sft_dataset_rejects_mismatched_target_or_silent_truncation() -> None:
    tokenizer = TinyTokenizer()
    mismatched = row()
    mismatched["candidate_sql"] = "SELECT other FROM products"
    with pytest.raises(ValueError, match="target SQL mismatch"):
        CausalSqlDataset([mismatched], tokenizer, max_seq_length=512)

    with pytest.raises(ValueError, match="refuse to truncate target SQL"):
        CausalSqlDataset([row()], tokenizer, max_seq_length=10)


def test_split_audit_must_match_current_split_files(tmp_path) -> None:
    train_path = tmp_path / "train.jsonl"
    validation_path = tmp_path / "validation.jsonl"
    train_path.write_text("train-row\n", encoding="utf-8")
    validation_path.write_text("validation-row\n", encoding="utf-8")
    audit = {
        "checks": {"status": "pass", "v2_holdout_used": False},
        "policy": {"primary_group": "spider_db_id"},
        "splits": {
            "train": {"rows": 1, "sha256": sha256_file(train_path)},
            "validation": {"rows": 1, "sha256": sha256_file(validation_path)},
        },
    }

    validate_split_audit(audit, train_path, validation_path)

    train_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="train file does not match split audit"):
        validate_split_audit(audit, train_path, validation_path)


def cspider_audit(train_path: Path, validation_path: Path, test_path: Path) -> dict[str, object]:
    return {
        "source": {"dataset": {"id": "cspider", "release": "full-2024-03-01"}},
        "policy": {
            "split_strategy": "official_cspider_train_dev_test",
            "primary_group": "cspider_db_id",
            "test_storage": "final_evaluation_only",
            "test_forbidden_for_training": True,
        },
        "splits": {
            "train": {
                "rows": 1,
                "sha256": sha256_file(train_path),
                "official_split": "train",
                "role": "parameter_updates",
            },
            "validation": {
                "rows": 1,
                "sha256": sha256_file(validation_path),
                "official_split": "dev",
                "role": "validation_only",
            },
            "test": {
                "rows": 1,
                "sha256": sha256_file(test_path),
                "official_split": "test",
                "role": "final_evaluation_only",
                "forbidden_for_training": True,
            },
        },
        "checks": {
            "status": "pass",
            "raw_data_in_git": False,
            "train_validation_database_overlap": [],
            "train_test_database_overlap": [],
            "validation_test_database_overlap": [],
            "sqlite_readonly_explain": {
                "train": {"pass": 1},
                "dev": {"pass": 1},
                "test": {"pass": 1},
            },
        },
        "outputs": {
            "train_jsonl": str(train_path),
            "validation_jsonl": str(validation_path),
            "test_jsonl": str(test_path),
        },
    }


def test_cspider_audit_accepts_official_train_and_validation_only(tmp_path) -> None:
    train_path = tmp_path / "train.jsonl"
    validation_path = tmp_path / "validation.jsonl"
    test_path = tmp_path / "final_evaluation_only" / "test.jsonl"
    test_path.parent.mkdir()
    for path, split_name in ((train_path, "train"), (validation_path, "validation"), (test_path, "test")):
        path.write_text(json.dumps({"split": {"name": split_name}}) + "\n", encoding="utf-8")

    validate_split_audit(cspider_audit(train_path, validation_path, test_path), train_path, validation_path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda audit, _train, _validation, _test: audit["policy"].__setitem__(
                "test_forbidden_for_training", False
            ),
            "does not forbid test training use",
        ),
        (
            lambda audit, _train, _validation, _test: audit["checks"].__setitem__(
                "train_test_database_overlap", ["leaked_db"]
            ),
            "non-empty train_test_database_overlap",
        ),
        (
            lambda audit, _train, _validation, test: audit["outputs"].__setitem__(
                "train_jsonl", str(test)
            ),
            "train_jsonl does not match the requested input",
        ),
    ],
)
def test_cspider_audit_rejects_missing_final_test_isolation(tmp_path, mutate, message) -> None:
    train_path = tmp_path / "train.jsonl"
    validation_path = tmp_path / "validation.jsonl"
    test_path = tmp_path / "final_evaluation_only" / "test.jsonl"
    test_path.parent.mkdir()
    for path in (train_path, validation_path, test_path):
        path.write_text("row\n", encoding="utf-8")
    audit = cspider_audit(train_path, validation_path, test_path)

    mutate(audit, train_path, validation_path, test_path)

    with pytest.raises(ValueError, match=message):
        validate_split_audit(audit, train_path, validation_path)
