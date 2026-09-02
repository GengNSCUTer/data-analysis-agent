from __future__ import annotations

import pytest

from scripts.post_training.data.materialize_cspider_sft_splits import materialize_rows


class TinyTokenizer:
    eos_token_id = 99

    def __call__(self, text: str, add_special_tokens: bool = False):
        del add_special_tokens
        return {"input_ids": [ord(char) % 17 + 1 for char in text]}


def row(sample_id: str, sql: str = "SELECT 1") -> dict[str, object]:
    prompt = "### Question\n示例"
    return {
        "sample_id": sample_id,
        "training_text": prompt + "\n\n### SQL\n" + sql,
        "candidate_sql": sql,
    }


def test_materializer_excludes_only_over_budget_train_rows() -> None:
    tokenizer = TinyTokenizer()
    rows = [row("keep"), row("exclude", "x" * 80)]

    kept, excluded, stats = materialize_rows(
        rows, tokenizer, max_seq_length=45, split="train", filter_over_budget=True
    )

    assert [item["sample_id"] for item in kept] == ["keep"]
    assert [item["sample_id"] for item in excluded] == ["exclude"]
    assert excluded[0]["reason"] == "sequence_exceeds_frozen_contract"
    assert excluded[0]["eligible_for_sft"] is False
    assert stats["source_rows"] == 2
    assert stats["kept_rows"] == 1
    assert stats["excluded_rows"] == 1
    assert stats["max_sequence_tokens"] <= 45
    assert stats["source_max_sequence_tokens"] > 45


def test_materializer_fails_closed_instead_of_filtering_final_test() -> None:
    with pytest.raises(ValueError, match="final test cannot be filtered"):
        materialize_rows(
            [row("too-long", "x" * 80)],
            TinyTokenizer(),
            max_seq_length=45,
            split="test",
            filter_over_budget=False,
        )
