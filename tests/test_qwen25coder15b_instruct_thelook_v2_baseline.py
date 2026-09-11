from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_analysis_agent.thelook_v2_matching import TheLookV2MatchingError
from scripts.post_training.evaluation.run_qwen25coder15b_instruct_thelook_v2_baseline_generation import (
    MODEL_ID,
    MODEL_REVISION,
    _chat_text,
    _messages_for_server_prompt,
    _read_model_manifest,
    _sha256_texts,
    _template_sha256,
)


class _FakeTokenizer:
    chat_template = "official-template-v1"

    def __init__(self) -> None:
        self.calls: list[tuple[object, bool, bool]] = []

    def apply_chat_template(
        self, messages: object, *, tokenize: bool, add_generation_prompt: bool, **_: object
    ) -> str:
        self.calls.append((messages, tokenize, add_generation_prompt))
        assert tokenize is False
        return "<|im_start|>user\nserver prompt<|im_end|>\n<|im_start|>assistant\n"


def test_instruct_runner_uses_one_unchanged_user_turn_and_official_template() -> None:
    tokenizer = _FakeTokenizer()
    prompt = "### Task\nGenerate SQL only"

    assert _messages_for_server_prompt(prompt) == [{"role": "user", "content": prompt}]
    assert _chat_text(tokenizer, prompt).endswith("<|im_start|>assistant\n")
    assert tokenizer.calls == [
        ([{"role": "user", "content": prompt}], False, True)
    ]
    assert _template_sha256(tokenizer) == _template_sha256(tokenizer)
    assert _sha256_texts(["a", "b"]) != _sha256_texts(["b", "a"])


def test_instruct_runner_rejects_empty_prompt() -> None:
    with pytest.raises(TheLookV2MatchingError, match="non-empty"):
        _messages_for_server_prompt("  ")


def test_instruct_runner_binds_the_frozen_official_model_revision(tmp_path: Path) -> None:
    (tmp_path / "download_manifest.json").write_text(
        json.dumps({"model_id": MODEL_ID, "revision": MODEL_REVISION}), encoding="utf-8"
    )
    assert _read_model_manifest(tmp_path)["model_id"] == MODEL_ID

    (tmp_path / "download_manifest.json").write_text(
        json.dumps({"model_id": MODEL_ID, "revision": "wrong"}), encoding="utf-8"
    )
    with pytest.raises(TheLookV2MatchingError, match="frozen"):
        _read_model_manifest(tmp_path)
