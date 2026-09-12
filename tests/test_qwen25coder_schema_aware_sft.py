from __future__ import annotations

import pytest
import torch

from scripts.post_training.training.run_qwen25coder_schema_aware_sft import (
    PairEventDataset,
    RightPaddingCollator,
    SchemaAwareTrainerError,
)


class DummyTokenizer:
    eos_token_id = 99
    pad_token_id = 0

    def __call__(self, text: str, *, add_special_tokens: bool = False) -> dict[str, list[int]]:
        assert add_special_tokens is False
        return {"input_ids": [ord(char) % 50 + 1 for char in text]}


def _row(task: str, prompt: str = "prompt", target: str = "target") -> dict[str, str]:
    return {
        "event_id": f"pair:{task}",
        "pair_id": "pair",
        "task_type": task,
        "rendered_prompt": prompt,
        "target_text": target,
    }


def test_pair_dataset_masks_prompt_and_supervises_eos() -> None:
    dataset = PairEventDataset([_row("sql")], DummyTokenizer(), max_seq_length=100)

    item = dataset[0]
    prompt_length = len("prompt\n")
    assert item["input_ids"].tolist()[-1] == DummyTokenizer.eos_token_id
    assert item["labels"].tolist()[:prompt_length] == [-100] * prompt_length
    assert item["labels"].tolist()[prompt_length:-1] != [-100] * len("target")
    assert dataset.stats["task_counts"] == {"sql": 1}


def test_pair_dataset_rejects_empty_target() -> None:
    with pytest.raises(SchemaAwareTrainerError, match="empty target"):
        PairEventDataset([_row("schema_link_plan", target="")], DummyTokenizer(), 100)


def test_pair_dataset_rejects_length_overflow() -> None:
    with pytest.raises(SchemaAwareTrainerError, match="max_seq_length"):
        PairEventDataset([_row("sql", prompt="p" * 20, target="t" * 20)], DummyTokenizer(), 10)


def test_right_padding_masks_padding_from_attention_and_loss() -> None:
    dataset = PairEventDataset([_row("sql", target="t"), _row("schema_link_plan", target="long")], DummyTokenizer(), 100)
    batch = RightPaddingCollator(DummyTokenizer.pad_token_id)([dataset[0], dataset[1]])

    assert batch["input_ids"].shape[0] == 2
    assert torch.all(batch["attention_mask"][0, len(dataset[0]["input_ids"]):] == 0)
    assert torch.all(batch["labels"][0, len(dataset[0]["labels"]):] == -100)

