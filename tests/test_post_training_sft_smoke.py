from __future__ import annotations

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
