"""Frozen Qwen3.5 Instruct layout for SQL-only candidate SFT.

The Olist runtime prompt remains the entire user message.  This module adds
only Qwen3.5's official message envelope and derives the causal-LM labels from
the exact token boundary produced by that envelope.  It deliberately does not
load model weights, read a database, or render SQL.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


QWEN35_OLIST_SFT_TEMPLATE_VERSION = "qwen35-olist-instruct-sft-v1"
IGNORE_INDEX = -100


class Qwen35SftFormatError(ValueError):
    """Raised when the frozen Qwen3.5 prompt/label boundary is not provable."""


@dataclass(frozen=True)
class Qwen35SqlSftExample:
    """One unpadded causal-LM example with only SQL/EOT supervised."""

    input_ids: tuple[int, ...]
    attention_mask: tuple[int, ...]
    labels: tuple[int, ...]
    prompt_tokens: int
    supervised_tokens: int


def qwen35_sql_messages(
    rendered_prompt: str, candidate_sql: str
) -> tuple[list[dict[str, list[dict[str, str]]]], list[dict[str, list[dict[str, str]]]]]:
    """Return official-template prefix and full conversations.

    ``rendered_prompt`` is intentionally not rewritten into a synthetic schema
    prompt.  The prefix contains the user turn plus the assistant generation
    boundary; the full conversation adds a SQL-only assistant turn.
    """

    if not isinstance(rendered_prompt, str) or not rendered_prompt.strip():
        raise Qwen35SftFormatError("rendered runtime prompt must be a non-empty string")
    if not isinstance(candidate_sql, str) or not candidate_sql.strip():
        raise Qwen35SftFormatError("candidate SQL must be a non-empty string")
    user_turn: dict[str, list[dict[str, str]]] = {
        "role": "user",
        "content": [{"type": "text", "text": rendered_prompt.rstrip()}],
    }
    assistant_turn: dict[str, list[dict[str, str]]] = {
        "role": "assistant",
        "content": [{"type": "text", "text": candidate_sql.strip()}],
    }
    return [user_turn], [user_turn, assistant_turn]


def _single_input_ids(value: Any) -> list[int]:
    """Extract exactly one text sequence from ``AutoProcessor`` output.

    ``AutoProcessor.apply_chat_template(..., return_dict=True)`` returns a
    batch-shaped ``input_ids`` even for a single conversation.  Treating its
    outer list as a token sequence silently reports a length of one, so this
    extraction is intentionally strict.
    """

    if not isinstance(value, Mapping):
        raise Qwen35SftFormatError("chat template must return a mapping")
    batch = value.get("input_ids")
    if not isinstance(batch, Sequence) or isinstance(batch, (str, bytes)) or len(batch) != 1:
        raise Qwen35SftFormatError("chat template must return exactly one input-id batch")
    sequence = batch[0]
    if not isinstance(sequence, Sequence) or isinstance(sequence, (str, bytes)):
        raise Qwen35SftFormatError("chat template input IDs must be a token sequence")
    if not sequence or any(not isinstance(token, int) for token in sequence):
        raise Qwen35SftFormatError("chat template returned invalid input IDs")
    return list(sequence)


def _tokenize(processor: Any, messages: list[dict[str, list[dict[str, str]]]], *, add_generation_prompt: bool) -> list[int]:
    output = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=add_generation_prompt,
        return_dict=True,
        return_tensors=None,
        enable_thinking=False,
    )
    return _single_input_ids(output)


def _trim_after_assistant_eot(token_ids: list[int], prefix_tokens: int, eos_token_id: int | None) -> list[int]:
    """Keep SQL through the assistant EOT and drop template-only trailing newline.

    Qwen3.5's template serializes an assistant turn as ``SQL + <|im_end|> +
    newline``.  Generation stops at ``<|im_end|>``, so supervising the later
    newline would create a train/inference mismatch.  The user-turn EOT occurs
    in the masked prefix; only an EOT in the supervised suffix is considered.
    """

    if not isinstance(eos_token_id, int):
        raise Qwen35SftFormatError("Qwen3.5 processor lacks an integer eos token ID")
    suffix = token_ids[prefix_tokens:]
    try:
        final_eot_offset = len(suffix) - 1 - suffix[::-1].index(eos_token_id)
    except ValueError as exc:
        raise Qwen35SftFormatError("assistant SQL completion has no end-of-turn token") from exc
    if final_eot_offset == 0:
        raise Qwen35SftFormatError("assistant SQL completion has no supervised SQL token")
    return token_ids[: prefix_tokens + final_eot_offset + 1]


def build_qwen35_sql_sft_example(
    processor: Any,
    rendered_prompt: str,
    candidate_sql: str,
    *,
    max_seq_length: int,
) -> Qwen35SqlSftExample:
    """Construct an exact Qwen3.5 Instruct SFT layout without truncation."""

    if not isinstance(max_seq_length, int) or max_seq_length <= 0:
        raise Qwen35SftFormatError("max sequence length must be a positive integer")
    prefix_messages, full_messages = qwen35_sql_messages(rendered_prompt, candidate_sql)
    prefix_ids = _tokenize(processor, prefix_messages, add_generation_prompt=True)
    full_ids = _tokenize(processor, full_messages, add_generation_prompt=False)
    if full_ids[: len(prefix_ids)] != prefix_ids:
        raise Qwen35SftFormatError(
            "full chat-template sequence does not preserve the generation prefix"
        )
    tokenizer = getattr(processor, "tokenizer", None)
    eos_token_id = getattr(tokenizer, "eos_token_id", None)
    input_ids = _trim_after_assistant_eot(full_ids, len(prefix_ids), eos_token_id)
    supervised_tokens = len(input_ids) - len(prefix_ids)
    if supervised_tokens <= 1:
        raise Qwen35SftFormatError("SQL target must contain SQL plus an end-of-turn token")
    if len(input_ids) > max_seq_length:
        raise Qwen35SftFormatError(
            f"Qwen3.5 sequence has {len(input_ids)} tokens above max_seq_length={max_seq_length}; "
            "refuse to truncate prompt or SQL"
        )
    labels = [IGNORE_INDEX] * len(prefix_ids) + input_ids[len(prefix_ids) :]
    return Qwen35SqlSftExample(
        input_ids=tuple(input_ids),
        attention_mask=tuple([1] * len(input_ids)),
        labels=tuple(labels),
        prompt_tokens=len(prefix_ids),
        supervised_tokens=supervised_tokens,
    )
