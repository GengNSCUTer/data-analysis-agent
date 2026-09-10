"""Small, deterministic checks shared by the Qwen3.5 Olist SFT trainer."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .qwen35_sft_format import Qwen35SftFormatError


QWEN35_LORA_TARGET_SUFFIXES = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
)
QWEN35_LANGUAGE_MODULE_PREFIX = "model.language_model.layers."


def select_qwen35_language_lora_targets(module_names: Iterable[str]) -> tuple[str, ...]:
    """Return the real text-model LoRA targets, rejecting ambiguous modules.

    Qwen3.5-4B is a hybrid model: only some language layers expose attention
    projections, while all language layers expose MLP projections.  A suffix
    list alone is unsafe if a future multimodal component uses the same names,
    so every matched module must belong to the text language-model path.
    """

    targets: list[str] = []
    matched_suffixes: set[str] = set()
    for name in module_names:
        suffix = next(
            (item for item in QWEN35_LORA_TARGET_SUFFIXES if name.endswith(f".{item}")),
            None,
        )
        if suffix is None:
            continue
        if not name.startswith(QWEN35_LANGUAGE_MODULE_PREFIX):
            raise Qwen35SftFormatError(
                f"ambiguous Qwen3.5 LoRA target outside language model: {name}"
            )
        targets.append(name)
        matched_suffixes.add(suffix)
    missing = sorted(set(QWEN35_LORA_TARGET_SUFFIXES) - matched_suffixes)
    if missing:
        raise Qwen35SftFormatError(
            "frozen Qwen3.5 model lacks expected LoRA target suffixes: "
            + ", ".join(missing)
        )
    if not targets:
        raise Qwen35SftFormatError("frozen Qwen3.5 model has no LoRA target modules")
    return tuple(sorted(targets))


def select_bounded_rows(
    rows: list[dict[str, Any]],
    limit: int | None,
    label: str,
    selection: str,
    sequence_length: Callable[[dict[str, Any]], int],
) -> list[dict[str, Any]]:
    """Select an explicit bounded smoke subset without changing full-run order.

    Shortest-sequence selection exists only to prove the full Trainer lifecycle
    under a shared GPU. It is not a length-cap test and cannot establish that
    the formal 3,072-token run fits; the frozen CPU layout audit owns that
    separate statement.
    """

    if limit is None:
        return rows
    if limit <= 0 or limit > len(rows):
        raise Qwen35SftFormatError(f"{label} sample limit must be in [1, {len(rows)}]")
    if selection == "first":
        return rows[:limit]
    if selection == "shortest_sequence":
        ranked = sorted(
            ((int(sequence_length(row)), index, row) for index, row in enumerate(rows)),
            key=lambda item: (item[0], item[1]),
        )
        return [row for _, _, row in ranked[:limit]]
    raise Qwen35SftFormatError(f"unsupported {label} sample selection: {selection}")
