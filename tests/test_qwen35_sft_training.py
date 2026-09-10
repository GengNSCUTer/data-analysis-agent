from __future__ import annotations

import pytest

from data_analysis_agent.qwen35_sft_format import Qwen35SftFormatError
from data_analysis_agent.qwen35_sft_training import (
    QWEN35_LORA_TARGET_SUFFIXES,
    select_bounded_rows,
    select_qwen35_language_lora_targets,
)


def _one_name_per_suffix() -> list[str]:
    return [f"model.language_model.layers.0.mlp.{suffix}" for suffix in QWEN35_LORA_TARGET_SUFFIXES]


def test_target_selector_accepts_only_real_language_model_projection_paths() -> None:
    targets = select_qwen35_language_lora_targets(
        ["model.language_model.layers.0.input_layernorm", *_one_name_per_suffix()]
    )

    assert targets == tuple(sorted(_one_name_per_suffix()))


def test_target_selector_rejects_non_language_projection_name() -> None:
    names = _one_name_per_suffix() + ["model.visual.blocks.0.self_attn.q_proj"]

    with pytest.raises(Qwen35SftFormatError, match="outside language model"):
        select_qwen35_language_lora_targets(names)


def test_target_selector_rejects_missing_expected_suffix() -> None:
    names = _one_name_per_suffix()
    names.remove("model.language_model.layers.0.mlp.down_proj")

    with pytest.raises(Qwen35SftFormatError, match="down_proj"):
        select_qwen35_language_lora_targets(names)


def test_bounded_rows_shortest_sequence_is_stable_and_only_for_limited_runs() -> None:
    rows = [{"tokens": 8}, {"tokens": 3}, {"tokens": 3}, {"tokens": 5}]

    selected = select_bounded_rows(
        rows,
        limit=2,
        label="validation",
        selection="shortest_sequence",
        sequence_length=lambda row: row["tokens"],
    )

    assert selected == [{"tokens": 3}, {"tokens": 3}]
    assert select_bounded_rows(
        rows,
        limit=None,
        label="train",
        selection="shortest_sequence",
        sequence_length=lambda row: row["tokens"],
    ) is rows
