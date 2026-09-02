from __future__ import annotations

from scripts.post_training.data.audit_cspider_sft_token_lengths import (
    analyze_rows,
    nearest_rank,
    tokenizer_assets_sha256,
)


class TinyTokenizer:
    eos_token_id = 99
    pad_token_id = 99

    def __call__(self, text: str, add_special_tokens: bool = False):
        assert add_special_tokens is False
        return {"input_ids": [ord(character) for character in text]}


def row(sample_id: str, sql: str) -> dict[str, object]:
    prompt = "### SQLite schema\nTABLE products: name\n\n### Question\nList names."
    return {
        "sample_id": sample_id,
        "training_text": prompt + "\n\n### SQL\n" + sql,
        "candidate_sql": sql,
    }


def test_token_audit_counts_trainer_layout_and_never_truncates() -> None:
    tokenizer = TinyTokenizer()
    rows = [row("one", "SELECT name"), row("two", "SELECT longer_name")]

    result = analyze_rows(rows, tokenizer, max_seq_length=90, comparison_lengths=[50, 100])

    prompt_length = len(tokenizer("### SQLite schema\nTABLE products: name\n\n### Question\nList names.\n\n### SQL\n")["input_ids"])
    assert result["prompt_tokens"]["min"] == prompt_length
    assert result["target_plus_eos_tokens"]["min"] == len("SELECT name") + 1
    assert result["sequence_tokens"]["max"] == prompt_length + len("SELECT longer_name") + 1
    assert result["over_budget_rows"]["50"] == 2
    assert result["over_budget_rows"]["100"] == 0
    # One sample is at or below the contract limit; the longer one is not.
    assert result["eligible_rows_at_contract_limit"] == 1
    assert result["counting_contract"]["silent_truncation"] is False


def test_nearest_rank_percentile_is_stable_at_small_sample_counts() -> None:
    values = [2, 10, 20, 100]

    assert nearest_rank(values, 50) == 10
    assert nearest_rank(values, 90) == 100
    assert nearest_rank(values, 100) == 100


def test_tokenizer_fingerprint_excludes_model_weight_files(tmp_path) -> None:
    (tmp_path / "tokenizer.json").write_text("tokenizer", encoding="utf-8")
    (tmp_path / "vocab.json").write_text("vocabulary", encoding="utf-8")
    (tmp_path / "model.safetensors").write_text("must not be read", encoding="utf-8")

    filenames, fingerprint = tokenizer_assets_sha256(tmp_path)

    assert filenames == ["tokenizer.json", "vocab.json"]
    assert len(fingerprint) == 64
