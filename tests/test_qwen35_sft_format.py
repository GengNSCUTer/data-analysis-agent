from __future__ import annotations

import pytest

from data_analysis_agent.qwen35_sft_format import (
    IGNORE_INDEX,
    Qwen35SftFormatError,
    build_qwen35_sql_sft_example,
    qwen35_sql_messages,
)


class StubTokenizer:
    eos_token_id = 99


class StubProcessor:
    tokenizer = StubTokenizer()

    def __init__(self, *, preserve_prefix: bool = True, include_eot: bool = True) -> None:
        self.preserve_prefix = preserve_prefix
        self.include_eot = include_eot
        self.calls: list[dict[str, object]] = []

    def apply_chat_template(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if kwargs["add_generation_prompt"]:
            ids = [11, 12, 13]
        elif self.preserve_prefix:
            ids = [11, 12, 13, 21, 22] + ([99, 198] if self.include_eot else [])
        else:
            ids = [11, 12, 77, 21, 22, 99, 198]
        return {"input_ids": [ids]}


def test_qwen35_layout_masks_template_prefix_and_supervises_sql_through_eot() -> None:
    processor = StubProcessor()
    example = build_qwen35_sql_sft_example(
        processor, "runtime prompt\n### SQL", "SELECT 1;", max_seq_length=16
    )

    assert example.input_ids == (11, 12, 13, 21, 22, 99)
    assert example.labels == (IGNORE_INDEX, IGNORE_INDEX, IGNORE_INDEX, 21, 22, 99)
    assert example.attention_mask == (1, 1, 1, 1, 1, 1)
    assert example.prompt_tokens == 3
    assert example.supervised_tokens == 3
    assert processor.calls[0]["enable_thinking"] is False
    assert processor.calls[0]["add_generation_prompt"] is True
    assert processor.calls[1]["add_generation_prompt"] is False


def test_qwen35_layout_uses_runtime_prompt_as_unchanged_user_content() -> None:
    prefix, full = qwen35_sql_messages(" runtime prompt \n", " SELECT 1; \n")

    assert prefix[0]["role"] == "user"
    assert prefix[0]["content"][0]["text"] == " runtime prompt"
    assert full[1]["role"] == "assistant"
    assert full[1]["content"][0]["text"] == "SELECT 1;"


@pytest.mark.parametrize(
    ("processor", "message"),
    [
        (StubProcessor(preserve_prefix=False), "does not preserve the generation prefix"),
        (StubProcessor(include_eot=False), "has no end-of-turn token"),
    ],
)
def test_qwen35_layout_fails_closed_when_template_boundary_is_not_provable(processor, message) -> None:
    with pytest.raises(Qwen35SftFormatError, match=message):
        build_qwen35_sql_sft_example(processor, "prompt", "SELECT 1;", max_seq_length=16)


def test_qwen35_layout_refuses_silent_length_truncation() -> None:
    with pytest.raises(Qwen35SftFormatError, match="refuse to truncate"):
        build_qwen35_sql_sft_example(StubProcessor(), "prompt", "SELECT 1;", max_seq_length=5)
