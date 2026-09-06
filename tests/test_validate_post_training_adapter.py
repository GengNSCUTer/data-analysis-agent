from __future__ import annotations

from scripts.post_training.training.validate_post_training_adapter import encode


class _Tokenizer:
    eos_token_id = 99

    def __call__(self, text: str, *, add_special_tokens: bool) -> dict[str, list[int]]:
        assert add_special_tokens is False
        return {"input_ids": list(range(len(text)))}


def test_encode_accepts_olist_runtime_prompt_layout() -> None:
    prompt = "### Task\nGenerate SQL.\n### SQL"
    sql = "SELECT 1;"

    batch = encode(
        {
            "rendered_prompt": prompt,
            "training_text": prompt + "\n" + sql,
            "candidate_sql": sql,
        },
        _Tokenizer(),
        max_seq_length=128,
    )

    prompt_length = len(prompt + "\n")
    assert batch["labels"][0, :prompt_length].tolist() == [-100] * prompt_length
    assert batch["labels"][0, -1].item() == 99


def test_encode_rejects_olist_runtime_prompt_target_drift() -> None:
    prompt = "### SQL"
    try:
        encode(
            {
                "rendered_prompt": prompt,
                "training_text": prompt + "\nSELECT 2;",
                "candidate_sql": "SELECT 1;",
            },
            _Tokenizer(),
            max_seq_length=128,
        )
    except ValueError as exc:
        assert "rendered runtime prompt target" in str(exc)
    else:
        raise AssertionError("target drift must be rejected")
